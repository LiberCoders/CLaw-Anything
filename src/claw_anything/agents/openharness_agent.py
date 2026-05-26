"""OpenHarnessAgent: runs vanilla OpenHarness (the ``oh`` CLI) as a claw-anything agent.

Spawns a fresh ``oh -p <prompt> --output-format stream-json
--dangerously-skip-permissions`` subprocess per trial, with an isolated
``OPENHARNESS_CONFIG_DIR`` containing a generated ``clawanything`` plugin that
exposes the task's tools as native OH ``BaseTool`` subclasses backed by HTTP
calls to the mock services running alongside this process.

Unlike ``LoopAgent``, OH drives its own think-act-observe loop — this agent
does not call the model turn-by-turn. It captures OH's stream-json output,
parses it into turns, and reports each turn back to ``BaseAgent`` via
``emit_message`` / ``emit_tool_dispatch`` so the standard trace lifecycle
(TraceStart / TraceEnd / audit snapshots / time accounting) is shared with
every other agent.

Model / base-url / API key all come from the OH settings file
(``--oh-settings``); this agent never passes them on the command line. If the
settings omit them, ``oh`` reports the error itself.

This is the base class for the OpenHarness-family agents. ``OpenHarnessExtAgent``
(``openharness_ext_agent.py``) extends this with OH-Ext-specific knobs — namely
denying the 3 expert-* builtin tools added by the fork. Subclasses can override
``LOG_PREFIX`` and ``builtin_tools_for_deny`` to customise behaviour.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from claw_anything.config import ModelConfig, PromptConfig
from claw_anything.models.content import (
    ContentBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from claw_anything.models.message import Message
from claw_anything.models.task import TaskDefinition
from claw_anything.models.trace import TokenUsage, ToolDispatch
from claw_anything.runner.openharness_plugin_gen import PLUGIN_NAME, generate_plugin_files
from claw_anything.runner.providers.openai_compat import OpenAICompatProvider

from .base import BaseAgent


#: Names of all vanilla OpenHarness built-in tools (openharness-ai 0.1.7,
#: ``src/openharness/tools/``). Owned by this module — the OH-Ext subclass
#: extends this list with its own fork-only additions rather than the plugin_gen
#: module carrying the knowledge of both sets. When ``disable_builtin_tools`` is
#: set, ``builtin_tools_for_deny()`` returns this list and the runner passes it
#: to ``generate_plugin_files`` as ``extra_denied_tools`` so OH only exposes the
#: per-task clawanything tools to the model.
OH_BUILTIN_TOOLS: list[str] = [
    "agent", "ask_user_question", "bash", "brief", "config",
    "cron_create", "cron_delete", "cron_list", "cron_toggle",
    "edit_file", "enter_plan_mode", "enter_worktree",
    "exit_plan_mode", "exit_worktree", "glob", "grep",
    "image_generation", "image_to_text",
    "list_mcp_resources", "lsp", "mcp_auth", "notebook_edit",
    "read_file", "read_mcp_resource", "remote_trigger",
    "send_message", "skill", "sleep",
    "task_create", "task_get", "task_list", "task_output",
    "task_stop", "task_update",
    "team_create", "team_delete", "todo_write", "tool_search",
    "web_fetch", "web_search", "write_file",
]


#: Per-task wall-clock budget (seconds) used only when the task does not set
#: one. ``TaskDefinition.environment.timeout_seconds`` already defaults to this
#: value, so the fallback effectively triggers only for an explicit 0.
_DEFAULT_TASK_TIMEOUT_S = 1200

#: Extra grace beyond the task budget for ``oh`` startup + teardown before the
#: subprocess is force-killed.
_OH_SHUTDOWN_GRACE_S = 60


def _log(msg: str) -> None:
    """Print a log line and flush immediately.

    Mirrors the logging convention in ``agents/loop.py`` — the flush matters so
    progress is visible in real time when stdout is a pipe (e.g. container logs).
    """
    print(msg, flush=True)


# ---------------------------------------------------------------------- #
#  OH stream-json / dispatch.jsonl parsing helpers                        #
# ---------------------------------------------------------------------- #
#
# OH emits stream-json events of the following shape (see
# ``openharness/ui/app.py`` ``run_print_mode``):
#
#     {"type": "system", "message": ...}
#     {"type": "assistant_delta", "text": ...}
#     {"type": "assistant_complete", "text": <final turn text>}
#     {"type": "tool_started", "tool_name": ..., "tool_input": {...}}
#     {"type": "tool_completed", "tool_name": ..., "output": <str>, "is_error": <bool>}
#     {"type": "error", "message": ..., "recoverable": <bool>}
#     {"type": "compact_progress", ...}
#     {"type": "status", "message": ...}
#
# Each trial also writes a side-channel ``dispatch.jsonl`` (see
# ``openharness_plugin_gen.py``); each line records the verbatim HTTP
# request/response/latency for one tool invocation.


def _read_dispatch_log(path: Path) -> list[dict]:
    """Read dispatch.jsonl lines (best-effort)."""
    if not path.exists():
        return []
    out: list[dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _pop_first_match(records: list[dict], tool_name: str) -> dict | None:
    """Pop the first record whose ``tool_name`` matches; FIFO within a name."""
    for idx, rec in enumerate(records):
        if rec.get("tool_name") == tool_name:
            return records.pop(idx)
    return None


def _split_turns(events: list[dict]) -> list[dict]:
    """Group events into turns: (assistant text, tool_uses, tool_results)."""
    turns: list[dict] = []
    i = 0
    n = len(events)
    while i < n:
        progress_anchor = i
        text = ""
        usage: dict | None = None
        tool_uses: list[dict] = []   # {id, name, input}
        tool_results: list[dict] = []  # {name, output, is_error}

        # Consume assistant_delta / assistant_complete (and informational events).
        while i < n:
            t = events[i].get("type")
            if t == "assistant_delta":
                text += events[i].get("text", "")
                i += 1
            elif t == "assistant_complete":
                # Authoritative text of the turn.
                text = events[i].get("text", text)
                # ``usage`` enters the stream via two paths: the build-time
                # patch baked into claw-anything-oh
                # (docker/oh/patch_print_mode_usage.py) for vanilla OH, or
                # OH-Ext emitting it natively when its
                # ``settings.print_mode.stream_json_extra_fields`` includes
                # ``"usage"``. The build-time patch fails loudly on OH bumps;
                # this ``None`` fallback only triggers for an unpatched ``oh``
                # on PATH (e.g. local dev outside the image), in which case
                # ``_emit_oh_turns`` records a per-turn failure.
                usage = events[i].get("usage")
                i += 1
                break
            elif t in ("system", "status", "compact_progress"):
                i += 1  # informational; ignore for trace
            else:
                break

        # Consume tool_started / tool_completed until the next assistant block.
        while i < n:
            t = events[i].get("type")
            if t == "tool_started":
                new_id = f"toolu_{uuid4().hex[:24]}"
                tool_uses.append({
                    "id": new_id,
                    "name": events[i].get("tool_name", ""),
                    "input": events[i].get("tool_input", {}) or {},
                })
                i += 1
            elif t == "tool_completed":
                tool_results.append({
                    "name": events[i].get("tool_name", ""),
                    "output": events[i].get("output", ""),
                    "is_error": bool(events[i].get("is_error", False)),
                })
                i += 1
            elif t in ("system", "status", "compact_progress", "error"):
                i += 1
            elif t in ("assistant_delta", "assistant_complete"):
                break
            else:
                i += 1  # unknown event; skip

        if text or tool_uses or tool_results:
            turns.append({
                "text": text,
                "tool_uses": tool_uses,
                "tool_results": tool_results,
                "usage": usage,
            })

        # Defensive: avoid infinite loops if no event in this iteration matched.
        if i == progress_anchor:
            i += 1

    return turns


def _result_text(output: Any) -> str:
    if isinstance(output, str):
        return output
    if output is None:
        return ""
    return json.dumps(output, ensure_ascii=False)


def _truncate(s: str, max_len: int = 200) -> str:
    """One-line excerpt for log readability."""
    s = " ".join(s.split())  # collapse whitespace incl. newlines
    return s if len(s) <= max_len else s[:max_len] + "..."


def _brief_args(d: Any, max_len: int = 80) -> str:
    """Compact one-line summary of tool args for logging — mirrors loop.py."""
    try:
        s = json.dumps(d, ensure_ascii=False)
    except Exception:
        s = str(d)
    return s if len(s) <= max_len else s[:max_len] + "..."


class _OHStreamPrinter:
    """Stateful printer that converts streaming OH events into per-turn logs
    and persists each completed turn as one JSONL record.

    Matches the structure ``LoopAgent`` prints (``[turn N] assistant: ...`` /
    ``-> tool: ...`` / ``<- name: OK (Xms)``) so the two agents have parallel
    operator visibility. Token deltas are omitted — stream-json does not carry
    them. Pair latencies come from the dispatch.jsonl side-channel, which lags
    the stream by a few ms; we fall back to ``OK`` / ``ERR`` if we cannot read
    the matching record at print time.

    The printer is fed event-by-event from the subprocess read loop, so
    ``[turn N] assistant: ...`` lands the moment OH closes a turn — same
    real-time experience as LoopAgent.

    Side-effect: appends a JSONL record to ``<LLM_LOG_DIR>/oh_responses.jsonl``
    at the end of each turn (next ``assistant_complete``, or stream close).
    OH owns the per-turn ``llm_requests.jsonl`` (request side); pairing the
    two by ``turn`` gives full request/response visibility.

    ``log_prefix`` controls the bracketed tag printed before each line so the
    base ``OpenHarnessAgent`` and its subclasses can be distinguished in mixed
    logs (e.g. ``[oh]`` vs ``[oh-ext]``).
    """

    def __init__(self, dispatch_log_path: Path, *, log_prefix: str = "[oh]") -> None:
        self._dispatch_log = dispatch_log_path
        self._log_prefix = log_prefix
        self._turn = 0
        self._text_buf = ""
        self._last_dispatch_offset = 0  # bytes read so far from dispatch.jsonl
        self._pending_pairs: dict[str, dict] = {}  # tool_name -> latest record

        # Per-turn buffer; flushed on the NEXT assistant_complete or on close.
        self._cur_turn_record: dict | None = None
        try:
            from claw_anything.llm_logger import _get_log_dir as _claw_log_dir
            self._response_log: Path | None = _claw_log_dir() / "oh_responses.jsonl"
        except Exception:
            # LLM_LOG_DIR unwritable etc. — silently disable the side channel;
            # console + oh_stream.jsonl remain the source of truth.
            self._response_log = None

    # ---- public API ----

    @property
    def turn_count(self) -> int:
        return self._turn

    def feed(self, ev: dict) -> None:
        t = ev.get("type")
        if t == "assistant_delta":
            self._text_buf += ev.get("text", "")
        elif t == "assistant_complete":
            self._flush_turn_record()  # finalize the previous turn, if any
            self._turn += 1
            text = ev.get("text", self._text_buf)
            self._text_buf = ""
            self._cur_turn_record = {
                "turn": self._turn,
                "timestamp": datetime.now().isoformat(),
                "text": text,
                "tool_calls": [],
            }
            _log(f"{self._log_prefix} [turn {self._turn}] assistant")
            if text:
                _log(f"{self._log_prefix}   text: {_truncate(text)}")
        elif t == "tool_started":
            name = ev.get("tool_name", "")
            args = ev.get("tool_input", {}) or {}
            if self._cur_turn_record is not None:
                self._cur_turn_record["tool_calls"].append({
                    "name": name,
                    "input": args,
                    "output": None,
                    "is_error": None,
                    "latency_ms": None,
                })
            _log(f"{self._log_prefix}   -> tool: {name}({_brief_args(args)})")
        elif t == "tool_completed":
            name = ev.get("tool_name", "")
            is_err = bool(ev.get("is_error", False))
            self._refresh_dispatch_log()
            rec = self._pending_pairs.pop(name, None)
            latency = rec.get("latency_ms") if rec else None
            # Pair with the latest in-flight tool_call in the current turn
            # whose name matches and output is still None (FIFO).
            if self._cur_turn_record is not None:
                for call in self._cur_turn_record["tool_calls"]:
                    if call["name"] == name and call["output"] is None:
                        call["output"] = ev.get("output")
                        call["is_error"] = is_err
                        call["latency_ms"] = latency
                        break
            tag = "ERR" if is_err else "OK"
            lat = f" ({latency:.0f}ms)" if isinstance(latency, (int, float)) else ""
            _log(f"{self._log_prefix}   <- {name}: {tag}{lat}")
        elif t == "error":
            _log(f"{self._log_prefix}   ! error: {_truncate(str(ev.get('message', '')))}")
        # system / status / compact_progress — intentionally quiet.

    def close(self) -> None:
        """Flush the trailing turn record. Call once after the read loop exits."""
        self._flush_turn_record()

    # ---- helpers ----

    def _flush_turn_record(self) -> None:
        rec = self._cur_turn_record
        self._cur_turn_record = None
        if rec is None or self._response_log is None:
            return
        try:
            with open(self._response_log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass  # best-effort; stream.jsonl already has the raw events

    def _refresh_dispatch_log(self) -> None:
        """Incrementally read dispatch.jsonl for the latest record per tool.

        Cheap: only reads new bytes since the last call. We index records by
        tool_name (FIFO within a name is good enough for latency display).
        """
        if not self._dispatch_log.exists():
            return
        try:
            with open(self._dispatch_log, "rb") as fh:
                fh.seek(self._last_dispatch_offset)
                chunk = fh.read()
                self._last_dispatch_offset = fh.tell()
        except OSError:
            return
        for raw in chunk.splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            name = rec.get("tool_name")
            if isinstance(name, str):
                self._pending_pairs[name] = rec


class OpenHarnessAgent(BaseAgent):
    """Runs vanilla OpenHarness via the ``oh`` CLI as a claw-anything agent."""

    #: Log prefix used for stdout messages and threaded into ``_OHStreamPrinter``
    #: so mixed logs (e.g. parallel batch with both agents) stay legible.
    LOG_PREFIX: str = "[oh]"

    def __init__(
        self,
        *,
        settings_path: str | Path | None = None,
        disable_builtin_tools: bool = False,
    ) -> None:
        # Everything else OH needs (model, base_url, API key) is read by OH
        # from this settings file — claw-anything does not duplicate it.
        self.settings_path = Path(settings_path) if settings_path else None
        self.disable_builtin_tools = disable_builtin_tools

    # ------------------------------------------------------------------ #
    #  Subclass hooks                                                     #
    # ------------------------------------------------------------------ #

    def builtin_tools_for_deny(self) -> list[str]:
        """Builtin tool names to deny when ``disable_builtin_tools`` is set.

        Vanilla OH returns only the upstream openharness-ai 0.1.7 tools.
        Subclasses (e.g. OH-Ext) extend this with their fork-only additions.
        We never union the two lists at the plugin_gen layer — denying a tool
        that does not exist for the active agent would leak the existence of
        the OH-Ext extensions to the model.
        """
        return OH_BUILTIN_TOOLS

    def print_mode_extra_fields(self) -> list[str]:
        """Opt-in stream-json extra fields this adapter wants OH to emit.

        Vanilla OH (this base class) returns ``[]`` because vanilla OH has
        no ``print_mode`` setting — the field would be ignored at best and
        rejected at worst. Vanilla OH gets ``usage`` via the build-time
        patch shipped in ``docker/oh/patch_print_mode_usage.py`` (which
        unconditionally injects ``usage`` into ``assistant_complete``)
        rather than through this declarative settings path.

        OH-Ext (and any future fork with native ``PrintModeSettings``)
        overrides this to return ``["usage"]`` (plus future identifiers),
        which ``generate_plugin_files`` merges into the per-trial
        ``settings.json`` so OH-Ext's ``run_print_mode`` emits the
        corresponding extra fields.
        """
        return []

    # ------------------------------------------------------------------ #
    #  Core execution                                                     #
    # ------------------------------------------------------------------ #

    def _execute(
        self,
        task: TaskDefinition,
        *,
        provider: OpenAICompatProvider,
        prompt_cfg: PromptConfig | None,
        model_cfg: ModelConfig | None,
        sandbox_tools: bool,
        sandbox_url: str | None,
        workspace_root: Path | None,
        content_blocks: list[ContentBlock],
    ) -> None:
        """Run the ``oh`` subprocess and convert its output into trace events.

        The signature matches ``BaseAgent._execute`` so ``run_task`` can invoke
        every agent uniformly, but OH only uses ``task``. The rest is loop-agent
        plumbing that this agent deliberately ignores:

          - ``prompt_cfg.skill_mode`` is read to decide whether the generated
            plugin uses progressive tool revelation (skill mode).  All other
            ``prompt_cfg`` fields are loop-agent-specific and are ignored here.
          - ``provider`` / ``model_cfg`` drive ``LoopAgent``'s turn-by-turn
            model loop. OH runs its own think-act-observe loop and reads model /
            base-url / API key from its own settings file.
          - ``sandbox_*`` / ``workspace_root`` / ``content_blocks`` feed
            ``LoopAgent``'s in-process tool execution. OH's tools are
            HTTP-backed plugin tools instead.

        Most parameters are kept only to honour the base contract.
        """
        skill_mode = prompt_cfg.skill_mode if prompt_cfg is not None else False
        cfg_root = self._prepare_oh_config(task, skill_mode=skill_mode)
        oh_events, return_code = self._run_oh_subprocess(task, cfg_root)
        self._emit_oh_turns(
            oh_events=oh_events,
            dispatch_log=cfg_root / "dispatch.jsonl",
            return_code=return_code,
        )

    # ------------------------------------------------------------------ #
    #  Step 1 — config dir + generated plugin                             #
    # ------------------------------------------------------------------ #

    def _prepare_oh_config(self, task: TaskDefinition, skill_mode: bool = False) -> Path:
        """Create the OH config dir and generate the per-task ``clawanything`` plugin.

        Returns the config-dir root. All OH artifacts (generated plugin,
        dispatch log, workspace, raw stream) live in this dir, a sibling of the
        JSONL trace written by BaseAgent.
        """
        # BaseAgent.run_task constructs ``_writer`` before invoking ``_execute`` /
        # this helper, so it's always non-None here. Assert for Pylance.
        assert self._writer is not None
        cfg_root = self._writer.path.parent / "oh_cfg"
        cfg_root.mkdir(parents=True, exist_ok=True)
        # OH's cwd is an empty workspace — never the task dir. Pointing OH at
        # the task dir gives the model a backdoor: with builtin tools enabled
        # it just runs ``read_file fixtures/...`` and skips the clawanything tools.
        (cfg_root / "workspace").mkdir(parents=True, exist_ok=True)
        extra_denied = self.builtin_tools_for_deny() if self.disable_builtin_tools else None
        generate_plugin_files(
            task,
            cfg_root / "plugins" / PLUGIN_NAME,
            settings_root=cfg_root,
            extra_denied_tools=extra_denied,
            settings_path=self.settings_path,
            skill_mode=skill_mode,
            print_mode_extra_fields=self.print_mode_extra_fields() or None,
        )
        _log(f"{self.LOG_PREFIX} config dir: {cfg_root}")
        return cfg_root

    def _build_oh_env(self, task: TaskDefinition, cfg_root: Path) -> dict[str, str]:
        """Build the environment for the ``oh`` subprocess.

        Points OH at the per-task config dir and tells the generated plugin
        tools where to append their HTTP dispatch records. The model API key is
        not set here — OH reads it from its settings file.

        ``CLAW_TASK_EXECUTION_DATE`` (when ``task.execution_date`` is set) is
        consumed by the build-time patch baked into claw-anything-oh
        (``docker/oh/patch_environment_date.py``), which rewrites
        ``openharness.prompts.environment.get_environment_info`` to read this
        env var at call time. That keeps the system prompt's ``# Environment``
        date aligned with the task's simulated date instead of the container
        wall clock. OH-Ext uses its own ``settings.prompt_meta.today`` path
        and ignores this env var.
        """
        env = dict(os.environ)
        env["OPENHARNESS_CONFIG_DIR"] = str(cfg_root)
        env["OPENHARNESS_DATA_DIR"] = str(cfg_root / "data")
        env["OPENHARNESS_LOGS_DIR"] = str(cfg_root / "logs")
        # The generated plugin tools append one HTTP dispatch record per call.
        env["CLAW_ANYTHING_DISPATCH_LOG"] = str(cfg_root / "dispatch.jsonl")
        # Mock services bind to localhost — never go through any HTTP proxy.
        env.setdefault("no_proxy", "localhost,127.0.0.1")
        env.setdefault("NO_PROXY", "localhost,127.0.0.1")
        if task.execution_date:
            env["CLAW_TASK_EXECUTION_DATE"] = str(task.execution_date)
        return env

    # ------------------------------------------------------------------ #
    #  Step 2 — run `oh`, capture stream-json                             #
    # ------------------------------------------------------------------ #

    def _run_oh_subprocess(
        self, task: TaskDefinition, cfg_root: Path,
    ) -> tuple[list[dict], int]:
        """Spawn ``oh``, stream its stdout, and return (parsed events, exit code).

        Each stream-json line is written to ``oh_stream.jsonl`` as it arrives,
        so a crash mid-stream still leaves the raw OH output for debugging.
        """
        workspace = cfg_root / "workspace"
        task_timeout_s = int(task.environment.timeout_seconds or _DEFAULT_TASK_TIMEOUT_S)
        kill_deadline_s = task_timeout_s + _OH_SHUTDOWN_GRACE_S

        argv = [
            "oh",
            "-p", task.prompt.text,
            "--output-format", "stream-json",
            "--dangerously-skip-permissions",
            "--max-turns", str(task.environment.max_turns),
            "--cwd", str(workspace),
        ]
        _log(f"{self.LOG_PREFIX} launching: oh -p ... (cwd={workspace})")

        env = self._build_oh_env(task, cfg_root)
        wall_t0 = time.monotonic()
        try:
            proc = subprocess.Popen(
                argv,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "oh CLI not found on PATH. Install OpenHarness (vanilla "
                "`pip install openharness-ai==0.1.7`) or OpenHarnessExtended "
                "(`pip install -e <OpenHarnessExtended-repo>`) and re-run."
            ) from exc

        oh_events: list[dict] = []
        timed_out = False
        kill_at = wall_t0 + kill_deadline_s
        # Real-time per-turn console output (matches LoopAgent's UX) is driven
        # off the same parsed events we keep in ``oh_events`` for the post-hoc
        # trace emission — feed the printer once per line, no extra parsing.
        printer = _OHStreamPrinter(cfg_root / "dispatch.jsonl", log_prefix=self.LOG_PREFIX)
        # Each parsed event is re-dumped with ``ensure_ascii=False`` so the
        # archived stream stays Chinese/CJK-readable instead of inheriting OH's
        # ``\uXXXX`` escapes. Lines that fail to parse are kept verbatim so a
        # debugger can still inspect raw OH output.
        with open(cfg_root / "oh_stream.jsonl", "w", encoding="utf-8") as stream_fh:
            assert proc.stdout is not None
            try:
                for line in proc.stdout:
                    if time.monotonic() > kill_at:
                        proc.kill()
                        timed_out = True
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        stream_fh.write(line + "\n")
                        stream_fh.flush()
                        continue
                    oh_events.append(ev)
                    stream_fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
                    stream_fh.flush()
                    printer.feed(ev)
            finally:
                printer.close()  # flush the last turn's response record
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)

        oh_wall_time_s = time.monotonic() - wall_t0
        return_code = proc.returncode if proc.returncode is not None else -1

        oh_stderr = ""
        try:
            if proc.stderr is not None:
                oh_stderr = proc.stderr.read() or ""
        except Exception:
            pass
        if oh_stderr:
            (cfg_root / "oh_stderr.log").write_text(oh_stderr, encoding="utf-8")

        if timed_out:
            _log(f"{self.LOG_PREFIX} timeout after {kill_deadline_s:.0f}s — killed oh subprocess")
            oh_events.append({
                "type": "error",
                "message": f"oh subprocess killed after {kill_deadline_s:.0f}s timeout",
                "recoverable": False,
            })
        if return_code != 0 and oh_stderr:
            tail = "\n".join(oh_stderr.strip().splitlines()[-10:])
            _log(f"{self.LOG_PREFIX} non-zero exit {return_code}; stderr tail:\n{tail}")
        _log(f"{self.LOG_PREFIX} [end] turns={printer.turn_count} time=wall {oh_wall_time_s:.1f}s exit={return_code}")
        return oh_events, return_code

    # ------------------------------------------------------------------ #
    #  Step 3 — stream-json → trace events                                #
    # ------------------------------------------------------------------ #

    def _emit_oh_turns(
        self,
        *,
        oh_events: list[dict],
        dispatch_log: Path,
        return_code: int,
    ) -> None:
        """Parse OH stream-json turns and report them via ``emit_*``.

        Runs once, after ``oh`` has exited — a turn boundary is only known once
        the next turn begins. BaseAgent has already written TraceStart + the
        initial user prompt message, and writes TraceEnd afterwards, so this
        only emits the assistant / tool-result turns in between. Model time is
        left at zero so BaseAgent derives it as ``wall - tool``.

        Token usage is read from the per-turn ``usage`` field on each
        ``assistant_complete`` event. Vanilla OH carries it via the
        build-time patch (``docker/oh/patch_print_mode_usage.py``); OH-Ext
        emits it natively when ``settings.print_mode.stream_json_extra_fields``
        includes ``"usage"``. If the field is missing on a given turn we
        fall back to ``TokenUsage(0, 0)`` and record a per-turn failure so
        the gap surfaces in the trace.
        """
        dispatches = _read_dispatch_log(dispatch_log)
        turns = _split_turns(oh_events)

        if return_code != 0:
            self.record_failure(f"oh-exit-{return_code}")
        for ev in oh_events:
            if ev.get("type") == "error" and not ev.get("recoverable", True):
                self.record_failure(f"oh-error: {ev.get('message', '')[:200]}")

        for turn in turns:
            text = turn["text"]
            tool_uses = turn["tool_uses"]
            tool_results = turn["tool_results"]
            usage = turn.get("usage")

            # Assistant message (text + tool_use blocks).
            blocks: list[ContentBlock] = []
            if text:
                blocks.append(TextBlock(text=text))
            for tu in tool_uses:
                blocks.append(ToolUseBlock(
                    id=tu["id"],
                    name=tu["name"],
                    input=tu["input"],
                ))
            if usage is None:
                # Patch broke or running against an unpatched OH — canary signal.
                self.record_failure("usage-missing-from-stream-json")
                token_usage = TokenUsage(input_tokens=0, output_tokens=0)
            else:
                token_usage = TokenUsage(
                    input_tokens=int(usage.get("input_tokens", 0) or 0),
                    output_tokens=int(usage.get("output_tokens", 0) or 0),
                )
            self.emit_message(
                Message(role="assistant", content=blocks),
                token_usage,
            )

            # ToolDispatch events + tool-result user message.
            result_blocks: list[ContentBlock] = []
            for idx, tu in enumerate(tool_uses):
                # Pair tool_use to a dispatch.jsonl record (FIFO by tool_name).
                rec = _pop_first_match(dispatches, tu["name"])
                # Pair tool_use to a tool_completed (FIFO over the turn).
                completed = tool_results[idx] if idx < len(tool_results) else None

                if rec is None:
                    # Synthesize from completed/tool_use only.
                    self.record_failure(f"dispatch-missing-{tu['name']}")
                    rec = {
                        "endpoint_url": "",
                        "request_body": tu["input"],
                        "response_status": 200 if (completed and not completed["is_error"]) else 500,
                        "response_body": (completed.get("output") if completed else None),
                        "latency_ms": 0.0,
                    }

                self.emit_tool_dispatch(ToolDispatch(
                    trace_id=self._trace_id,
                    tool_use_id=tu["id"],
                    tool_name=tu["name"],
                    endpoint_url=rec.get("endpoint_url", ""),
                    request_body=rec.get("request_body") or {},
                    response_status=int(rec.get("response_status", 0) or 0),
                    response_body=rec.get("response_body"),
                    latency_ms=float(rec.get("latency_ms", 0.0) or 0.0),
                ))

                output_text = (
                    _result_text(completed["output"]) if completed
                    else _result_text(rec.get("response_body"))
                )
                is_err = bool(completed["is_error"]) if completed else (
                    int(rec.get("response_status", 0) or 0) >= 400
                )
                result_blocks.append(ToolResultBlock(
                    tool_use_id=tu["id"],
                    content=[TextBlock(text=output_text)],
                    is_error=is_err,
                ))

            if result_blocks:
                self.emit_message(Message(role="user", content=result_blocks))

        self.increment_turn_count(len(turns))
