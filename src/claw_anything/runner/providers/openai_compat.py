"""OpenAI-compatible provider (GPT-4o, vLLM, MiMo, etc.)."""

from __future__ import annotations

import json
import os
import random
import re
import sys
import time
from uuid import uuid4
from typing import Any

import httpx
from openai import OpenAI

from ...llm_logger import log_llm_call
from ...models.content import TextBlock, ToolUseBlock
from ...models.message import Message
from ...models.tool import ToolSpec
from ...models.trace import TokenUsage


def _tool_spec_to_openai(spec: ToolSpec) -> dict[str, Any]:
    """Convert our ToolSpec to OpenAI function calling format."""
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.input_schema,
        },
    }


_TOOL_CALL_BLOCK_RE = re.compile(
    r"<tool_call>\s*(.*?)\s*</tool_call>",
    flags=re.IGNORECASE | re.DOTALL,
)
_FUNCTION_RE = re.compile(
    r"<function\s*=\s*([a-zA-Z0-9_:-]+)\s*>",
    flags=re.IGNORECASE,
)
_PARAM_RE = re.compile(
    r"<parameter\s*=\s*([a-zA-Z0-9_:-]+)\s*>(.*?)</parameter>",
    flags=re.IGNORECASE | re.DOTALL,
)
# Kimi-k2.5 native tool-call special-token format. The model defaults to this
# even when the system prompt asks for XML, because it is what it was trained on.
_KIMI_TOOL_CALL_RE = re.compile(
    r"<\|tool_call_begin\|>\s*functions\.([a-zA-Z0-9_-]+)\s*:\s*\d+\s*"
    r"<\|tool_call_argument_begin\|>\s*(\{.*?\})\s*<\|tool_call_end\|>",
    flags=re.DOTALL,
)
_KIMI_SECTION_RE = re.compile(
    r"<\|tool_calls_section_begin\|>.*?<\|tool_calls_section_end\|>",
    flags=re.DOTALL,
)
_KIMI_STRAY_TOKEN_RE = re.compile(r"<\|tool_call[^|]*\|>")


def _coerce_param_value(raw: str) -> Any:
    value = raw.strip()
    if not value:
        return ""

    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered == "null":
        return None

    if re.fullmatch(r"-?\d+", value):
        try:
            return int(value)
        except Exception:
            return value
    if re.fullmatch(r"-?\d+\.\d+", value):
        try:
            return float(value)
        except Exception:
            return value

    if (value.startswith("{") and value.endswith("}")) or (
        value.startswith("[") and value.endswith("]")
    ):
        try:
            return json.loads(value)
        except Exception:
            return value

    return value


def _extract_kimi_native_tool_calls(text: str) -> tuple[str, list[ToolUseBlock]]:
    """Parse kimi-k2.5's native <|tool_call_begin|>...<|tool_call_end|> format."""
    if "<|tool_call_begin|>" not in text:
        return text, []
    tool_uses: list[ToolUseBlock] = []
    for m in _KIMI_TOOL_CALL_RE.finditer(text):
        tool_name = m.group(1).strip()
        try:
            parsed_input = json.loads(m.group(2))
        except json.JSONDecodeError:
            parsed_input = {}
        if not isinstance(parsed_input, dict):
            parsed_input = {}
        tool_uses.append(ToolUseBlock(
            id=f"fallback_{uuid4().hex[:12]}",
            name=tool_name,
            input=parsed_input,
        ))
    if not tool_uses:
        return text, []
    cleaned = _KIMI_SECTION_RE.sub("", text)
    cleaned = _KIMI_STRAY_TOKEN_RE.sub("", cleaned).strip()
    return cleaned, tool_uses


def _extract_text_tool_calls(text: str) -> tuple[str, list[ToolUseBlock]]:
    """Parse pseudo tool-call markup from text as fallback.

    Tries kimi-k2.5's native special-token format first, then XML markup like:
    <tool_call>
      <function=todo_list_tasks>
      <parameter=status>all</parameter>
    </tool_call>
    """
    cleaned, tool_uses = _extract_kimi_native_tool_calls(text)
    if tool_uses:
        return cleaned, tool_uses

    tool_uses = []
    if "<tool_call" not in text.lower():
        return text, tool_uses

    matches = list(_TOOL_CALL_BLOCK_RE.finditer(text))
    if not matches:
        return text, tool_uses

    for m in matches:
        block = m.group(1)
        fn = _FUNCTION_RE.search(block)
        if not fn:
            continue

        tool_name = fn.group(1).strip()
        parsed_input: dict[str, Any] = {}
        for p in _PARAM_RE.finditer(block):
            key = p.group(1).strip()
            raw_val = p.group(2)
            parsed_input[key] = _coerce_param_value(raw_val)

        tool_uses.append(
            ToolUseBlock(
                id=f"fallback_{uuid4().hex[:12]}",
                name=tool_name,
                input=parsed_input,
            )
        )

    if not tool_uses:
        return text, tool_uses

    cleaned_text = _TOOL_CALL_BLOCK_RE.sub("", text).strip()
    return cleaned_text, tool_uses


