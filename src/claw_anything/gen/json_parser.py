"""Unified LLM JSON response parser with robust fallback handling.

Handles common LLM output quirks:
- <think>...</think> blocks (DeepSeek, Qwen reasoning models)
- Markdown ```json ... ``` fences
- Preamble/postamble text around JSON
- Trailing commas, unescaped newlines in string values
- response_format compatibility detection
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# response_format compatibility
# ---------------------------------------------------------------------------

_NO_JSON_MODE_PATTERNS = [
    r"deepseek",
    r"qwen.*think",
    r"qwq",
    r"glm",
    r"yi-",
]


def should_use_response_format(model_id: str) -> bool:
    """Return True if the model likely supports ``response_format: json_object``.

    Override with env var ``CLAW_JSON_MODE``: ``"0"`` to disable, ``"1"`` to force.
    """
    override = os.environ.get("CLAW_JSON_MODE")
    if override == "0":
        return False
    if override == "1":
        return True
    model_lower = model_id.lower()
    for pattern in _NO_JSON_MODE_PATTERNS:
        if re.search(pattern, model_lower):
            return False
    return True


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _strip_thinking(raw: str) -> str:
    """Remove ``<think>...</think>`` blocks and stray ``</think>`` prefixes."""
    # Full think blocks (greedy within each block)
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    # Stray closing tag (some models emit only </think> at the start)
    cleaned = re.sub(r"^.*?</think>\s*", "", cleaned, flags=re.DOTALL)
    return cleaned.strip()


def _try_parse(text: str) -> dict | list | None:
    """Attempt ``json.loads``; return *None* on failure."""
    try:
        result = json.loads(text)
        if isinstance(result, (dict, list)):
            return result
    except (json.JSONDecodeError, TypeError):
        pass
    return None


def _extract_fenced(text: str) -> str | None:
    """Extract content from markdown code fences."""
    m = re.search(r"```(?:json)?\s*\n?(.*?)\s*```", text, re.DOTALL)
    return m.group(1) if m else None


def _extract_braces(text: str, open_ch: str = "{", close_ch: str = "}") -> str | None:
    """Extract substring from first *open_ch* to last *close_ch*."""
    first = text.find(open_ch)
    last = text.rfind(close_ch)
    if first != -1 and last > first:
        return text[first : last + 1]
    return None


def _repair_json(text: str) -> str:
    """Best-effort repair of common LLM JSON syntax issues."""
    # Trailing commas before } or ]
    text = re.sub(r",\s*([}\]])", r"\1", text)
    # Single-quoted strings → double-quoted (simple heuristic)
    # Only if the text has no double-quoted strings at all
    if '"' not in text and "'" in text:
        text = text.replace("'", '"')
    return text


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_llm_json(
    raw: str | None,
    default: Any = None,
    *,
    source: str = "",
) -> Any:
    """Parse a JSON dict/list from an LLM response with layered fallbacks.

    Parameters
    ----------
    raw:
        Raw LLM response text.
    default:
        Value returned when all parse attempts fail.  If *None*, returns ``{}``.
    source:
        Label for log messages (e.g. ``"persona_builder"``).
    """
    if default is None:
        default = {}

    tag = f"[{source}] " if source else ""

    if not raw or not raw.strip():
        log.warning("json_parser %sempty LLM response", tag)
        return default

    # Step 1: strip thinking blocks
    cleaned = _strip_thinking(raw)
    if not cleaned:
        log.warning("json_parser %sresponse was only thinking blocks", tag)
        return default

    # Step 2: direct parse
    result = _try_parse(cleaned.strip())
    if result is not None:
        return result

    # Step 3: markdown fence
    fenced = _extract_fenced(cleaned)
    if fenced is not None:
        result = _try_parse(fenced)
        if result is not None:
            log.debug("json_parser %sextracted from markdown fence", tag)
            return result

    # Step 4: brace/bracket extraction
    for open_ch, close_ch in [("{", "}"), ("[", "]")]:
        extracted = _extract_braces(cleaned, open_ch, close_ch)
        if extracted is not None:
            result = _try_parse(extracted)
            if result is not None:
                log.debug("json_parser %sextracted via brace matching", tag)
                return result

    # Step 5: repair + retry steps 2-4
    for candidate in filter(None, [cleaned.strip(), fenced,
                                    _extract_braces(cleaned, "{", "}"),
                                    _extract_braces(cleaned, "[", "]")]):
        repaired = _repair_json(candidate)
        result = _try_parse(repaired)
        if result is not None:
            log.info("json_parser %sparsed after JSON repair", tag)
            return result

    # Step 6: give up
    log.error("json_parser %sall parse attempts failed. Raw (first 500 chars): %s",
              tag, raw[:500])
    return default
