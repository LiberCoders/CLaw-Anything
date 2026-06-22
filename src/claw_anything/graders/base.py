"""Abstract base grader with shared helpers for robustness and communication."""

from __future__ import annotations

import importlib.util
import inspect
import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models.task import TaskDefinition
from ..models.trace import DimensionScores, ToolDispatch, TraceMessage

# base.py is at src/claw_anything/graders/base.py → parents[3] is the repo root.
_DEFAULT_TASKS_DIR = Path(__file__).resolve().parents[3] / "tasks"

# Infrastructure endpoints that are NOT agent tool calls — they must never be
# surfaced to the LLM judge (they are noise and can mislead the evaluation).
_INFRA_SUFFIXES = ("/health", "/audit", "/reset", "/docs", "/openapi.json")

# Max characters for inlined tool-call params / results, to keep judge input bounded.
_TRUNCATE_LEN = 300


def _truncate(text: str, limit: int = _TRUNCATE_LEN) -> str:
    text = text if isinstance(text, str) else str(text)
    return text if len(text) <= limit else text[:limit] + "…"


# ======================================================================
# State-verification + grounding primitives
# ----------------------------------------------------------------------
# The grading pipeline historically scored the agent's NARRATION (did it
# *say* the right keywords / *call* the right tools) rather than the
# resulting world-state. That let hallucinations and silent write-failures
# pass. The helpers below let graders verify (a) that a claimed mutation
# actually landed in a service's /audit log, (b) that asserted IDs/values
# are grounded in real tool responses, and (c) that the agent didn't claim
# completion of an action whose write actually failed or never happened.
# ======================================================================

# Single source of truth for each mock service's /audit "action" lists — the
# keys under which a LANDED mutation is recorded. Verified against real audit
# snapshots. A failed write (e.g. sheet "Sheet not found") never appears here,
# so an empty list is itself evidence of a silent failure. Graders must use
# these exact keys rather than guessing (e.g. "sent" vs "sent_messages").
ACTION_KEYS: dict[str, list[str]] = {
    "gmail": ["sent_messages", "drafts"],
    "calendar": ["created_events", "deleted"],
    "todo": ["updated_tasks", "created_tasks", "deleted"],
    "helpdesk": ["updated_tickets", "closed"],
    "kb": ["updates"],
    "sheet": ["updates", "inserts", "created_workbooks", "added_sheets", "renamed", "deleted"],
    "finance": ["submitted_reports"],
    "contacts": ["sent_messages"],
    "notes": ["shared"],
    "crm": ["exported_reports"],
    "scheduler": ["created_jobs", "updated_jobs", "deleted_jobs"],
    "inventory": ["created_orders"],
}

# Tool-name suffixes that mutate state (used by the generic re-grade to know
# which dispatches are "writes" whose success must be verified).
WRITE_TOOL_MARKERS: tuple[str, ...] = (
    "_send_message", "_save_draft", "_send",
    "_create_", "_create", "_update_", "_update", "_set_cell", "_set_range",
    "_insert_row", "_add_", "_close_", "_close", "_delete_", "_delete",
    "_archive", "_publish", "_submit", "_report_submit",
)

# Default entity patterns for grounding: record IDs (MSG-xxxx, TK-xxx, …) plus
# money amounts. Mirrors grader_generator._extract_record_ids and adds numbers.
_ID_PATTERN = r'(?:MSG|TK|CUS|CON|KB|SKU|EVT|TODO|TXN|JOB|RSS|CFG|NOTE|INT|NPAG|ZOT|WB|TODO|OIG)-[A-Za-z0-9_]+'
_MONEY_PATTERN = r'\$\s?\d[\d,]*(?:\.\d+)?'


