"""Seed Task: data model, loading, and persona matching for seed task library."""

from __future__ import annotations

import logging
import random
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from .persona import PersonaDefinition

log = logging.getLogger(__name__)


# Content axis — what cognitive operation the task demands.
TASK_CONTENT_DESCRIPTIONS: dict[str, str] = {
    "info_triage":
        "Information Triage & Routing — filter a large set of noisy signals "
        "(emails, notifications, requests), distinguish important from noise, "
        "categorize, prioritize, and route.",
    "info_aggregation":
        "Information Aggregation & Digest — collect information from multiple "
        "sources (docs, emails, notes, web, kb) and synthesize into a structured "
        "digest, report, or briefing.",
    "scheduling":
        "Scheduling & Time Coordination — handle calendars, time-slot conflicts, "
        "meeting preparation, multi-party coordination across time windows, or "
        "cron/DAG-style temporal ordering.",
    "multi_d_tradeoff":
        "Multi-dimensional Tradeoff — choose among options with multiple "
        "competing criteria (cost vs time, social vs personal capital, risk vs "
        "reward). Requires Pareto-style reasoning under soft preferences.",
    "constraint_solving":
        "Constraint Satisfaction & Combinatorial — solve under hard constraints "
        "(resource caps, thresholds, quotas, combinatorial puzzles such as "
        "discount stacking, shift assignment, DAG ordering, port contention).",
    "conflict_detection":
        "Conflict Detection & Belief Revision — identify hidden contradictions, "
        "information asymmetry, role conflicts, or revise prior commitments "
        "when new evidence contradicts earlier plans.",
    "risk_safety":
        "Risk & Safety Interception — detect red-line violations, supply chain "
        "risks, system anomalies, or irreversible actions; the agent must "
        "intercept, escalate, or refuse.",
    "workflow_execution":
        "Multi-step Workflow Execution — execute a concrete cross-service "
        "workflow to produce an artifact (expense report, complaint resolution, "
        "reconciliation, demo prep).",
    "proactive_patrol":
        "Proactive Patrol & Anomaly Discovery — with no explicit user prompt, "
        "discover anomalies or latent intent in the user's behavior logs "
        "(hesitation, abandoned plans, repeated fruitless searches).",
}

TASK_CONTENT_VALUES: tuple[str, ...] = tuple(TASK_CONTENT_DESCRIPTIONS.keys())


# Interaction axis — how the user triggers the task.
INTERACTION_MODE_DESCRIPTIONS: dict[str, str] = {
    "standard":
        "Reactive task — the user gives an explicit instruction and the agent "
        "executes, decides, or produces the requested artifact.",
    "patrol":
        "Proactive discovery — no explicit instruction. Agent must scan "
        "activity logs for behavioral anomalies and surface them with context.",
    "overview":
        "Proactive daily overview — agent surfaces a simple list of pending "
        "items (today's todos, upcoming calendar, important unread messages).",
    "patrol_setup":
        "Internal-only. Seed reserved for planting behavioral foreshadowing "
        "during persona build; never sampled as a direct eval task.",
}

INTERACTION_MODE_VALUES: tuple[str, ...] = tuple(INTERACTION_MODE_DESCRIPTIONS.keys())


def format_content_catalog() -> str:
    """Render the full content-category catalog as a prompt fragment."""
    return "\n".join(
        f"- `{name}`: {desc}"
        for name, desc in TASK_CONTENT_DESCRIPTIONS.items()
    )


def format_mode_catalog() -> str:
    """Render the full interaction-mode catalog as a prompt fragment."""
    return "\n".join(
        f"- `{name}`: {desc}"
        for name, desc in INTERACTION_MODE_DESCRIPTIONS.items()
    )


# CLI-side pseudo-services that are always available regardless of template whitelist.
# They represent the agent's inherent file/log access, not mock API services.
CLI_VIRTUAL_SERVICES: frozenset[str] = frozenset({"filesystem", "local_notes", "logs"})


class SeedTask(BaseModel):
    """A hand-written seed task that can be adapted to different personas."""

    seed_id: str
    name: str
    category: str
    # New taxonomy axes (see TASK_CONTENT_DESCRIPTIONS / INTERACTION_MODE_DESCRIPTIONS).
    task_content: str = "workflow_execution"
    interaction_mode: str = "standard"
    # Floating-content seeds (M-series) list alternative surface domains here.
    # At gen_eval time, one entry is sampled as the target content direction.
    # Empty => fall back to single-binding task_content (S/P-series).
    plausible_content_directions: list[str] = Field(default_factory=list)
    difficulty: str = "medium"  # simple | medium | hard
    description: str = ""

    required_services: list[str] = Field(default_factory=list)
    optional_services: list[str] = Field(default_factory=list)

    key_actions: list[str] = Field(default_factory=list)
    safety_concerns: list[str] = Field(default_factory=list)
    adaptable_elements: list[str] = Field(default_factory=list)

    applicable_roles: list[str] = Field(default_factory=list)
    inapplicable_roles: list[str] = Field(default_factory=list)

    @property
    def task_content_description(self) -> str:
        return TASK_CONTENT_DESCRIPTIONS.get(self.task_content, self.task_content)

    @property
    def interaction_mode_description(self) -> str:
        return INTERACTION_MODE_DESCRIPTIONS.get(self.interaction_mode, self.interaction_mode)

    @classmethod
    def from_yaml(cls, path: str | Path) -> SeedTask:
        path = Path(path)
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return cls(**data)


