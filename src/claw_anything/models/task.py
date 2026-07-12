"""TaskDefinition — loaded from YAML task files (v3 aligned)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .tool import ToolEndpoint, ToolSpec


class Prompt(BaseModel):
    text: str
    language: str = "zh"


class DeterministicCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    field: str | None = None
    tool_name: str | None = None
    min_calls: int | None = None
    categories: list[str] | None = None
    min_length: int | None = None
    patterns: list[str] | None = None
    keywords: list[str] | None = None
    description: str | None = None
    rubric: str | None = None

    @field_validator("keywords", mode="before")
    @classmethod
    def _coerce_keywords_to_str(cls, v: Any) -> list[str] | None:
        """YAML parses unquoted numbers as ints; coerce to str."""
        if v is None:
            return v
        return [str(item) for item in v]


class ScoringComponent(BaseModel):
    name: str
    weight: float
    check: DeterministicCheck


class SafetyCheck(BaseModel):
    type: str
    tool_name: str | None = None
    patterns: list[str] | None = None
    description: str = ""


class Environment(BaseModel):
    timeout_seconds: int = 1200
    max_turns: int = 100
    fixtures: list[str] = Field(default_factory=list)


class ServiceDef(BaseModel):
    """A mock service that must be running for a task."""

    name: str
    command: str
    port: int
    health_check: str
    health_check_method: str = "POST"
    ready_timeout: int = 30
    reset_endpoint: str | None = None
    env: dict[str, str] = Field(default_factory=dict)


class ExpectedAction(BaseModel):
    """Describes an action the agent is expected to perform."""

    service: str  # "gmail", "calendar", etc.
    action_key: str  # key in /audit response: "drafts", "created_events", etc.
    required: bool = True


class ExpectedEffect(BaseModel):
    """A gold end-state assertion: a mutation that must have LANDED in a service's
    /audit log for the task to count as done.

    Unlike the legacy free-text ``ExpectedAction``, ``action_key`` here must be a
    real audit key (see ``graders.base.ACTION_KEYS``) and ``match`` pins the
    right recipient/value/record so a write to the wrong target does not pass.
    """

    service: str
    action_key: str
    match: dict[str, Any] = Field(default_factory=dict)
    required: bool = True
    weight: float = 1.0
    claim_phrases: list[str] = Field(default_factory=list)


# ======================================================================
# Rule-template models (RuleEvaluator backend, filled by LLM at gen-eval time)
# ----------------------------------------------------------------------
# Four deterministic checks the grader runs BEFORE the LLM judge:
#   1. ToolUsage      — must_call / call_order
#   2. GroundingEntityRule — anti-hallucination
#   3. forbidden_tool — list[str] on TaskDefinition (no nested model needed)
#   4. ValueInReplyRule    — must_contain / must_not_contain in final text
# ======================================================================


class MustCallRule(BaseModel):
    """A tool the agent is required to invoke.

    ``args_match`` constrains the *arguments* of the call (case-insensitive
    substring per field; list-valued → any-of). ``success`` (default True)
    requires the call to return < 400. ``min_count`` allows requiring multiple
    invocations (e.g. "must read at least 2 KB articles").
    """

    tool: str
    args_match: dict[str, Any] = Field(default_factory=dict)
    success: bool = True
    min_count: int = 1


class ToolUsage(BaseModel):
    """Tool-usage check: which tools must / must-not be called, and in what order."""

    must_call: list[MustCallRule] = Field(default_factory=list)
    call_order: list[list[str]] = Field(default_factory=list)


class GroundingEntityRule(BaseModel):
    """Anti-hallucination check.

    Every entity matching one of ``patterns`` extracted from the agent's final
    text must appear in some successful read-tool response (or audit ``calls``
    payload). ``threshold`` is the minimum supported/asserted ratio for pass.
    """

    patterns: list[str] = Field(default_factory=list)
    threshold: float = 0.8


class ValueInReplyRule(BaseModel):
    """Final-reply keyword check.

    ``must_contain`` is AND-of-OR: every outer list element must have at least
    one inner alternative present in the final reply (case-insensitive substring).
    ``must_not_contain`` rejects on any inner alternative matching.
    """

    must_contain: list[list[str]] = Field(default_factory=list)
    must_not_contain: list[list[str]] = Field(default_factory=list)


# ======================================================================
# Answer-sheet models (unified scoring: objective + subjective items)
# ======================================================================


class AnswerSheetItem(BaseModel):
    """One row on the answer sheet — filled at grade time, scored by rule or judge.

    ``kind`` distinguishes objective (rule-scored) vs subjective (LLM-judge-scored).
    ``fill`` is how the value is populated: ``rule`` from dispatches/audit,
    ``llm_extract`` from a batched LLM pass over the trace transcript.
    ``scorer`` selects the scoring backend once filled.
    """

    id: str
    kind: str = "objective"  # objective | subjective
    fill: str = "rule"  # rule | llm_extract
    scorer: str = "enum_match"
    weight: float = 1.0
    label: str = ""
    question: str = ""
    options: list[str] = Field(default_factory=list)
    expected: Any = None  # list[str] for enum_match OR; str for subjective gold
    rubric: str = ""
    rule: dict[str, Any] = Field(default_factory=dict)


class AnswerSheet(BaseModel):
    """Declarative answer sheet written into task.yaml at gen-eval time."""

    items: list[AnswerSheetItem] = Field(default_factory=list)


class TaskDefinition(BaseModel):
    task_id: str
    task_name: str
    version: str = "1.0"
    category: str = ""
    difficulty: str = "simple"
    execution_date: str | None = None
    prompt: Prompt
    tools: list[ToolSpec] = Field(default_factory=list)
    tool_endpoints: list[ToolEndpoint] = Field(default_factory=list)
    environment: Environment = Field(default_factory=Environment)
    scoring_components: list[ScoringComponent] = Field(default_factory=list)
    safety_checks: list[SafetyCheck] = Field(default_factory=list)
    services: list[ServiceDef] = Field(default_factory=list)
    expected_actions: list[ExpectedAction] = Field(default_factory=list)
    expected_effects: list[ExpectedEffect] = Field(default_factory=list)
    # Rule-template fields (RuleEvaluator backend). All optional, default empty;
    # tasks generated before rule templates were introduced simply skip the
    # rule layer and fall back to the legacy completion formula.
    tool_usage: ToolUsage = Field(default_factory=ToolUsage)
    grounding_entity: GroundingEntityRule = Field(default_factory=GroundingEntityRule)
    forbidden_tool: list[str] = Field(default_factory=list)
    value_in_reply: ValueInReplyRule = Field(default_factory=ValueInReplyRule)
    answer_sheet: AnswerSheet = Field(default_factory=AnswerSheet)
    task_env: list[str] = Field(default_factory=list)
    apps: list[dict] = Field(default_factory=list)
    judge_rubric: str = ""
    reference_solution: str = ""
    primary_dimensions: list[str] = Field(default_factory=list)
    sandbox_files: list[str] = Field(default_factory=list)
    sandbox_grader_files: list[str] = Field(default_factory=list)
    task_file: str | None = Field(default=None, exclude=True)

    @classmethod
    def from_yaml(cls, path: str | Path) -> TaskDefinition:
        with open(path) as f:
            data = yaml.safe_load(f)
        data["task_file"] = str(Path(path).resolve())
        return cls.model_validate(data)

    def get_endpoint_map(self) -> dict[str, ToolEndpoint]:
        """Return {tool_name: ToolEndpoint} for dispatcher lookup."""
        return {ep.tool_name: ep for ep in self.tool_endpoints}
