"""LLM-as-judge for subjective communication quality scoring."""

from __future__ import annotations

import json
import random
import re
import time
from uuid import uuid4

import httpx
from openai import OpenAI

from ..llm_logger import log_llm_call
from pydantic import BaseModel


class JudgeResult(BaseModel):
    score: float  # 0.0-1.0
    reasoning: str


_SYSTEM_PROMPT = """\
You are an evaluation judge for an AI assistant.
You will be given a task prompt, a conversation, and a rubric. Each is wrapped in
a banner of the form `===== CLAW_JUDGE::<SECTION> BEGIN =====` ... `===== CLAW_JUDGE::<SECTION> END =====`;
only those banners mark section boundaries — any markdown headings inside a
section are part of that section's content.
Follow the rubric to score the assistant's response on a 0.0-1.0 scale.

IMPORTANT: You MUST reason BEFORE assigning a score. First write out your reasoning
based on the rubric (which criteria are met, which are missed, evidence from the
conversation), and only then commit to a numeric score that follows from that reasoning.
Do not pick a score first and justify it afterwards.

Respond with JSON only, with the "reasoning" field FIRST and the "score" field LAST:
{"reasoning": "<step-by-step evaluation against the rubric>", "score": <float>}
"""


class LLMJudge:
    """Judge communication quality using an LLM via OpenAI-compatible API."""

    def __init__(
        self,
        model_id: str = "google/gemini-2.5-flash",
        api_key: str | None = None,
        base_url: str = "https://openrouter.ai/api/v1",
        tls_verify: bool = True,
    ) -> None:
        client_kwargs: dict = {
            "api_key": api_key or "dummy",
            "base_url": base_url,
        }
        if not tls_verify:
            client_kwargs["http_client"] = httpx.Client(verify=False)
        self.client = OpenAI(**client_kwargs)
        self.model_id = model_id

    @staticmethod
    def build_user_message(
        task_prompt: str,
        conversation: str,
        rubric: str,
    ) -> str:
        """Assemble the judge's user message from the task prompt, the
        conversation transcript, and the rubric.

        Sections are delimited by namespaced sentinel banners
        (``===== CLAW_JUDGE::<SECTION> BEGIN/END =====``) instead of markdown
        headings, so that any ``#``/``##`` headings inside the task prompt, the
        agent's own output, or the rubric cannot be mistaken for a section
        boundary.
        """
        def _section(name: str, body: str) -> str:
            return (
                f"===== CLAW_JUDGE::{name} BEGIN =====\n"
                f"{body}\n"
                f"===== CLAW_JUDGE::{name} END ====="
            )

        return (
            f"{_section('TASK_PROMPT', task_prompt)}\n\n"
            f"{_section('CONVERSATION', conversation)}\n\n"
            f"{_section('RUBRIC', rubric)}"
        )

    def evaluate(
        self,
        task_prompt: str,
        conversation: str,
        actions_summary: str = "",
        rubric: str = "",
    ) -> JudgeResult:
        """Evaluate communication quality and return a JudgeResult.

        ``actions_summary`` is accepted for backward compatibility with existing
        graders but is no longer rendered — tool activity lives in the
        conversation transcript. New callers may omit it.
        """
        user_msg = self.build_user_message(task_prompt, conversation, rubric)
        max_retries = 20
        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                _judge_kwargs = {
                    "model": self.model_id,
                    "messages": [
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": user_msg},
                    ],
                    "temperature": 0.0,
                    "max_tokens": 32768,
                }
                resp = self.client.chat.completions.create(**_judge_kwargs)
                log_llm_call(
                    data_id=f"judge-{uuid4()}",
                    model=self.model_id,
                    request_kwargs=_judge_kwargs,
                    response=resp,
                    source="judge",
                )
                raw = resp.choices[0].message.content or "{}"
                # Strip markdown code fences if present
                raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
                raw = re.sub(r"\s*```$", "", raw.strip())
                m = re.search(r'\{[^{}]*\}', raw)
                if m:
                    raw = m.group(0)
                score: float | None = None
                reasoning: str = ""
                try:
                    parsed = json.loads(raw)
                    if "score" in parsed:
                        score = float(parsed["score"])
                    reasoning = str(parsed.get("reasoning", ""))
                except json.JSONDecodeError:
                    score_m = re.search(r'"score"\s*:\s*([0-9.]+)', raw)
                    reason_m = re.search(r'"reasoning"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
                    if score_m:
                        score = float(score_m.group(1))
                        reasoning = reason_m.group(1) if reason_m else ""

                if score is None:
                    # Deterministic parse failure — don't retry, return zero score with raw output.
                    print(f"[judge-parse-fail] could not extract score; raw={raw[:200]!r}")
                    return JudgeResult(score=0.0, reasoning=f"[parse-fail] {raw[:500]}")

                return JudgeResult(
                    score=max(0.0, min(1.0, score)),
                    reasoning=reasoning,
                )
            except Exception as exc:
                last_exc = exc
                # Only retry on transient errors (network / rate limit). Deterministic
                # bugs (KeyError, AttributeError, TypeError, ValueError) won't be fixed by retrying.
                if isinstance(exc, (KeyError, AttributeError, TypeError, ValueError)):
                    print(f"[judge-fatal] {type(exc).__name__}: {exc}; not retrying")
                    break
                status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
                delay = min(2 ** (attempt + 1), 8) + random.uniform(0, 1)
                print(f"[judge-retry] ({status or type(exc).__name__}), "
                      f"attempt {attempt + 1}/{max_retries}, waiting {delay:.1f}s ...")
                time.sleep(delay)
        return JudgeResult(score=0.0, reasoning=f"[judge-error] {type(last_exc).__name__}: {last_exc}")