class SeedTaskLibrary:
    """Manages a collection of seed tasks and provides matching/sampling."""

    def __init__(self, seed_dir: str | Path):
        self.seed_dir = Path(seed_dir).resolve()
        self.tasks: list[SeedTask] = []
        self._used_ids: set[str] = set()
        # When set, match_persona will reject seeds whose required_services
        # include any service outside this whitelist. None = no constraint.
        self._allowed_services: set[str] | None = None
        self._load_all()

    def set_allowed_services(self, allowed: set[str] | None) -> None:
        """Restrict matching to seeds whose required_services subset ⊆ allowed.

        Pass None to clear the constraint.
        """
        self._allowed_services = set(allowed) if allowed else None

    def _load_all(self) -> None:
        """Load all .yaml files from seed_dir."""
        if not self.seed_dir.exists():
            log.warning("Seed tasks directory not found: %s", self.seed_dir)
            return
        yaml_files = sorted(self.seed_dir.glob("*.yaml"))
        for f in yaml_files:
            try:
                task = SeedTask.from_yaml(f)
                self.tasks.append(task)
            except Exception as e:
                log.warning("Failed to load seed task %s: %s", f.name, e)
        log.info("Loaded %d seed tasks from %s", len(self.tasks), self.seed_dir)

    def match_persona(self, seed: SeedTask, persona: PersonaDefinition) -> bool:
        """Check if a seed task is compatible with a persona.

        Rules:
        1. Persona role must not be in inapplicable_roles
        2. Seed category must not be in persona's implausible_categories
        3. If applicable_roles is set, persona role must match one
        4. If a template service whitelist is active, every service the seed
           lists in required_services must be inside the whitelist — otherwise
           the resulting task could not run against that template.
        """
        # Check inapplicable roles
        if seed.inapplicable_roles:
            for role in seed.inapplicable_roles:
                if role in persona.role:
                    return False

        # Check implausible categories
        if seed.category in persona.task_constraints.implausible_categories:
            return False

        # Check applicable roles (if specified, at least one must match)
        if seed.applicable_roles:
            matched = any(role in persona.role for role in seed.applicable_roles)
            if not matched:
                # Also check plausible_categories as fallback
                if seed.category not in persona.task_constraints.plausible_categories:
                    return False

        # Enforce template service whitelist on required_services.
        # CLI_VIRTUAL_SERVICES (filesystem, local_notes, logs) are always allowed
        # because they represent the agent's inherent CLI capabilities, not mock services.
        if self._allowed_services is not None and seed.required_services:
            missing = [
                s for s in seed.required_services
                if s not in self._allowed_services and s not in CLI_VIRTUAL_SERVICES
            ]
            if missing:
                return False

        return True

    def sample(
        self,
        persona: PersonaDefinition,
        n: int = 1,
        exclude_ids: set[str] | None = None,
    ) -> list[SeedTask]:
        """Sample n compatible seed tasks for a persona.

        Args:
            persona: Target persona to match against.
            n: Number of tasks to sample.
            exclude_ids: Seed IDs to exclude (already used).

        Returns:
            List of compatible seed tasks (may be fewer than n if not enough match).
        """
        exclude = (exclude_ids or set()) | self._used_ids
        candidates = [
            t for t in self.tasks
            if t.seed_id not in exclude
            and self.match_persona(t, persona)
        ]
        if not candidates:
            log.warning("No compatible seed tasks found for persona %s", persona.persona_name)
            return []

        k = min(n, len(candidates))
        return random.sample(candidates, k)

    def mark_used(self, seed_id: str) -> None:
        """Mark a seed task as used."""
        self._used_ids.add(seed_id)

    def reset_used(self) -> None:
        """Reset usage tracking."""
        self._used_ids.clear()

    def get_by_id(self, seed_id: str) -> SeedTask | None:
        """Retrieve a seed task by ID."""
        for t in self.tasks:
            if t.seed_id == seed_id:
                return t
        return None

    @property
    def available_count(self) -> int:
        return len(self.tasks) - len(self._used_ids)
