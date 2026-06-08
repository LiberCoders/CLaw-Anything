"""Build-time patch for openharness-ai's system prompt assembly.

Lets ``CLAW_SYSTEM_PROMPT_SUFFIX`` (set per-trial by
``OpenHarnessAgent._build_oh_env``) be appended to OH's base system prompt.
claw-anything stages the user's work-history logs under ``/workspace/logs/``
inside the trial container; the suffix is an "Activity Logs" section that
advertises those absolute paths so the model reads them with its native
``read_file``. We APPEND (never replace) so OH's base instructions survive.

The env var is read **at call time** of ``build_system_prompt`` (not at
import), so per-trial isolation holds across trials sharing one interpreter.

Exits non-zero (raises) if the matched line is gone after an openharness-ai
bump, so the docker build fails loudly rather than silently dropping the
activity-log injection.
"""
from __future__ import annotations

import pathlib

import openharness

PATH = pathlib.Path(openharness.__file__).parent / "prompts" / "system_prompt.py"

OLD = '    return f"{base}\\n\\n{env_section}"'

NEW = (
    "    _claw_result = f\"{base}\\n\\n{env_section}\"\n"
    "    # CLAW: append per-trial activity-log suffix (set by\n"
    "    # OpenHarnessAgent._build_oh_env); appended, not replaced, so OH's\n"
    "    # base system prompt stays intact.\n"
    "    import os as _claw_os\n"
    '    _claw_suffix = _claw_os.environ.get("CLAW_SYSTEM_PROMPT_SUFFIX")\n'
    "    if _claw_suffix:\n"
    '        _claw_result = f"{_claw_result}\\n\\n{_claw_suffix}"\n'
    "    return _claw_result"
)


def main() -> None:
    src = PATH.read_text()
    if OLD not in src:
        raise SystemExit(
            f"build_system_prompt return line not found in {PATH}; "
            "Dockerfile.oh patch needs updating for the new upstream version"
        )
    PATH.write_text(src.replace(OLD, NEW))
    print(f"patched: {PATH}")


if __name__ == "__main__":
    main()
