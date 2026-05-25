"""sitecustomize for the claw-anything-oh image.

Python auto-imports ``sitecustomize`` on interpreter startup if it can find one
on the path (this file ships in site-packages via Dockerfile.oh). The patches
below stay no-ops unless their gating env var is set — running ``oh`` outside
claw-anything keeps stock behaviour.

Patches applied
---------------

1. ``CLAW_TASK_EXECUTION_DATE`` → override ``EnvironmentInfo.date``

   Vanilla ``openharness-ai`` builds its system prompt via
   ``openharness.prompts.environment.get_environment_info(...)``, which reads
   the wall clock. claw-anything tasks have a simulated ``execution_date``
   and need the model to see that, not real today.

   The OH-Ext fork solves this with a ``settings.prompt_meta.today`` field
   that vanilla lacks. Vanilla ``settings.system_prompt`` would work but
   ``build_system_prompt`` always appends another env section after the
   custom prompt, leaving the LLM with two ``# Environment`` blocks at
   different dates. Patching ``get_environment_info`` keeps OH's base
   prompt path intact and produces a single env section with the right
   date.

   Coupling: depends on ``openharness.prompts.environment.get_environment_info``
   keeping its name and signature. If a future upstream release renames or
   refactors it, the override silently no-ops (date falls back to wall clock).
"""

from __future__ import annotations

import os


def _patch_environment_date() -> None:
    """Override ``EnvironmentInfo.date`` from the ``CLAW_TASK_EXECUTION_DATE`` env var.

    No-op when the env var is unset or the OH module layout doesn't match.
    """
    override = os.environ.get("CLAW_TASK_EXECUTION_DATE")
    if not override:
        return
    try:
        from dataclasses import replace
        from openharness.prompts import environment as oh_env
    except ImportError:
        return
    original = getattr(oh_env, "get_environment_info", None)
    if original is None:
        return

    def _patched(cwd: str | None = None):  # type: ignore[no-untyped-def]
        info = original(cwd=cwd)
        try:
            return replace(info, date=override)
        except Exception:
            return info

    oh_env.get_environment_info = _patched


_patch_environment_date()
