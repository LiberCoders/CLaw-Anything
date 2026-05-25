"""Task Generator: LLM-based generation of task.yaml content from persona + data thread."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from openai import OpenAI

from ..llm_logger import log_llm_call

from .analyzer import TaskCandidate
from .json_parser import parse_llm_json, should_use_response_format
from .persona import GoldEnvironment, DataThread

log = logging.getLogger(__name__)



_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


def _load_prompt(name: str) -> str:
    return (_PROMPT_DIR / name).read_text(encoding="utf-8")


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

    def __repr__(self) -> str:
        return f"TaskGenerationResult({self.task_id!r}, {self.task_name!r})"


class TaskGenerator:
    """Generates task.yaml content from a persona + data thread using an LLM."""

    def __init__(
        self,
        model_id: str,
        api_key: str | None = None,
        base_url: str | None = None,
    ):
        self.model_id = model_id
        self.client = OpenAI(
            api_key=api_key or "unused",
            base_url=base_url,
            max_retries=5,
            timeout=120.0,
        )

    def generate(
        self,
        env: GoldEnvironment,
        candidate: TaskCandidate,
        task_num: int = 1,
    ) -> TaskGenerationResult:
        """Generate task.yaml content for a given candidate."""
        persona = env.persona
        thread = candidate.thread

        if thread is None:
            raise ValueError(f"Candidate {candidate.task_name!r} has no associated data thread")

        # Get full records for the thread
        thread_records_json = env.get_thread_records_json(thread)

        # Build persona summary
        persona_summary = (
            f"- Name: {persona.persona_name}\n"
            f"- Role: {persona.role}\n"
            f"- Company: {persona.company}\n"
            f"- Industry: {persona.industry}\n"
            f"- Seniority: {persona.seniority}\n"
            f"- Traits: {', '.join(persona.traits)}\n"
            f"- Responsibilities: {', '.join(persona.daily_responsibilities)}\n"
        )

        # Format the prompt
        prompt_template = _load_prompt("generate_task.txt")
        prompt = prompt_template.format(
            persona_summary=persona_summary,
            can_approve=", ".join(persona.authority.can_approve),
            cannot_approve=", ".join(persona.authority.cannot_approve),
            communication_style=persona.authority.communication_style,
            quality_focus=persona.authority.quality_focus,
            thread_name=thread.name,
            thread_description=thread.description,
            thread_records_json=thread_records_json,
            task_name=candidate.task_name,
            task_description=candidate.description,
            involved_services=", ".join(candidate.involved_services),
            difficulty=candidate.difficulty,
            category=candidate.category,
            key_actions=", ".join(candidate.key_actions),
            safety_concerns=", ".join(candidate.safety_concerns),
            persona_id=persona.persona_id.replace("E", ""),
            task_num=f"{task_num:02d}",
            seniority=persona.seniority,
        )

        log.info("Generating task for candidate: %s", candidate.task_name)

        _kwargs = {
            "model": self.model_id,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.5,
        }
        if should_use_response_format(self.model_id):
            _kwargs["response_format"] = {"type": "json_object"}
        response = self.client.chat.completions.create(**_kwargs)
        log_llm_call(
            data_id=f"gen-task-{uuid4()}",
            model=self.model_id,
            request_kwargs=_kwargs,
            response=response,
            source="gen_task",
        )

        raw = response.choices[0].message.content
        log.debug("Task generator raw response: %s", raw[:500])

        return self._parse_response(raw)

    def _parse_response(self, raw: str) -> TaskGenerationResult:
        """Parse LLM response into TaskGenerationResult."""
        data = parse_llm_json(raw, default=None, source="task_generator")
        if data is None:
            raise ValueError("Failed to parse task generator response as JSON")

        # Sanitize task_id: ensure it's a valid directory name
        raw_task_id = data["task_id"]
        # Normalize: keep only alnum and underscores, collapse multiple underscores
        sanitized_id = re.sub(r"[^a-zA-Z0-9_]", "_", raw_task_id)
        sanitized_id = re.sub(r"_+", "_", sanitized_id).strip("_")
        if not sanitized_id:
            sanitized_id = f"E00_T00_generated_task"

        return TaskGenerationResult(
            task_id=sanitized_id,
            task_name=data["task_name"],
            category=data.get("category", "general"),
            difficulty=data.get("difficulty", "medium"),
            prompt_text=data["prompt_text"],
            scoring_components=data.get("scoring_components", []),
            safety_checks=data.get("safety_checks", []),
            expected_actions=data.get("expected_actions", []),
            judge_rubric=data.get("judge_rubric", ""),
            reference_solution=data.get("reference_solution", ""),
            language=data.get("language", "zh"),
        )
