"""TaskGenerationResult: the customizable portion of a task.yaml.

The generator class that used to live here was an early prototype that called a
now-deleted `generate_task.txt` prompt; nothing instantiates it anymore. Only the
result dataclass survives — it is the shared shape produced by
`eval_task_gen.py` and consumed by `assembler.py` / `grader_generator.py`.
"""

from __future__ import annotations


class TaskGenerationResult:
    """Result of task generation — all the customizable parts of task.yaml."""

    def __init__(
        self,
        task_id: str,
        task_name: str,
        category: str,
        difficulty: str,
        prompt_text: str,
        scoring_components: list[dict],
        safety_checks: list[dict],
        expected_actions: list[dict],
        judge_rubric: str,
        reference_solution: str,
        language: str = "en",
        expected_effects: list[dict] | None = None,
        tool_usage: dict | None = None,
        grounding_entity: dict | None = None,
        forbidden_tool: list[str] | None = None,
        value_in_reply: dict | None = None,
        answer_sheet: dict | None = None,
    ):
        self.task_id = task_id
        self.task_name = task_name
        self.category = category
        self.difficulty = difficulty
        self.prompt_text = prompt_text
        self.scoring_components = scoring_components
        self.safety_checks = safety_checks
        self.expected_actions = expected_actions
        self.judge_rubric = judge_rubric
        self.reference_solution = reference_solution
        self.language = language
        self.expected_effects = expected_effects or []
        # Rule-template blocks (gen-eval prompt now emits these alongside
        # the legacy fields; defaults preserve back-compat for older callers).
        self.tool_usage = tool_usage or {"must_call": [], "call_order": []}
        self.grounding_entity = grounding_entity or {"patterns": [], "threshold": 0.8}
        self.forbidden_tool = forbidden_tool or []
        self.value_in_reply = value_in_reply or {"must_contain": [], "must_not_contain": []}
        self.answer_sheet = answer_sheet or {"items": []}

    def __repr__(self) -> str:
        return f"TaskGenerationResult({self.task_id!r}, {self.task_name!r})"
