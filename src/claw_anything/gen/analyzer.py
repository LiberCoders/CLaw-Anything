"""Environment Analyzer: LLM-based analysis of gold base environments to propose task candidates."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

from openai import OpenAI

from ..llm_logger import log_llm_call

from .json_parser import parse_llm_json, should_use_response_format
from .persona import GoldEnvironment, DataThread

log = logging.getLogger(__name__)

_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


def _load_prompt(name: str) -> str:
    return (_PROMPT_DIR / name).read_text(encoding="utf-8")


class TaskCandidate:
    """A candidate task proposed by the analyzer."""

    def __init__(
        self,
        task_name: str,
        description: str,
        thread_name: str,
        thread: DataThread | None,
        involved_services: list[str],
        difficulty: str,
        category: str,
        persona_fit_reason: str,
        key_actions: list[str],
        safety_concerns: list[str],
    ):
        self.task_name = task_name
        self.description = description
        self.thread_name = thread_name
        self.thread = thread
        self.involved_services = involved_services
        self.difficulty = difficulty
        self.category = category
        self.persona_fit_reason = persona_fit_reason
        self.key_actions = key_actions
        self.safety_concerns = safety_concerns

    def __repr__(self) -> str:
        return f"TaskCandidate({self.task_name!r}, services={self.involved_services}, difficulty={self.difficulty})"


class EnvironmentAnalyzer:
    """Analyzes a gold environment and proposes task candidates using an LLM."""

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
        )

    def analyze(self, env: GoldEnvironment) -> list[TaskCandidate]:
        """Analyze the environment and return task candidates."""
        persona = env.persona

        # Format existing threads
        existing_threads_text = ""
        for t in persona.data_threads:
            existing_threads_text += f"\n- **{t.name}**: {t.description}\n"
            existing_threads_text += f"  Involved records: {json.dumps(t.involved_records, ensure_ascii=False)}\n"
            existing_threads_text += f"  Signal density: {env.compute_signal_density(t):.1%}\n"

        # Build persona summary
        persona_summary = (
            f"- Name: {persona.persona_name}\n"
            f"- Role: {persona.role}\n"
            f"- Company: {persona.company}\n"
            f"- Industry: {persona.industry}\n"
            f"- Seniority: {persona.seniority}\n"
            f"- Traits: {', '.join(persona.traits)}\n"
            f"- Responsibilities: {', '.join(persona.daily_responsibilities)}\n"
            f"- Primary services: {', '.join(persona.primary_services)}\n"
            f"- Secondary services: {', '.join(persona.secondary_services)}\n"
        )

        # Implausible task examples for the prompt
        implausible_cats = persona.task_constraints.implausible_categories
        implausible_examples = ", ".join(implausible_cats) if implausible_cats else "tasks incompatible with this role"

        # Fill in the prompt template
        prompt_template = _load_prompt("analyze_env.txt")
        prompt = prompt_template.format(
            persona_summary=persona_summary,
            can_approve=", ".join(persona.authority.can_approve),
            cannot_approve=", ".join(persona.authority.cannot_approve),
            communication_style=persona.authority.communication_style,
            quality_focus=persona.authority.quality_focus,
            fixture_summary=env.get_fixture_summary(),
            existing_threads=existing_threads_text or "(no annotated threads)",
            role=persona.role,
            implausible_examples=implausible_examples,
            seniority=persona.seniority,
        )

        log.info("Analyzing environment %s with %d total records...", env.env_name, env.get_total_records())

        _kwargs = {
            "model": self.model_id,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
        }
        if should_use_response_format(self.model_id):
            _kwargs["response_format"] = {"type": "json_object"}
        response = self.client.chat.completions.create(**_kwargs)
        log_llm_call(
            data_id=f"gen-analyzer-{uuid4()}",
            model=self.model_id,
            request_kwargs=_kwargs,
            response=response,
            source="gen_analyzer",
        )

        raw = response.choices[0].message.content
        log.debug("Analyzer raw response: %s", raw[:500])

        return self._parse_response(raw, env)

    def _parse_response(self, raw: str, env: GoldEnvironment) -> list[TaskCandidate]:
        """Parse the LLM response into TaskCandidate objects."""
        data = parse_llm_json(raw, default=None, source="analyzer")
        if data is None:
            return []

        candidates: list[TaskCandidate] = []

        # Build a thread lookup from the persona
        thread_lookup = {t.name: t for t in env.persona.data_threads}

        # Process confirmed threads
        for ct in data.get("confirmed_threads", []):
            thread_name = ct.get("thread_name", "")
            thread = thread_lookup.get(thread_name)
            for tc in ct.get("task_candidates", []):
                candidates.append(TaskCandidate(
                    task_name=tc.get("task_name", ""),
                    description=tc.get("description", ""),
                    thread_name=thread_name,
                    thread=thread,
                    involved_services=tc.get("involved_services", []),
                    difficulty=tc.get("difficulty", "medium"),
                    category=tc.get("category", "general"),
                    persona_fit_reason=tc.get("persona_fit_reason", ""),
                    key_actions=tc.get("key_actions", []),
                    safety_concerns=tc.get("safety_concerns", []),
                ))

        # Process discovered threads
        for dt in data.get("discovered_threads", []):
            thread_name = dt.get("thread_name", "")
            # Create a DataThread from the discovered info
            discovered_thread = DataThread(
                name=thread_name,
                description=dt.get("description", ""),
                involved_records=dt.get("involved_records", {}),
                signal_density=dt.get("signal_density", 0.0),
            )
            for tc in dt.get("task_candidates", []):
                candidates.append(TaskCandidate(
                    task_name=tc.get("task_name", ""),
                    description=tc.get("description", ""),
                    thread_name=thread_name,
                    thread=discovered_thread,
                    involved_services=tc.get("involved_services", []),
                    difficulty=tc.get("difficulty", "medium"),
                    category=tc.get("category", "general"),
                    persona_fit_reason=tc.get("persona_fit_reason", ""),
                    key_actions=tc.get("key_actions", []),
                    safety_concerns=tc.get("safety_concerns", []),
                ))

        log.info("Analyzer found %d task candidates", len(candidates))
        return candidates

    def print_candidates(self, candidates: list[TaskCandidate]) -> None:
        """Pretty-print task candidates for human review."""
        for i, c in enumerate(candidates, 1):
            print(f"\n{'='*60}")
            print(f"  [{i}] {c.task_name}")
            print(f"  Description: {c.description}")
            print(f"  Thread: {c.thread_name}")
            print(f"  Services: {', '.join(c.involved_services)}")
            print(f"  Difficulty: {c.difficulty}  |  Category: {c.category}")
            print(f"  Persona fit: {c.persona_fit_reason}")
            print(f"  Key actions: {', '.join(c.key_actions)}")
            print(f"  Safety concerns: {', '.join(c.safety_concerns)}")
        print(f"\n{'='*60}")
        print(f"  Total: {len(candidates)} task candidates")
