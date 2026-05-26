"""Build-time patch for openharness-ai 0.1.9's ``run_print_mode``.

Adds a ``usage`` field to every ``assistant_complete`` event emitted on the
``stream-json`` output format, so claw-anything's OH adapter can report real
per-turn token counts instead of 0/0. Vanilla 0.1.9 drops the field on the
floor; OH-Ext emits it natively via ``PrintModeSettings.stream_json_extra_fields``.

Exits non-zero (raises) if the matched line is gone after an openharness-ai
bump so the docker build fails loudly rather than silently regressing token
accounting in downstream traces.
"""
from __future__ import annotations

import pathlib

import openharness

PATH = pathlib.Path(openharness.__file__).parent / "ui" / "app.py"

OLD = '                    obj = {"type": "assistant_complete", "text": event.message.text.strip()}'

NEW = (
    "                    # CLAW: add usage so OH adapter can report per-turn tokens.\n"
    "                    usage = getattr(event, \"usage\", None)\n"
    "                    obj = {\n"
    '                        "type": "assistant_complete",\n'
    "                        \"text\": event.message.text.strip(),\n"
    '                        "usage": {\n'
    '                            "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),\n'
    '                            "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),\n'
    "                        },\n"
    "                    }"
)


def main() -> None:
    src = PATH.read_text()
    if OLD not in src:
        raise SystemExit(
            f"assistant_complete obj line not found in {PATH}; "
            "Dockerfile.oh patch needs updating for the new upstream version"
        )
    PATH.write_text(src.replace(OLD, NEW))
    print(f"patched: {PATH}")


if __name__ == "__main__":
    main()
