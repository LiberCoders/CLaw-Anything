"""LoopAgent: claw-anything's native agent implementation."""

from __future__ import annotations

import json
import time
from pathlib import Path

from claw_anything.config import ModelConfig, PromptConfig
from claw_anything.models.content import ContentBlock, TextBlock
from claw_anything.models.message import Message
from claw_anything.models.task import TaskDefinition
from claw_anything.models.tool import ToolSpec
from claw_anything.runner.dispatcher import ToolDispatcher
from claw_anything.runner.providers.openai_compat import OpenAICompatProvider
from claw_anything.runner.system_prompt import build_system_prompt

from .base import BaseAgent


def _log(msg: str) -> None:
    """Print a log line and flush immediately (important for container logs)."""
    print(msg, flush=True)


def _brief(d: dict, max_len: int = 80) -> str:
    """Compact one-line summary of a dict for logging."""
    s = json.dumps(d, ensure_ascii=False)
    return s if len(s) <= max_len else s[:max_len] + "..."


class LoopAgent(BaseAgent):
    """claw-anything's native agent implementation using its built-in loop."""

    def _execute(
        self,
        task: TaskDefinition,
        *,
        provider: OpenAICompatProvider,
        prompt_cfg: PromptConfig | None = None,
        model_cfg: ModelConfig | None = None,
        sandbox_tools: bool = False,
        sandbox_url: str | None = None,
        workspace_root: Path | None = None,
        content_blocks: list[ContentBlock],
    ) -> None:
        """Execute the think-act-observe loop."""

        trace_id = self._trace_id

        endpoint_map = task.get_endpoint_map()
        http_dispatcher = ToolDispatcher(endpoint_map)

        sandbox_tool_list = None
        if sandbox_tools:
            from claw_anything.runner.sandbox_dispatcher import SandboxToolDispatcher
            from claw_anything.runner.sandbox_tools import get_sandbox_tool_specs

            sandbox_tool_list = get_sandbox_tool_specs({t.name for t in task.tools})
            task_tools = list(task.tools) + sandbox_tool_list
            dispatcher = SandboxToolDispatcher(
                http_dispatcher,
                sandbox_url=sandbox_url,
                workspace_root=workspace_root,
            )
        else:
            task_tools = list(task.tools)
            dispatcher = http_dispatcher

        # Skill mode: progressive tool revelation
        _skill_mode = prompt_cfg is not None and prompt_cfg.skill_mode
        if _skill_mode:
            from claw_anything.runner.skill_mode import GET_TOOL_SCHEMA_SPEC, handle_get_tool_schema

            all_tools_by_name: dict[str, ToolSpec] = {t.name: t for t in task_tools}
            active_tools: list[ToolSpec] = [GET_TOOL_SCHEMA_SPEC]
            activated_names: set[str] = {"get_tool_schema"}
        else:
            active_tools = task_tools

        _log(f"[start] task={task.task_id} model={provider.model_id}")
        _log(f"[config] max_turns={task.environment.max_turns} timeout={task.environment.timeout_seconds}s sandbox_tools={sandbox_tools}")

        # Build initial messages
        system_prompt = build_system_prompt(task, prompt_cfg, extra_tools=sandbox_tool_list)
        if model_cfg and model_cfg.system_prompt_prefix:
            system_prompt = model_cfg.system_prompt_prefix + "\n\n" + system_prompt
        self._system_prompt = system_prompt

        messages: list[Message] = [
            Message(role="system", content=[TextBlock(text=system_prompt)]),
            Message(role="user", content=content_blocks),
        ]

        turn_count = 0
        try:
            while turn_count < task.environment.max_turns:
                # Check timeout
                elapsed = time.monotonic() - self._wall_start
                if elapsed > task.environment.timeout_seconds:
                    _log(f"[timeout] {elapsed:.1f}s exceeded limit {task.environment.timeout_seconds}s")
                    break

                # Call model
                _log(f"[turn {turn_count + 1}/{task.environment.max_turns}] calling model ...")
                model_t0 = time.monotonic()
                response, usage = provider.chat(messages, tools=active_tools)
                self.add_model_time(time.monotonic() - model_t0)
                self.increment_turn_count()
                turn_count += 1

                # Log assistant message
                self.emit_message(response, usage)

                messages.append(response)

                # Summarize what the model returned
                text_blocks = [b for b in response.content if b.type == "text"]
                tool_uses = [b for b in response.content if b.type == "tool_use"]
                text_preview = text_blocks[0].text[:120].replace("\n", " ") if text_blocks else ""
                _log(f"[turn {turn_count}] assistant: {len(text_blocks)} text, {len(tool_uses)} tool_use | tokens: +{usage.input_tokens}in +{usage.output_tokens}out")
                if text_preview:
                    _log(f"  text: {text_preview}{'...' if len(text_blocks[0].text) > 120 else ''}")

                if not tool_uses:
                    _log(f"[done] no tool calls — agent finished at turn {turn_count}")
                    break

                # Dispatch each tool call
                result_blocks = []
                for tu in tool_uses:
                    _log(f"  -> tool: {tu.name}({_brief(tu.input)})")
                    if _skill_mode and tu.name == "get_tool_schema":
                        result, dispatch_event, newly_activated = handle_get_tool_schema(
                            tu, trace_id, all_tools_by_name
                        )
                        for name in newly_activated:
                            if name not in activated_names:
                                activated_names.add(name)
                                active_tools.append(all_tools_by_name[name])
                    else:
                        result, dispatch_event = dispatcher.dispatch(tu, trace_id)
                    self.emit_tool_dispatch(dispatch_event)
                    result_blocks.append(result)
                    status_tag = "OK" if not result.is_error else "ERR"
                    _log(f"  <- {tu.name}: {status_tag} ({dispatch_event.latency_ms:.0f}ms)")

                # Append tool results as a user message
                tool_msg = Message(role="user", content=result_blocks)
                messages.append(tool_msg)
                self.emit_message(tool_msg)
        finally:
            dispatcher.close()

        total_tok = self._total_usage.input_tokens + self._total_usage.output_tokens
        _log(
            f"[end] turns={turn_count} tokens={total_tok} "
            f"({self._total_usage.input_tokens}in/{self._total_usage.output_tokens}out) "
            f"time=wall {time.monotonic() - self._wall_start:.1f}s"
        )
