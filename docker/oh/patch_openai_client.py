"""Build-time patch for openharness-ai 0.1.9's OpenAI client.

Comments out the ``stream_options`` pop in ``OpenAIClient._stream_once`` so
per-turn usage chunks still come through when tools are present. The pop was
an upstream Kimi-specific workaround; re-enable if it breaks anything.

Exits non-zero (raises) if the matched line is gone after an openharness-ai
bump so the docker build fails loudly rather than silently leaving the bug.
"""
from __future__ import annotations

import pathlib

import openharness

PATH = pathlib.Path(openharness.__file__).parent / "api" / "openai_client.py"

OLD = '            params.pop("stream_options", None)'

NEW = (
    "            # Disabled to keep per-turn usage; re-enable if it breaks anything.\n"
    '            # params.pop("stream_options", None)\n'
    "            pass"
)


def main() -> None:
    src = PATH.read_text()
    if OLD not in src:
        raise SystemExit(
            f"stream_options pop line not found in {PATH}; "
            "Dockerfile.oh patch needs updating for the new upstream version"
        )
    PATH.write_text(src.replace(OLD, NEW))
    print(f"patched: {PATH}")


if __name__ == "__main__":
    main()