@dataclass(frozen=True)
class EffectResult:
    """Outcome of asserting that a required mutation landed in the audit log."""
    landed: bool
    count: int
    matched: int
    records: list[dict] = field(default_factory=list)
    reason: str = ""

    @property
    def score(self) -> float:
        """1.0 if at least one record matched the constraints, else 0.0."""
        return 1.0 if self.landed and self.matched >= 1 else 0.0


@dataclass(frozen=True)
class GroundingResult:
    """Which asserted entities are backed by real tool responses."""
    supported: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    score: float = 1.0


@dataclass(frozen=True)
class SilentFailure:
    """A completion the agent CLAIMED but that did not actually happen."""
    claim: str
    kind: str  # 'absent' | 'dispatch_failed' | 'mismatch'
    reason: str


@dataclass(frozen=True)
class ClaimSpec:
    """Maps a natural-language completion claim to the effect that must back it.

    If any ``phrases`` appears in the agent's final text, the agent is asserting
    it performed this action; the effect (``service``/``action_key`` + optional
    ``match``) must then be present in the audit log or it is a silent failure.
    """
    phrases: list[str]
    service: str
    action_key: str
    match: dict[str, Any] | None = None


def load_peer_grader(task_id: str, tasks_dir: str | Path = _DEFAULT_TASKS_DIR) -> type:
    """Load a grader class from another task directory.

    Used by English variant graders to inherit from their Chinese counterpart.

    Returns the first AbstractGrader subclass found in tasks/<task_id>/grader.py.
    """
    grader_path = Path(tasks_dir) / task_id / "grader.py"
    if not grader_path.exists():
        raise FileNotFoundError(
            f"No grader found at {grader_path} for task_id={task_id!r}"
        )

    module_name = f"peer_grader_{task_id}"
    spec = importlib.util.spec_from_file_location(module_name, grader_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load peer grader module from {grader_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    for _name, obj in inspect.getmembers(module, inspect.isclass):
        if issubclass(obj, AbstractGrader) and obj is not AbstractGrader:
            return obj

    raise ValueError(f"No AbstractGrader subclass found in {grader_path}")


class AbstractGrader(ABC):
    """Base class for task graders."""

    @abstractmethod
    def grade(
        self,
        messages: list[TraceMessage],
        dispatches: list[ToolDispatch],
        task: TaskDefinition,
        audit_data: dict[str, dict] | None = None,
        judge: Any | None = None,
    ) -> DimensionScores:
        """Grade a trace and return dimension scores."""
        ...

    # ------------------------------------------------------------------
    # Shared helpers – subclasses can use or override
    # ------------------------------------------------------------------

    @staticmethod
    def _get_final_assistant_text(messages: list[TraceMessage]) -> str:
        """Extract text from the last assistant message."""
        for msg in reversed(messages):
            if msg.message.role == "assistant":
                return msg.message.text
        return ""

    @staticmethod
    def _get_all_assistant_text(messages: list[TraceMessage]) -> str:
        """Concatenate text from all assistant messages."""
        return "\n".join(
            m.message.text for m in messages if m.message.role == "assistant"
        )

    @staticmethod
    def compute_robustness(dispatches: list[ToolDispatch]) -> float:
        """Robustness = recovery rate from errors.

        - If errors occurred and all were retried successfully → 1.0
        - If errors occurred and none recovered → floor based on overall success rate
        - If no errors occurred (clean run) → 1.0 (full credit)

        Recovery is detected when the same tool_name is called successfully
        after a failed call.  An agent that succeeds *despite* errors (by
        working around them) also demonstrates robustness, so a floor is
        applied based on the overall success rate of tool calls.
        """
        error_dispatches = [d for d in dispatches if d.response_status >= 400]
        if not error_dispatches:
            return 1.0  # no errors ⇒ clean run, full credit

        # Track which tool names had errors
        errored_tools: dict[str, int] = {}
        for d in error_dispatches:
            errored_tools[d.tool_name] = errored_tools.get(d.tool_name, 0) + 1

        # Check for recovery: successful call to same tool after error
        recovered_tools: set[str] = set()
        seen_errors: set[str] = set()
        for d in dispatches:
            if d.response_status >= 400:
                seen_errors.add(d.tool_name)
            elif d.tool_name in seen_errors and d.response_status < 400:
                recovered_tools.add(d.tool_name)

        recovery_rate = len(recovered_tools) / len(errored_tools)

        # Floor: an agent that makes many successful calls despite some errors
        # demonstrates resilience even without explicit retries.
        total_calls = len(dispatches)
        success_calls = total_calls - len(error_dispatches)
        if total_calls > 0:
            success_ratio = success_calls / total_calls
            # If most calls succeed, give a floor of up to 0.5
            floor = round(min(success_ratio, 0.5), 2)
        else:
            floor = 0.0

        return round(max(recovery_rate, floor), 2)

    @staticmethod
    def compute_communication_substance(
        final_text: str,
        tool_entities: list[str],
        format_score: float,
    ) -> float:
        """Communication score that requires substance, not just formatting.

        - Cross-validates: what fraction of expected entities appear in output
        - Format score alone caps at 0.5
        - Substance alone caps at 0.5
        - Combined: format_component + substance_component

        Args:
            final_text: The final assistant message text.
            tool_entities: List of entity strings from tool responses that
                          should appear in the output (names, IDs, values).
            format_score: 0.0-1.0 score for formatting quality.
        """
        if not tool_entities:
            # No entities to validate → fall back to format only (capped at 0.7)
            return min(format_score, 0.7)

        # Count how many entities appear in the output
        found = sum(1 for e in tool_entities if e in final_text)
        entity_rate = found / len(tool_entities)

        # Substance component: up to 0.5
        substance = 0.5 * min(entity_rate / 0.4, 1.0)  # 40% threshold → full marks

        # Format component: up to 0.5
        fmt = 0.5 * format_score

        return round(min(substance + fmt, 1.0), 2)

    # ------------------------------------------------------------------
    # Audit-data helpers for action-oriented graders
    # ------------------------------------------------------------------

    @staticmethod
    def get_service_actions(
        audit_data: dict[str, dict] | None,
        service: str,
        action_key: str,
    ) -> list[dict]:
        """Extract a list of action records from audit data.

        Example: get_service_actions(audit, "gmail", "drafts") returns the
        list of saved drafts from the gmail mock service audit.
        """
        if not audit_data:
            return []
        svc_data = audit_data.get(service, {})
        result = svc_data.get(action_key, [])
        if isinstance(result, list):
            return result
        return []

    @staticmethod
    def get_audit_calls(
        audit_data: dict[str, dict] | None,
        service: str,
    ) -> list[dict]:
        """Get the raw call log from a service's audit data."""
        if not audit_data:
            return []
        svc_data = audit_data.get(service, {})
        return svc_data.get("calls", [])

    # ------------------------------------------------------------------
    # State verification + grounding primitives
    # ------------------------------------------------------------------

    @staticmethod
    def _norm(value: Any) -> str:
        """Normalize a scalar for fuzzy matching (lowercase, strip $ and commas)."""
        s = value if isinstance(value, str) else str(value)
        return s.lower().replace("$", "").replace(",", "").strip()

    @classmethod
    def _record_matches(cls, record: dict, match: dict[str, Any]) -> bool:
        """True if ``record`` satisfies every (field, expected) in ``match``.

        - expected may be a scalar or a list-of-alternatives (any-of).
        - string match is case-insensitive substring; numbers are compared
          after stripping $/commas; list-valued record fields (e.g. attendees)
          match if any element contains the expected value.
        """
        if not isinstance(record, dict):
            return False
        for fieldname, expected in match.items():
            actual = record.get(fieldname)
            alternatives = expected if isinstance(expected, list) else [expected]
            hit = False
            for alt in alternatives:
                alt_n = cls._norm(alt)
                if isinstance(actual, list):
                    if any(alt_n in cls._norm(el) for el in actual):
                        hit = True
                        break
                elif actual is not None and alt_n in cls._norm(actual):
                    hit = True
                    break
            if not hit:
                return False
        return True

    @classmethod
    def assert_effect(
        cls,
        audit_data: dict[str, dict] | None,
        service: str,
        action_key: str,
        *,
        match: dict[str, Any] | None = None,
        min_count: int = 1,
    ) -> EffectResult:
        """Assert a required mutation actually landed in the service's audit log.

        Returns an :class:`EffectResult`. ``landed`` is True when at least
        ``min_count`` records exist under ``service``/``action_key``; ``matched``
        counts records additionally satisfying ``match`` (recipient/value/etc.).
        A claimed-but-absent or wrong-recipient action yields ``score == 0``.
        """
        records = cls.get_service_actions(audit_data, service, action_key)
        count = len(records)
        if match is None:
            matched = count
            matched_recs = records
        else:
            matched_recs = [r for r in records if cls._record_matches(r, match)]
            matched = len(matched_recs)
        landed = count >= min_count
        if count == 0:
            reason = f"no records under {service}.{action_key}"
        elif match is not None and matched == 0:
            reason = f"{count} record(s) under {service}.{action_key} but none match {match}"
        else:
            reason = f"{matched}/{count} record(s) matched under {service}.{action_key}"
        return EffectResult(
            landed=landed, count=count, matched=matched,
            records=matched_recs or records, reason=reason,
        )

    @staticmethod
    def extract_asserted_ids(text: str, patterns: list[str] | None = None) -> list[str]:
        """Extract record-ID / money-like tokens the agent asserts in its text."""
        if not text:
            return []
        pats = patterns if patterns is not None else [_ID_PATTERN, _MONEY_PATTERN]
        out: list[str] = []
        for pat in pats:
            out.extend(re.findall(pat, text))
        # dedupe, preserve order
        seen: set[str] = set()
        result = []
        for tok in out:
            key = tok.strip()
            if key and key not in seen:
                seen.add(key)
                result.append(key)
        return result

    @classmethod
    def _response_corpus(
        cls,
        dispatches: list[ToolDispatch],
        audit_data: dict[str, dict] | None = None,
    ) -> str:
        """Concatenate what the environment REVEALED via READ calls (for grounding).

        Only successful, non-write dispatch responses are included: grounding
        asks "did the agent learn this fact from the environment", and only
        reads provide facts. WRITE responses are excluded so the agent cannot
        self-ground a fabricated value by writing it into a record whose echo
        would otherwise count as support. (Server-assigned IDs from a write are
        validated separately by ``assert_effect``.) ``audit_data`` is unused
        here — dispatches already carry every tool response.
        """
        parts: list[str] = []
        for d in dispatches:
            if getattr(d, "response_status", 200) >= 400 or d.response_body is None:
                continue
            if any(m in d.tool_name for m in WRITE_TOOL_MARKERS):
                continue  # skip write echoes — they only mirror agent-supplied input
            parts.append(
                d.response_body if isinstance(d.response_body, str)
                else json.dumps(d.response_body, ensure_ascii=False, default=str)
            )
        return "\n".join(parts)

    @classmethod
    def grounding_score(
        cls,
        final_text: str,
        dispatches: list[ToolDispatch],
        entities: list[str] | None = None,
        *,
        audit_data: dict[str, dict] | None = None,
        require_all: bool = False,
    ) -> GroundingResult:
        """Fraction of asserted entities that are backed by real tool responses.

        ``entities`` defaults to auto-extracted IDs/amounts in ``final_text``.
        An entity is "asserted" if it appears in ``final_text`` and "supported"
        only if it ALSO appears in the tool-response corpus. ``score`` =
        supported / asserted (1.0 if nothing asserted). ``unsupported`` are
        likely fabrications.
        """
        asserted = (
            [e for e in entities if e and e in final_text]
            if entities is not None
            else cls.extract_asserted_ids(final_text)
        )
        if not asserted:
            return GroundingResult(supported=[], unsupported=[], score=1.0)
        corpus = cls._response_corpus(dispatches, audit_data)
        corpus_n = cls._norm(corpus)
        supported, unsupported = [], []
        for e in asserted:
            (supported if cls._norm(e) in corpus_n else unsupported).append(e)
        score = len(supported) / len(asserted)
        if require_all and unsupported:
            score = 0.0
        return GroundingResult(supported=supported, unsupported=unsupported, score=round(score, 4))

    @classmethod
    def detect_silent_failure(
        cls,
        final_text: str,
        dispatches: list[ToolDispatch],
        audit_data: dict[str, dict] | None,
        completion_claims: list[ClaimSpec],
    ) -> list[SilentFailure]:
        """Find completions the agent CLAIMED in text that did not actually happen.

        For each claim whose ``phrases`` appear in ``final_text``: if no
        successful (<400) dispatch wrote it, or the effect is absent / does not
        match in the audit log, emit a :class:`SilentFailure`. Returns [] when
        no claim is asserted (safe for advisory/text-only tasks).
        """
        text_l = (final_text or "").lower()
        failures: list[SilentFailure] = []
        for claim in completion_claims:
            if not any(p.lower() in text_l for p in claim.phrases):
                continue  # agent never claimed this — nothing to verify
            label = claim.phrases[0]
            eff = cls.assert_effect(
                audit_data, claim.service, claim.action_key, match=claim.match,
            )
            if eff.count == 0:
                # Did a dispatch to a writing tool fail outright?
                failed = [
                    d for d in dispatches
                    if d.tool_name.startswith(claim.service + "_")
                    and getattr(d, "response_status", 200) >= 400
                ]
                if failed:
                    failures.append(SilentFailure(
                        claim=label, kind="dispatch_failed",
                        reason=f"claimed '{label}' but {claim.service} write returned "
                               f"{failed[0].response_status}: {eff.reason}",
                    ))
                else:
                    failures.append(SilentFailure(
                        claim=label, kind="absent",
                        reason=f"claimed '{label}' but {eff.reason}",
                    ))
            elif claim.match is not None and eff.matched == 0:
                failures.append(SilentFailure(
                    claim=label, kind="mismatch",
                    reason=f"claimed '{label}' but {eff.reason}",
                ))
        return failures

    @classmethod
    def build_evidence(
        cls,
        dispatches: list[ToolDispatch],
        audit_data: dict[str, dict] | None = None,
        *,
        max_chars: int = 8000,
    ) -> str:
        """Compact, write/effect-prioritized evidence block for the judge.

        Unlike ``format_conversation`` (which truncates every result to 300
        chars), this gives the judge the actual values it needs to verify
        factual correctness. Landed audit effects and write-tool responses are
        kept first; read-only list calls are dropped first when over budget.
        """
        effect_lines: list[str] = []
        if audit_data:
            for svc, svc_data in audit_data.items():
                if not isinstance(svc_data, dict):
                    continue
                for key in ACTION_KEYS.get(svc, []):
                    recs = svc_data.get(key)
                    if recs:
                        effect_lines.append(
                            f"[EFFECT] {svc}.{key} ({len(recs)}): "
                            f"{_truncate(json.dumps(recs, ensure_ascii=False, default=str), 1200)}"
                        )
        write_lines, read_lines = [], []
        for d in dispatches:
            is_write = any(m in d.tool_name for m in WRITE_TOOL_MARKERS)
            status = getattr(d, "response_status", 200)
            outcome = "OK" if status < 400 else f"FAILED({status})"
            body = (
                d.response_body if isinstance(d.response_body, str)
                else json.dumps(d.response_body, ensure_ascii=False, default=str)
            ) if d.response_body is not None else ""
            line = (
                f"[{'WRITE' if is_write else 'READ'}] {d.tool_name}"
                f"({_truncate(json.dumps(d.request_body, ensure_ascii=False, default=str), 200)})"
                f" -> {outcome} {_truncate(body, 600)}"
            )
            (write_lines if is_write else read_lines).append(line)

        # Assemble within budget: effects, then writes, then reads (oldest read
        # dropped first by truncating the read tail).
        out: list[str] = []
        budget = max_chars
        for group in (effect_lines, write_lines, read_lines):
            for ln in group:
                if budget - len(ln) <= 0:
                    return "\n".join(out)
                out.append(ln)
                budget -= len(ln) + 1
        return "\n".join(out)

    @staticmethod
    def format_conversation(
        messages: list[TraceMessage],
        dispatches: list[ToolDispatch] | None = None,
    ) -> str:
        """Format messages into a chronological transcript for judge input.

        Walks each message's content blocks in order and inlines tool activity
        so the judge sees a single timeline of "what the agent said → what it
        called → whether it succeeded → what it got back":

        - TextBlock        → ``[<ROLE>]: <text>``
        - ToolUseBlock     → ``[TOOL CALL] <name>(<input>) -> OK|FAILED(<status>)``
        - ToolResultBlock  → ``[TOOL RESULT] <text>`` (or ``[TOOL ERROR] ...``)

        Success/failure is taken from the matching ToolDispatch.response_status
        (linked by tool_use_id); when ``dispatches`` is not supplied it falls
        back to the ToolResultBlock.is_error flag. Infrastructure endpoints
        (/health, /audit, /reset …) are never agent tool calls and so never
        appear here. Params and results are truncated to keep input bounded.
        """
        status_by_id: dict[str, int] = {
            d.tool_use_id: d.response_status for d in (dispatches or [])
        }

        lines: list[str] = []
        for m in messages:
            role = m.message.role.upper()
            for block in m.message.content:
                btype = getattr(block, "type", None)
                if btype == "text":
                    if block.text and block.text.strip():
                        lines.append(f"[{role}]: {block.text}")
                elif btype == "tool_use":
                    params = _truncate(json.dumps(block.input, ensure_ascii=False))
                    status = status_by_id.get(block.id)
                    if status is not None:
                        outcome = "OK" if status < 400 else f"FAILED({status})"
                    else:
                        outcome = "OK"  # no dispatch record; refined below if a result errors
                    lines.append(f"[TOOL CALL] {block.name}({params}) -> {outcome}")
                elif btype == "tool_result":
                    result_text = " ".join(
                        tb.text for tb in block.content
                        if getattr(tb, "type", None) == "text"
                    )
                    tag = "ERROR" if block.is_error else "RESULT"
                    lines.append(f"[TOOL {tag}] {_truncate(result_text)}")
        return "\n".join(lines)

    @staticmethod
    def summarize_actions(audit_data: dict[str, dict] | None) -> str:
        """Produce a human-readable summary of actions taken, for judge input.

        Infrastructure endpoints (/health, /audit, /reset …) are filtered out —
        they are not agent tool calls and only add noise. Note: the richer
        per-call view (tool name + params + success/failure) now lives in the
        unified transcript from ``format_conversation(messages, dispatches)``;
        this method is kept as a lightweight, infra-filtered fallback.
        """
        if not audit_data:
            return "No audit data available."
        parts = []
        for svc_name, svc_data in audit_data.items():
            calls = svc_data.get("calls", [])
            endpoints = [
                c.get("endpoint", "?") for c in calls
                if not str(c.get("endpoint", "")).endswith(_INFRA_SUFFIXES)
            ]
            if endpoints:
                parts.append(
                    f"{svc_name}: {len(endpoints)} calls — {', '.join(endpoints)}"
                )
        return "\n".join(parts) if parts else "No actions recorded."
