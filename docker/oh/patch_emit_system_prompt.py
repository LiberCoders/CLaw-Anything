"""Build-time patch for openharness-ai's ``run_print_mode``.

Emits the final runtime system prompt as a one-shot
``{"type": "system_prompt", "text": ...}`` event on the ``stream-json`` output
format, right after the runtime is built. claw-anything's OH adapter captures
it onto ``self._system_prompt`` so the OpenAI-format trace export carries a
faithful system row — OH never sets that attribute otherwise, so without this
the exported ``*.openai.json`` has no ``system`` message.

The prompt is read from ``bundle.engine.system_prompt`` — the true prompt sent
to the model (base + environment + activity-log suffix + skills/memory).

Works for both vanilla openharness-ai (pip) and OH-Ext: both define
``run_print_mode`` with ``await start_runtime(bundle)`` and expose the
``QueryEngine.system_prompt`` property. That anchor line also appears in the
interactive path, so we splice out only the ``run_print_mode`` function body
and patch the first occurrence inside it — never the interactive one.

Exits non-zero (raises) if ``run_print_mode`` or its anchor is gone after an
openharness-ai bump, so the docker build fails loudly rather than silently
dropping the system row.
"""
from __future__ import annotations

import pathlib
import re

import openharness

PATH = pathlib.Path(openharness.__file__).parent / "ui" / "app.py"

ANCHOR = "    await start_runtime(bundle)\n"

# Already-patched marker (idempotency / double-build guard).
MARKER = '"type": "system_prompt"'

INJECT = (
    "    await start_runtime(bundle)\n"
    "    # CLAW: emit the final runtime system prompt once so the host adapter\n"
    "    # can record it on self._system_prompt for OpenAI-format trace export.\n"
    "    if output_format == \"stream-json\":\n"
    "        _claw_sp = getattr(bundle.engine, \"system_prompt\", None)\n"
    "        if _claw_sp:\n"
    "            print(json.dumps({\"type\": \"system_prompt\", \"text\": _claw_sp}), flush=True)\n"
)


def _slice_run_print_mode(src: str) -> tuple[int, int]:
    """Return (start, end) char offsets of the ``run_print_mode`` body.

    ``end`` is the next column-0 ``def``/``async def`` (nested helpers are
    indented and don't match) or EOF.
    """
    m = re.search(r"^async def run_print_mode\(", src, re.MULTILINE)
    if m is None:
        raise SystemExit(
            f"run_print_mode not found in {PATH}; "
            "Dockerfile patch needs updating for the new openharness-ai version"
        )
    nxt = re.search(r"^(?:async def |def )", src[m.end():], re.MULTILINE)
    end = m.end() + nxt.start() if nxt else len(src)
    return m.start(), end


def main() -> None:
    src = PATH.read_text()
    start, end = _slice_run_print_mode(src)
    body = src[start:end]
    if MARKER in body:
        print(f"already patched: {PATH}")
        return
    if ANCHOR not in body:
        raise SystemExit(
            f"`await start_runtime(bundle)` not found in run_print_mode of {PATH}; "
            "Dockerfile patch needs updating for the new openharness-ai version"
        )
    patched_body = body.replace(ANCHOR, INJECT, 1)
    PATH.write_text(src[:start] + patched_body + src[end:])
    print(f"patched: {PATH}")


if __name__ == "__main__":
    main()
