"""LLM interaction logger — saves complete request/response payloads to JSONL files.

Each record follows the schema:
    data_id, timestamp, model, request_type, request, response
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_DEFAULT_LOG_DIR = Path(__file__).resolve().parent.parent.parent / "llm_logs"

_LOG_DIR: Path | None = None
_lock = threading.Lock()
_file_locks: dict[str, threading.Lock] = {}


def _get_log_dir() -> Path:
    global _LOG_DIR
    if _LOG_DIR is None:
        env_dir = os.environ.get("CLAW_ANYTHING_LLM_LOG_DIR")
        _LOG_DIR = Path(env_dir) if env_dir else _DEFAULT_LOG_DIR
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    return _LOG_DIR


def _get_file_lock(source: str) -> threading.Lock:
    with _lock:
        if source not in _file_locks:
            _file_locks[source] = threading.Lock()
        return _file_locks[source]


def serialize_response(response: Any) -> dict | None:
    """Serialize an OpenAI SDK response object (or duck-typed streaming response) to dict."""
    if response is None:
        return None

    # Standard OpenAI SDK objects have model_dump()
    if hasattr(response, "model_dump"):
        try:
            return response.model_dump()
        except Exception:
            pass

    # Duck-typed streaming response assembled in _call_with_stream
    try:
        choices = []
        for choice in (response.choices or []):
            msg = choice.message
            tool_calls_list = None
            if msg.tool_calls:
                tool_calls_list = []
                for tc in msg.tool_calls:
                    tool_calls_list.append({
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    })
            choices.append({
                "message": {
                    "content": msg.content,
                    "tool_calls": tool_calls_list,
                    "reasoning_content": getattr(msg, "reasoning_content", None),
                },
            })

        usage_dict = None
        if response.usage:
            if hasattr(response.usage, "model_dump"):
                usage_dict = response.usage.model_dump()
            elif hasattr(response.usage, "prompt_tokens"):
                usage_dict = {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                }
        return {"choices": choices, "usage": usage_dict}
    except Exception as exc:
        log.debug("Failed to serialize response: %s", exc)
        return {"_serialization_error": str(exc)}


def log_llm_call(
    *,
    data_id: str,
    model: str,
    request_kwargs: dict[str, Any],
    response: Any,
    source: str = "default",
    request_type: str = "messages",
) -> None:
    """Write one LLM call record to the appropriate JSONL file.

    Args:
        data_id: Globally unique ID for this call.
        model: Model name as passed to the API.
        request_kwargs: The kwargs dict passed to chat.completions.create().
        response: The raw response object (OpenAI SDK or duck-typed), or None on failure.
        source: Log file name prefix (e.g. "runner", "judge", "gen_task").
        request_type: API endpoint type, typically "messages".
    """
    try:
        # Build request dict — always include messages
        request_dict = {}
        for key, value in request_kwargs.items():
            if key == "messages":
                request_dict["messages"] = value
            elif key in ("model",):
                continue  # model is stored at top level
            else:
                # Serialize non-trivial objects
                try:
                    json.dumps(value)
                    request_dict[key] = value
                except (TypeError, ValueError):
                    request_dict[key] = str(value)

        record = {
            "data_id": data_id,
            "timestamp": datetime.now().isoformat(),
            "model": model,
            "request_type": request_type,
            "request": request_dict,
            "response": serialize_response(response),
        }

        log_dir = _get_log_dir()
        log_file = log_dir / f"{source}.jsonl"

        line = json.dumps(record, ensure_ascii=False) + "\n"
        file_lock = _get_file_lock(source)
        with file_lock:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(line)

    except Exception as exc:
        log.warning("Failed to log LLM call (data_id=%s): %s", data_id, exc)
