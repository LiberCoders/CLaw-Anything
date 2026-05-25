"""Agent implementations for task evaluation."""

from __future__ import annotations

import argparse
from types import SimpleNamespace

from .base import BaseAgent
from .loop import LoopAgent

__all__ = ["BaseAgent", "LoopAgent", "make_agent"]


def make_agent(
    cfg: 'claw_anything.config.Config',
    args: argparse.Namespace | SimpleNamespace,
    model_id: str,
) -> BaseAgent:
    """Factory function to create an agent based on configuration.

    Args:
        cfg: Global configuration
        args: Command-line arguments
        model_id: Model ID to use

    Returns:
        An instance of BaseAgent (or subclass)

    Raises:
        ValueError: If agent_type is unknown

    A ``--agent`` CLI flag (``args.agent``) overrides ``cfg.agent.agent_type``
    when present, so the agent backend is selectable per run.
    """
    agent_type = getattr(args, "agent", None) or cfg.agent.agent_type

    if agent_type in ("loop", "openai-compat"):
        return LoopAgent()
    elif agent_type == "openharness":
        # Lazy import — vanilla openharness-ai (the ``oh`` CLI) need not be
        # installed unless this backend is actually selected. Model / base-url
        # / API key come from the OH settings file, not from claw-anything config.
        from .openharness_agent import OpenHarnessAgent

        return OpenHarnessAgent(
            settings_path=getattr(args, "oh_settings", None),
            disable_builtin_tools=getattr(args, "oh_disable_builtin_tools", False),
        )
    elif agent_type == "openharness-ext":
        # Lazy import — OpenHarnessExtended (the OH-Ext fork) need not be
        # installed unless this backend is actually selected.
        from .openharness_ext_agent import OpenHarnessExtAgent

        return OpenHarnessExtAgent(
            settings_path=getattr(args, "oh_settings", None),
            disable_builtin_tools=getattr(args, "oh_disable_builtin_tools", False),
        )
    else:
        raise ValueError(f"Unknown agent_type: {agent_type}")