def _blocks_to_openai_content(msg: Message) -> str:
    """Concatenate text blocks into a single string."""
    text_parts = [b.text for b in msg.content if b.type == "text"]
    return "\n".join(text_parts) if text_parts else ""


def _message_to_openai(msg: Message) -> dict[str, Any] | list[dict[str, Any]]:
    """Convert our Message to OpenAI chat format.

    Returns a single dict for simple messages, or a list of dicts
    when tool_result blocks need to be sent as separate tool messages.
    """
    # Tool result messages need special handling
    tool_results = [b for b in msg.content if b.type == "tool_result"]
    if tool_results:
        results = []
        for tr in tool_results:
            content_text = "\n".join(t.text for t in tr.content) if tr.content else ""
            results.append({
                "role": "tool",
                "tool_call_id": tr.tool_use_id,
                "content": content_text,
            })
        return results

    # Assistant messages with tool_use blocks
    tool_uses = [b for b in msg.content if b.type == "tool_use"]
    if tool_uses:
        d = {
            "role": "assistant",
            "content": _blocks_to_openai_content(msg),
            "tool_calls": [
                {
                    "id": tu.id,
                    "type": "function",
                    "function": {
                        "name": tu.name,
                        "arguments": json.dumps(tu.input),
                    },
                }
                for tu in tool_uses
            ],
        }
        if msg.reasoning_content:
            # Use "reasoning" for OpenRouter compatibility (also accepted as
            # "reasoning_content" by native DeepSeek/QwQ endpoints).
            d["reasoning"] = msg.reasoning_content
        return d

    # Simple text message
    d = {
        "role": msg.role,
        "content": _blocks_to_openai_content(msg),
    }
    if msg.reasoning_content:
        d["reasoning"] = msg.reasoning_content
    return d


