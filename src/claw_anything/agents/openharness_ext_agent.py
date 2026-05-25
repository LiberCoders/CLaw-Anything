"""OpenHarnessExtAgent: runs OpenHarnessExtended (the OH-Ext fork) as a claw-anything agent.

Thin subclass of ``OpenHarnessAgent`` — the entire stream-json / dispatch.jsonl
pipeline, subprocess management, and trace emission live in the base class.
This file only customises:

  - ``LOG_PREFIX``: ``[oh-ext]`` so mixed logs distinguish the fork from vanilla.
  - ``builtin_tools_for_deny``: extends the upstream list with the 3 expert-*
    tools that OpenHarnessExtended adds in ``src/openharness/extended/tools/``.

The ``oh`` CLI invocation is identical between the two forks (same flags, same
stream-json schema), so the base class needs no other extension points. If the
fork adds new env vars or CLI flags later, override ``_build_oh_env`` /
``_run_oh_subprocess`` here.

The list of fork-only tools lives next to this class (``OH_EXT_BUILTIN_TOOLS``
below) rather than in ``openharness_plugin_gen``, so each agent owns the
knowledge of what's in its own toolbox.
"""

from __future__ import annotations

from .openharness_agent import OH_BUILTIN_TOOLS, OpenHarnessAgent


#: Tools that exist ONLY in OpenHarnessExtended (``src/openharness/extended/tools/``),
#: i.e. additions on top of vanilla openharness-ai. The vanilla OH agent never
#: includes these (denying a non-existent tool would leak the existence of the
#: OH-Ext extensions to the model).
OH_EXT_ONLY_BUILTIN_TOOLS: list[str] = [
    "delegate_to_expert",
    "get_expert_status",
    "send_to_expert",
]

#: Full builtin-tool set seen by the OH-Ext ``oh`` CLI — vanilla list plus the
#: fork's expert-* additions. Precomputed at module load so
#: ``builtin_tools_for_deny`` is a constant-time lookup.
OH_EXT_BUILTIN_TOOLS: list[str] = [*OH_BUILTIN_TOOLS, *OH_EXT_ONLY_BUILTIN_TOOLS]


class OpenHarnessExtAgent(OpenHarnessAgent):
    """OpenHarnessExtended variant — adds the fork's expert-* builtin tools.

    Construction signature is inherited unchanged
    (``settings_path``, ``disable_builtin_tools``).
    """

    LOG_PREFIX = "[oh-ext]"

    def builtin_tools_for_deny(self) -> list[str]:
        return OH_EXT_BUILTIN_TOOLS
