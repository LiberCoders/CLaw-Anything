"""BaseAgent: abstract base class that manages the trace lifecycle.

Concrete agents implement ``_execute()`` and report results via
``emit_message()`` / ``emit_tool_dispatch()`` — they never touch the
``TraceWriter`` directly.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any
from uuid import uuid4

from claw_anything.config import ModelConfig, PromptConfig
from claw_anything.models.content import ContentBlock, TextBlock
from claw_anything.models.message import Message
from claw_anything.models.task import TaskDefinition
from claw_anything.models.trace import (
    AuditSnapshot,
    TokenUsage,
    ToolDispatch,
    TraceEnd,
    TraceMessage,
    TraceStart,
)
from claw_anything.runner.providers.openai_compat import OpenAICompatProvider
from claw_anything.trace.writer import TraceWriter


class BaseAgent(ABC):
    """Abstract base class for all agent implementations.

    Handles the full trace lifecycle (TraceStart, TraceEnd, audit snapshots,
    token/time accumulation).  Concrete subclasses only implement
    ``_execute()`` and use ``emit_*`` helpers to report events.
    """

    # ------------------------------------------------------------------ #
    #  Public entry point (not overridden by subclasses)                  #
    # ------------------------------------------------------------------ #

    def run_task(
        self,
        task: TaskDefinition,
        trace_dir: Path | str,
        *,
        provider: OpenAICompatProvider,
        prompt_cfg: PromptConfig | None = None,
        model_cfg: ModelConfig | None = None,
        task_dir: str | Path | None = None,
        sandbox_tools: bool = False,
        sandbox_url: str | None = None,
        workspace_root: Path | None = None,
        agent_type: str = "loop",
        trace_id: str | None = None,
    ) -> tuple[Path, dict]:
        """Execute a task and return (trace_path, openai_data).

        ``openai_data`` is a dict with ``messages`` and optional ``tools``
        keys in OpenAI format, used by Daily-Bench's OpenAI trace exporter.
        Agents that do not collect OpenAI-format data return an empty dict;
        ``task_dir`` is accepted for source-path context but not used by
        BaseAgent itself (subclasses may consult it).

        ``trace_id`` may be supplied by the caller so trial artifacts written
        elsewhere (e.g. GUI-init screenshots) co-locate with the JSONL trace;
        when omitted a fresh UUID is generated.
        """

        # --- initialise per-run state ---
        self._trace_id: str = trace_id or str(uuid4())
        trace_path = Path(trace_dir) / f"{task.task_id}_{self._trace_id[:8]}.jsonl"

        self._total_usage = TokenUsage()
        self._model_time_s: float = 0.0
        self._tool_time_s: float = 0.0
        self._turn_count: int = 0
        self._wall_start: float = time.monotonic()
        self._failure_modes: list[str] = []

        loop_error: str | None = None
        loop_exc: Exception | None = None

        with TraceWriter(trace_path) as writer:
            self._writer = writer

            # 1. TraceStart
            writer.write_event(TraceStart(
                trace_id=self._trace_id,
                task_id=task.task_id,
                model=provider.model_id,
                agent_type=agent_type,
            ))

            # 2. Build initial user message (text-only)
            content_blocks: list[ContentBlock] = [TextBlock(text=task.prompt.text)]
            writer.write_event(TraceMessage(
                trace_id=self._trace_id,
                message=Message(role="user", content=content_blocks),
            ))

            # 3. Execute agent core logic
            try:
                self._execute(
                    task,
                    provider=provider,
                    prompt_cfg=prompt_cfg,
                    model_cfg=model_cfg,
                    sandbox_tools=sandbox_tools,
                    sandbox_url=sandbox_url,
                    workspace_root=workspace_root,
                    content_blocks=content_blocks,
                )
            except Exception as exc:
                loop_error = f"{type(exc).__name__}: {exc}"
                loop_exc = exc
                print(f"[agent] loop error: {loop_error}", flush=True)

            # 4. Audit snapshots (best-effort)
            self._fetch_audit_snapshots(task)

            # 5. TraceEnd
            wall_time_s = time.monotonic() - self._wall_start
            model_time_raw = self._model_time_s
            tool_time_raw = self._tool_time_s
            # If subclass did not report model time, derive it
            if model_time_raw == 0.0 and wall_time_s > 0:
                model_time_raw = max(0.0, wall_time_s - tool_time_raw)
            # Compute other_time from raw values (before rounding),
            # then round each independently — matches old behavior.
            other_time_raw = max(0.0, wall_time_s - model_time_raw - tool_time_raw)
            model_time_s = round(model_time_raw, 2)
            tool_time_s = round(tool_time_raw, 2)
            other_time_s = round(other_time_raw, 2)

            input_tok = self._total_usage.input_tokens
            output_tok = self._total_usage.output_tokens
            writer.write_event(TraceEnd(
                trace_id=self._trace_id,
                total_turns=self._turn_count,
                model_input_tokens=input_tok,
                model_output_tokens=output_tok,
                input_tokens=input_tok,
                output_tokens=output_tok,
                total_tokens=input_tok + output_tok,
                model_time_s=model_time_s,
                tool_time_s=tool_time_s,
                other_time_s=other_time_s,
                wall_time_s=round(wall_time_s, 2),
                failure_modes=(
                    [loop_error] + self._failure_modes
                    if loop_error
                    else list(self._failure_modes)
                ),
            ))

            # Clean up writer reference
            self._writer = None  # type: ignore[assignment]

        # Re-raise so the caller can match on exception type
        if loop_exc is not None:
            raise loop_exc

        # Build OpenAI-format trace data from the completed JSONL trace so
        # Daily-Bench's _write_openai_trace keeps producing useful dumps.
        # System prompt is agent-specific — subclasses may set
        # self._system_prompt inside _execute() to have it prepended.
        openai_data: dict = {}
        try:
            from claw_anything.export.openai_format import convert_trace

            converted = convert_trace(
                trace_path,
                task,
                system_prompt=getattr(self, "_system_prompt", None),
            )
            if converted is not None:
                openai_data = converted
        except Exception as exc:  # stay non-fatal — export is best-effort
            print(f"[agent] openai trace export skipped: {exc}", flush=True)

        return trace_path, openai_data

    # ------------------------------------------------------------------ #
    #  Abstract method for subclasses                                     #
    # ------------------------------------------------------------------ #

    @abstractmethod
    def _execute(
        self,
        task: TaskDefinition,
        *,
        provider: OpenAICompatProvider,
        prompt_cfg: PromptConfig | None,
        model_cfg: ModelConfig | None,
        sandbox_tools: bool,
        sandbox_url: str | None,
        workspace_root: Path | None,
        content_blocks: list[ContentBlock],
    ) -> None:
        """Run the agent's core logic.

        Use ``emit_message()``, ``emit_tool_dispatch()``, etc. to report
        intermediate trace events back to the base class.
        """
        ...

    # ------------------------------------------------------------------ #
    #  Emit helpers (called by subclasses during _execute)                #
    # ------------------------------------------------------------------ #

    def emit_message(
        self,
        message: Message,
        usage: TokenUsage | None = None,
    ) -> None:
        """Write a TraceMessage and accumulate token usage."""
        self._writer.write_event(TraceMessage(
            trace_id=self._trace_id,
            message=message,
            usage=usage or TokenUsage(),
        ))
        if usage:
            self._total_usage.input_tokens += usage.input_tokens
            self._total_usage.output_tokens += usage.output_tokens

    def emit_tool_dispatch(self, dispatch: ToolDispatch) -> None:
        """Write a ToolDispatch event and accumulate tool time."""
        self._writer.write_event(dispatch)
        self._tool_time_s += dispatch.latency_ms / 1000.0

    def record_failure(self, msg: str) -> None:
        """Record a soft failure (e.g. timeout) without raising an exception.

        The message will appear in TraceEnd.failure_modes.
        """
        self._failure_modes.append(msg)

    # ------------------------------------------------------------------ #
    #  Timing / turn helpers                                              #
    # ------------------------------------------------------------------ #

    def add_model_time(self, seconds: float) -> None:
        """Accumulate model inference time (called by subclasses that can
        measure it directly, e.g. LoopAgent)."""
        self._model_time_s += seconds

    def increment_turn_count(self, n: int = 1) -> None:
        """Increment the turn counter."""
        self._turn_count += n

    # ------------------------------------------------------------------ #
    #  Audit snapshot fetching (shared implementation)                    #
    # ------------------------------------------------------------------ #

    def _fetch_audit_snapshots(self, task: TaskDefinition) -> None:
        """Fetch audit data from mock services (best-effort)."""
        import httpx

        for svc in task.services:
            if svc.reset_endpoint:
                audit_url = (
                    svc.reset_endpoint.rsplit("/reset", 1)[0] + "/audit"
                )
                try:
                    resp = httpx.get(audit_url, timeout=5)
                    self._writer.write_event(AuditSnapshot(
                        trace_id=self._trace_id,
                        service_name=svc.name,
                        audit_url=audit_url,
                        audit_data=resp.json(),
                    ))
                except Exception:
                    pass  # audit fetch is best-effort
