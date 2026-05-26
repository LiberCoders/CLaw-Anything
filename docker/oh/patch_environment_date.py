"""Build-time patch for openharness-ai 0.1.9's environment date.

Lets ``CLAW_TASK_EXECUTION_DATE`` (set per-trial by ``OpenHarnessAgent._build_oh_env``)
override the date that lands in the system prompt's ``# Environment`` block.
Without this, OH always reports the container's wall clock and the model
ignores the task's simulated date.

The env var is read **at call time** of ``get_environment_info`` (not at
import), so per-trial isolation is preserved across trials sharing the same
interpreter.

Exits non-zero (raises) if the matched line is gone after an openharness-ai
bump so the docker build fails loudly rather than silently regressing the
date override.
"""
from __future__ import annotations

import pathlib

import openharness

PATH = pathlib.Path(openharness.__file__).parent / "prompts" / "environment.py"

OLD = '        date=datetime.now(tz=timezone.utc).strftime("%Y-%m-%d"),'

NEW = (
    "        # CLAW: per-trial override via env var; falls back to wall clock.\n"
    '        date=os.environ.get("CLAW_TASK_EXECUTION_DATE") or datetime.now(tz=timezone.utc).strftime("%Y-%m-%d"),'
)


def main() -> None:
    src = PATH.read_text()
    if OLD not in src:
        raise SystemExit(
            f"EnvironmentInfo date line not found in {PATH}; "
            "Dockerfile.oh patch needs updating for the new upstream version"
        )
    PATH.write_text(src.replace(OLD, NEW))
    print(f"patched: {PATH}")


if __name__ == "__main__":
    main()