class OpenAICompatProvider:
    """Calls any OpenAI-compatible chat completions endpoint."""

    def __init__(
        self,
        model_id: str = "gpt-4o",
        api_key: str | None = None,
        base_url: str | None = None,
        extra_body: dict | None = None,
        session_cache_enabled: bool = False,
        text_tool_call_mode: bool = False,
        tls_verify: bool = True,
    ) -> None:
        self.model_id = model_id
        self.extra_body = extra_body or {}
        self.session_cache_enabled = session_cache_enabled
        self.text_tool_call_mode = text_tool_call_mode
        self._api_base = base_url
        self._session_id: str | None = None
        resolved_key = api_key or os.environ.get("OPENAI_API_KEY") or "unused"
        timeout = httpx.Timeout(connect=30, read=300, write=30, pool=30)
        client_kwargs: dict[str, Any] = {
            "api_key": resolved_key,
            "base_url": base_url,
            "timeout": timeout,
            "max_retries": 0,  # we handle retries ourselves in chat()
        }
        if not tls_verify:
            client_kwargs["http_client"] = httpx.Client(verify=False, timeout=timeout)
        self.client = OpenAI(**client_kwargs)

    def set_session_id(self, session_id: str | None) -> None:
        """Attach X-Session-Id to subsequent chat() calls. No-op if disabled."""
        self._session_id = session_id if self.session_cache_enabled else None

    def release_session(self) -> None:
        """DELETE /session_release so the backend frees the KV-cache slot."""
        if not (self.session_cache_enabled and self._session_id and self._api_base):
            self._session_id = None
            return
        url = f"{self._api_base.rstrip('/')}/session_release"
        try:
            with httpx.Client(timeout=10) as c:
                c.delete(url, params={"session_id": self._session_id})
        except Exception as e:
            print(
                f"[warn] session release failed for {self._session_id}: {e}",
                file=sys.stderr,
            )
        finally:
            self._session_id = None

    def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        data_id: str | None = None,
    ) -> tuple[Message, TokenUsage]:
        """Send messages to the model and return parsed response."""
        # Build OpenAI messages list
        oai_messages: list[dict[str, Any]] = []
        for msg in messages:
            converted = _message_to_openai(msg)
            if isinstance(converted, list):
                oai_messages.extend(converted)
            else:
                oai_messages.append(converted)

        kwargs: dict[str, Any] = {
            "model": self.model_id,
            "messages": oai_messages,
            "temperature": 0.0,
        }
        if self.extra_body:
            kwargs["extra_body"] = dict(self.extra_body)
        if tools and not self.text_tool_call_mode:
            kwargs["tools"] = [_tool_spec_to_openai(t) for t in tools]
        if self._session_id:
            kwargs["extra_headers"] = {"X-Session-Id": self._session_id}

        max_retries = 5
        last_exc: Exception | None = None
        use_stream = False  # default: non-streaming
        _log_id = data_id or f"runner-{uuid4().hex[:12]}"
        for attempt in range(max_retries + 1):
            try:
                if attempt <= 1:
                    response = self._call_without_stream(kwargs)
                else:
                    response = self._call_with_stream(kwargs)
                # Log successful call
                log_llm_call(
                    data_id=_log_id,
                    model=self.model_id,
                    request_kwargs=kwargs,
                    response=response,
                    source="runner",
                )
                # Parse inside try so that empty-choices errors are retried
                return self._parse_response(response)
            except Exception as exc:
                last_exc = exc
                # Check if retryable (rate-limit or server error)
                status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
                exc_str = str(exc).lower()
                exc_type = type(exc).__name__.lower()
                retryable = (
                    status in (429, 500, 502, 503, 529)
                    or "timeout" in exc_str
                    or "connection" in exc_str
                    or "empty choices" in exc_str
                    or "remoteprotocol" in exc_type
                    or "remoteprotocol" in exc_str
                    or "peer closed" in exc_str
                    or "incomplete" in exc_str
                    or "server disconnected" in exc_str
                )
                if not retryable or attempt == max_retries:
                    raise
                # Non-streaming timeout → switch to streaming to keep connection alive
                if not use_stream and ("timeout" in exc_str or "read timed out" in exc_str) and attempt >= 1:
                    use_stream = True
                    print(f"[retry] Switching to streaming mode after repeated timeouts")
                # Exponential backoff with jitter
                delay = random.uniform(2, 4)
                print(f"[retry] API error ({status or type(exc).__name__}), "
                      f"attempt {attempt + 1}/{max_retries}, waiting {delay:.1f}s ...")
                time.sleep(delay)

        # All retries exhausted — log failure
        log_llm_call(
            data_id=_log_id,
            model=self.model_id,
            request_kwargs=kwargs,
            response=None,
            source="runner",
        )
        raise last_exc or RuntimeError("All retries exhausted")

    # ------------------------------------------------------------------
    # Non-streaming call (fallback for endpoints where streaming 502s)
    # ------------------------------------------------------------------

    def _call_without_stream(self, kwargs: dict[str, Any]) -> Any:
        """Call the API without streaming. Used as fallback when streaming fails."""
        return self.client.chat.completions.create(**kwargs)

    # ------------------------------------------------------------------
    # Streaming call + chunk assembly
    # ------------------------------------------------------------------

    def _call_with_stream(self, kwargs: dict[str, Any]) -> Any:
        """Call the API with streaming to avoid reverse-proxy read timeouts.

        Assembles a synthetic response object that looks like a non-stream
        response so the rest of the code path stays the same.
        """
        stream_kwargs: dict[str, Any] = {"stream": True}
        # stream_options is OpenAI-specific; Anthropic/Claude endpoints reject it.
        model_lower = kwargs.get("model", "").lower()
        is_anthropic = any(s in model_lower for s in ("claude", "anthropic"))
        if not is_anthropic:
            stream_kwargs["stream_options"] = {"include_usage": True}
        stream = self.client.chat.completions.create(**kwargs, **stream_kwargs)

        reasoning_parts: list[str] = []
        content_parts: list[str] = []
        tool_calls_by_index: dict[int, dict[str, Any]] = {}
        usage_info = None
        has_any_choice = False

        for chunk in stream:
            if chunk.usage:
                usage_info = chunk.usage
            if not chunk.choices:
                continue
            has_any_choice = True
            delta = chunk.choices[0].delta

            # reasoning_content (thinking models: DeepSeek-R1, QwQ, etc.)
            # OpenRouter returns "reasoning" instead of "reasoning_content"
            rc = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
            if rc:
                reasoning_parts.append(rc)

            # content
            if delta.content:
                content_parts.append(delta.content)

            # tool_calls (streamed incrementally)
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_calls_by_index:
                        tool_calls_by_index[idx] = {"id": "", "name": "", "arguments": ""}
                    if tc_delta.id:
                        tool_calls_by_index[idx]["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            tool_calls_by_index[idx]["name"] = tc_delta.function.name
                        if tc_delta.function.arguments:
                            tool_calls_by_index[idx]["arguments"] += tc_delta.function.arguments

        if not has_any_choice:
            raise RuntimeError("Model returned empty choices (choices=None or [])")

        # Build a lightweight object that _parse_response can consume
        # (duck-typed to match openai ChatCompletion structure).
        class _Msg:
            pass

        msg = _Msg()
        msg.content = "".join(content_parts) if content_parts else None
        msg.reasoning_content = "".join(reasoning_parts) if reasoning_parts else None

        if tool_calls_by_index:
            assembled = []
            for idx in sorted(tool_calls_by_index):
                tc = tool_calls_by_index[idx]

                class _Fn:
                    pass

                fn = _Fn()
                fn.name = tc["name"]
                fn.arguments = tc["arguments"]

                class _TC:
                    pass

                t = _TC()
                t.id = tc["id"]
                t.function = fn
                assembled.append(t)
            msg.tool_calls = assembled
        else:
            msg.tool_calls = None

        class _Choice:
            pass

        choice = _Choice()
        choice.message = msg

        class _Resp:
            pass

        resp = _Resp()
        resp.choices = [choice]
        resp.usage = usage_info
        return resp

    # ------------------------------------------------------------------
    # Response parsing (shared by stream / non-stream paths)
    # ------------------------------------------------------------------

    def _parse_response(self, response: Any) -> tuple[Message, TokenUsage]:
        """Parse a (possibly synthetic) ChatCompletion into Message + TokenUsage."""
        if not response.choices:
            raise RuntimeError("Model returned empty choices (choices=None or [])")
        choice = response.choices[0]

        # Parse response into our content blocks
        content_blocks = []
        if choice.message.content:
            if isinstance(choice.message.content, str):
                content_blocks.append(TextBlock(text=choice.message.content))
            elif isinstance(choice.message.content, list):
                text_chunks = []
                for part in choice.message.content:
                    if isinstance(part, dict):
                        part_type = part.get("type")
                        if part_type == "text":
                            text_val = part.get("text")
                            if isinstance(text_val, str):
                                text_chunks.append(text_val)
                        continue

                    part_type = getattr(part, "type", None)
                    if part_type == "text":
                        text_val = getattr(part, "text", None)
                        if isinstance(text_val, str):
                            text_chunks.append(text_val)
                if text_chunks:
                    content_blocks.append(TextBlock(text="\n".join(text_chunks)))

        parsed_tool_uses: list[ToolUseBlock] = []
        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                parsed_tool_uses.append(ToolUseBlock(
                    id=tc.id,
                    name=tc.function.name,
                    input=args,
                ))
            content_blocks.extend(parsed_tool_uses)
        else:
            # Fallback for providers that return pseudo tool markup in text
            # instead of native tool_calls payloads.
            text_blocks = [b for b in content_blocks if b.type == "text"]
            if text_blocks:
                merged_text = "\n".join(tb.text for tb in text_blocks)
                cleaned_text, fallback_tools = _extract_text_tool_calls(merged_text)
                if fallback_tools:
                    content_blocks = []
                    if cleaned_text:
                        content_blocks.append(TextBlock(text=cleaned_text))
                    content_blocks.extend(fallback_tools)

        usage = TokenUsage()
        if response.usage:
            usage = TokenUsage(
                input_tokens=response.usage.prompt_tokens,
                output_tokens=response.usage.completion_tokens,
            )

        # Capture reasoning_content from thinking models (DeepSeek-R1, QwQ, etc.)
        # OpenRouter returns "reasoning" instead of "reasoning_content"
        reasoning = getattr(choice.message, "reasoning_content", None) or getattr(choice.message, "reasoning", None)

        return Message(role="assistant", content=content_blocks, reasoning_content=reasoning), usage
