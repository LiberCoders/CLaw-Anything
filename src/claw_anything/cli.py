"""CLI: claw-anything run | grade | list | gen | build-image | batch."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from dataclasses import dataclass
from uuid import uuid4

# Ensure localhost traffic (mock services) bypasses any HTTP proxy,
# while external API requests (OpenRouter etc.) still go through proxy.
os.environ.setdefault("no_proxy", "localhost,127.0.0.1")
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1")


# Gate concurrent ``docker run`` calls — the daemon serialises container
# creation under load (port pool contention + cgroup setup), so an
# unthrottled ``--parallel 32`` storm causes daemon timeouts and orphan
# containers. Env var name and default kept consistent with main's
# ContainerSession so existing tuning advice still applies.
_DOCKER_CREATE_PARALLELISM = int(os.environ.get("CLAW_DOCKER_CREATE_PARALLELISM", "4"))
_DOCKER_CREATE_SEM: asyncio.Semaphore | None = None
_DOCKER_CREATE_SEM_LOOP: asyncio.AbstractEventLoop | None = None


def _get_docker_create_sem() -> asyncio.Semaphore:
    """Lazily create the semaphore inside the running event loop.

    The benchmark suite runs each phase (skill / tool / gui) under its own
    ``asyncio.run()``, i.e. a fresh event loop. An ``asyncio.Semaphore`` is
    bound to the loop that was running when it was created, so a module-level
    cache from phase 1 would raise "bound to a different event loop" on every
    ``docker run`` in phase 2+. Recreate the semaphore whenever the running
    loop changes so each phase gets a loop-local instance.
    """
    global _DOCKER_CREATE_SEM, _DOCKER_CREATE_SEM_LOOP
    loop = asyncio.get_running_loop()
    if _DOCKER_CREATE_SEM is None or _DOCKER_CREATE_SEM_LOOP is not loop:
        _DOCKER_CREATE_SEM = asyncio.Semaphore(_DOCKER_CREATE_PARALLELISM)
        _DOCKER_CREATE_SEM_LOOP = loop
    return _DOCKER_CREATE_SEM


def _is_kimi_model(model_id: str) -> bool:
    return "kimi" in model_id.lower()


def _resolve_task_yaml(task_arg: str) -> Path:
    """Resolve --task to a YAML file path.

    Accepts either a directory (tasks/T01zh_email_triage) or a file (tasks/T01zh_email_triage/task.yaml).
    """
    p = Path(task_arg)
    if p.is_dir():
        yaml_path = p / "task.yaml"
        if not yaml_path.exists():
            raise FileNotFoundError(f"No task.yaml found in {p}")
        return yaml_path
    return p


def _resolve_tasks_dir(task_yaml: Path) -> Path:
    """Given a task YAML path like tasks/T01zh_email_triage/task.yaml, return the tasks/ root dir."""
    # task.yaml is at tasks/<ID>/task.yaml — parent.parent is tasks/
    return task_yaml.parent.parent


#: Default Docker image for each agent backend when ``--docker-image`` is not
#: explicitly passed. Each image is built by the matching ``scripts/build_*.sh``
#: helper (build_oh_image.sh / build_oh_ext_image.sh / build_loop_image.sh).
#: Unknown agent types fall back to the LoopAgent image, which contains only
#: claw-anything itself with no OH dependency.
_AGENT_DEFAULT_IMAGE: dict[str, str] = {
    "openharness": "claw-anything-oh:latest",
    "openharness-ext": "claw-anything-oh-ext:latest",
    "loop": "claw-anything-loop:latest",
    "openai-compat": "claw-anything-loop:latest",
}


def _default_image_for_agent(agent_type: str) -> str:
    """Pick the runner image for an agent when the user did not pass ``--docker-image``."""
    return _AGENT_DEFAULT_IMAGE.get(agent_type, "claw-anything-loop:latest")


def _make_trace_dir(base_dir: str | Path, model_id: str, agent_type: str = "loop") -> Path:
    """Build a trace output directory: ``<base_dir>/<agent_type>_<model>_<YYMMDDHHMM>/``.

    Model names like ``anthropic/claude-opus-4-6`` are sanitised to
    ``anthropic_claude-opus-4-6`` (slashes replaced with underscores).
    """
    from datetime import datetime

    date_str = datetime.now().strftime("%y-%m-%d-%H-%M")
    safe_model = model_id.replace("/", "_")
    trace_dir = Path(base_dir) / f"{agent_type}_{safe_model}_{date_str}"
    trace_dir.mkdir(parents=True, exist_ok=True)
    return trace_dir


def _make_judge(cfg, args):
    """Create an LLMJudge instance if enabled, or None."""
    if getattr(args, "no_judge", False):
        return None
    if not cfg.judge.enabled:
        return None
    # Need at least an API key to use the judge
    api_key = cfg.judge.api_key
    if not api_key:
        return None
    from .graders.llm_judge import LLMJudge

    model_id = getattr(args, "judge_model", None) or cfg.judge.model_id
    return LLMJudge(
        model_id=model_id,
        api_key=api_key,
        base_url=cfg.judge.base_url,
        tls_verify=cfg.judge.tls_verify,
    )


def _apply_proxy(proxy_url: str | None) -> None:
    """Set HTTP(S)_PROXY env vars for model/judge API traffic.

    Mock services are unaffected because ``services.py`` strips proxy vars
    from subprocess environments, and ``no_proxy`` already covers localhost.
    """
    if not proxy_url:
        return
    os.environ["HTTP_PROXY"] = proxy_url
    os.environ["HTTPS_PROXY"] = proxy_url
    os.environ["http_proxy"] = proxy_url
    os.environ["https_proxy"] = proxy_url
    print(f"[proxy] Model/judge traffic via {proxy_url}")


def _grade_with_optional_params(
    grader, messages, dispatches, task,
    *, audit_data, judge,
):
    """Call grader.grade with the shared kwargs every grader accepts."""
    return grader.grade(messages, dispatches, task, audit_data=audit_data, judge=judge)


def _trace_totals(end) -> dict[str, int | float]:
    """Extract model token/time totals from a TraceEnd event."""
    if end is None:
        return {
            "model_input_tokens": 0,
            "model_output_tokens": 0,
            "total_tokens": 0,
            "model_time_s": 0.0,
            "tool_time_s": 0.0,
            "other_time_s": 0.0,
            "wall_time_s": 0.0,
        }

    model_input_tokens = getattr(end, "model_input_tokens", getattr(end, "input_tokens", 0))
    model_output_tokens = getattr(end, "model_output_tokens", getattr(end, "output_tokens", 0))
    total_tokens = getattr(end, "total_tokens", model_input_tokens + model_output_tokens)
    model_time_s = getattr(end, "model_time_s", 0.0)
    tool_time_s = getattr(end, "tool_time_s", 0.0)
    other_time_s = getattr(end, "other_time_s", 0.0)
    wall_time_s = getattr(end, "wall_time_s", 0.0)

    # Backward compatibility for older traces.
    if not total_tokens:
        total_tokens = model_input_tokens + model_output_tokens
    if not other_time_s and wall_time_s:
        other_time_s = max(0.0, wall_time_s - model_time_s - tool_time_s)

    return {
        "model_input_tokens": model_input_tokens,
        "model_output_tokens": model_output_tokens,
        "total_tokens": total_tokens,
        "model_time_s": wall_time_s if not model_time_s and not tool_time_s else model_time_s,
        "tool_time_s": tool_time_s,
        "other_time_s": other_time_s,
        "wall_time_s": wall_time_s,
    }


# --------------------------------------------------------------------------- #
#  Mobile-GUI task helpers                                                     #
# --------------------------------------------------------------------------- #

def _task_needs_gui(task=None, task_dir: str | None = None) -> bool:
    """True when a task declares the mobile_gui environment.

    Pass exactly one of ``task`` (a loaded TaskDefinition) or ``task_dir``
    (a path to a task dir / YAML). When ``task_dir`` is given the task is
    loaded first; a load failure yields False.
    """
    if task is None and task_dir is None:
        print("[WARNING] _task_needs_gui called with neither task nor task_dir")
        return False
    if task_dir is not None:
        from .models.task import TaskDefinition

        try:
            task = TaskDefinition.from_yaml(_resolve_task_yaml(task_dir))
        except Exception:
            return False
    return "mobile_gui" in getattr(task, "task_env", [])


def _init_gui_task(
    task_dir: Path,
    device_serial: str,
    adb_path: str | None = None,
    screenshots_root: str | Path | None = None,
) -> None:
    """Initialise an Android emulator for a mobile_gui task (host-side)."""
    from .task.mobile_gui import init_gui_task as _gui_init

    print(f"[gui-init] initializing {task_dir.name} on {device_serial} ...")
    ok = _gui_init(
        str(task_dir),
        device=device_serial,
        adb_path=adb_path,
        yaml_name="task.yaml",
        screenshots_root=screenshots_root,
    )
    if not ok:
        raise RuntimeError(f"init_gui_task failed for {task_dir.name} on {device_serial}")


def _task_out_dir(trace_dir: str | Path | None, task_id: str, trace_id: str) -> Path:
    """The single per-trial output dir: ``<trace_dir>/<task_id>_<trace_id8>/``."""
    base = Path(trace_dir) if trace_dir else Path.cwd()
    out = base / f"{task_id}_{trace_id[:8]}"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _gui_init_artifacts_dir(trace_dir: str | Path | None, task_id: str, trace_id: str) -> Path:
    """Return ``<trace_dir>/<task_id>_<trace_id8>/gui_init_artifacts/``, creating it."""
    out = _task_out_dir(trace_dir, task_id, trace_id) / "gui_init_artifacts"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _workspace_dir(trace_dir: str | Path | None, task_id: str, trace_id: str) -> Path:
    """Return ``<trace_dir>/<task_id>_<trace_id8>/workspace/``, creating it.

    Host-prepared, agent-writable workspace for one trial. The agent's
    ``sandbox_*`` tools resolve relative paths against this dir; in
    trial-in-container mode it is mounted into the container as ``/workspace``.
    Kept on disk after the trial for inspection — same pattern as
    ``gui_init_artifacts`` and the ``oh_cfg`` dir.
    """
    out = _task_out_dir(trace_dir, task_id, trace_id) / "workspace"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _task_snapshot_dir(trace_dir: str | Path | None, task_id: str, trace_id: str) -> Path:
    """Return ``<trace_dir>/<task_id>_<trace_id8>/task_snapshot/``, creating it.

    A writable per-trial copy of ``task_dir``, mounted into the trial
    container in place of the original ``task_dir``. The inner stage deletes
    its ``fixtures/`` subtree after mock services have loaded their data so
    the agent cannot bypass the service-API boundary by ``cat``-ing the raw
    JSON via ``sandbox_shell_exec``. Local mode bypasses this snapshot —
    a host-side agent already has full filesystem access anyway.
    """
    out = _task_out_dir(trace_dir, task_id, trace_id) / "task_snapshot"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _prepare_task_snapshot(task_dir: Path, dest_dir: Path) -> None:
    """Stage the minimum the inner trial-container actually needs.

    Only ``task.yaml`` (the inner cli reads it once via ``--task`` at startup
    and the inner stage unlinks it post-load — see the redaction block in
    ``_run_one_trial`` ) and ``fixtures/`` (mock services load them at
    startup, then the inner stage rmtree's the dir once they're healthy) are
    copied. Everything else — ``grader.py``, ``screenshots/``,
    ``sandbox_grader_files`` — stays on the host: graders run host-side
    against the original ``task_dir``, and any grader-side answer files must
    never reach a path the in-container agent could read.
    """
    import shutil

    src_task_yaml = task_dir / "task.yaml"
    if src_task_yaml.exists():
        shutil.copyfile(src_task_yaml, dest_dir / "task.yaml")
    src_fixtures = task_dir / "fixtures"
    if src_fixtures.exists():
        shutil.copytree(src_fixtures, dest_dir / "fixtures", dirs_exist_ok=True, symlinks=True)


def _prepare_workspace(task_dir: Path, task, dest_dir: Path) -> int:
    """Stage task-declared inputs into ``dest_dir`` for the agent to read.

    The agent runs against ``dest_dir`` (its ``sandbox_*`` tools resolve
    relative paths there), so this is the moment the host decides what the
    agent gets to see. The contract — picked up from main's
    ``SandboxRunner.inject_files`` — is:

      - Copy every path in ``task.sandbox_files`` (the author-curated list of
        files the task expects the agent to read, e.g. ``fixtures/foo.csv``).
      - Copy ``task_dir/logs/*.md`` (excluding ``work_journal.md``, which is
        a meta-narrative artifact for grading and must not leak to the
        agent). The system prompt's "Activity Logs" section advertises these
        files, so skipping them would make the prompt point at files that
        don't exist.

    Explicitly NOT copied:

      - ``task.sandbox_grader_files`` — verify scripts / answer-bearing
        artifacts. Grading runs host-side against ``task_dir`` directly, so
        these never need to enter the agent's workspace.
      - ``task.environment.fixtures`` — mock-service backing data, loaded
        by services host-side via env vars. Surfacing them as files would
        let an agent with fs tools bypass the service-API boundary.

    Returns the number of files copied.
    """
    import shutil

    dest_dir.mkdir(parents=True, exist_ok=True)
    count = 0

    for rel in (task.sandbox_files or []):
        src = task_dir / rel
        if not src.exists():
            print(f"[workspace] skip missing sandbox_file: {rel}", flush=True)
            continue
        out = dest_dir / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)
        count += 1

    logs_src = task_dir / "logs"
    if logs_src.exists() and logs_src.is_dir():
        for log in sorted(logs_src.rglob("*.md")):
            if log.name == "work_journal.md":
                continue
            if log.stat().st_size == 0:
                continue
            rel = log.relative_to(task_dir)
            out = dest_dir / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(log, out)
            count += 1

    return count


def _read_info_from_oh_settings(oh_settings_path: str | None, info_name: str) -> str | None:
    """Read a known mobile_gui field from an oh-settings JSON file, or None.

    ``info_name`` must be ``"device_serial"`` or ``"adb_path"`` — both live
    under the ``mobile_gui`` key. Callers name the info they want, not the
    JSON path, so the oh-settings layout stays encapsulated here. Returns None
    on an unknown ``info_name``, a missing path, an unreadable file, or
    invalid JSON.
    """
    if info_name not in ("device_serial", "adb_path"):
        print(f"[WARNING] _read_info_from_oh_settings: unknown info_name {info_name!r}")
        return None
    if not oh_settings_path:
        return None
    try:
        data = json.loads(Path(oh_settings_path).read_text(encoding="utf-8"))
        return data.get("mobile_gui", {}).get(info_name)
    except Exception:
        return None


def _resolve_device_pool(cfg, oh_settings_path: str | None) -> list[str]:
    """Devices claw-anything may allocate to mobile_gui tasks, in priority order.

    ``config.android.emulator_pool`` wins; if empty, fall back to the single
    device pinned in ``--oh-settings`` (mobile_gui.device_serial). An empty
    list means no device is available — callers must error out.
    """
    if cfg.android.emulator_pool:
        return list(cfg.android.emulator_pool)
    s = _read_info_from_oh_settings(oh_settings_path, "device_serial")
    return [s] if s else []


def _pin_device_in_oh_settings(
    oh_settings_path: str | None,
    dest_dir: Path,
    *,
    device_serial: str,
) -> str:
    """Write a per-task oh-settings copy with ``mobile_gui.device_serial`` pinned.

    OpenHarnessExtended (mobile_gui plugin) only learns which device to drive
    from its settings template, so claw-anything's pool allocation must be
    injected here. Vanilla openharness has no ``mobile_gui`` schema, so this
    helper has no effect for it and is never called on that path.

    Starts from ``{}`` when no template was provided. Returns the copy's path.
    The container path does the same pin (plus localhost→host.docker.internal
    rewrites) inside ``container_launcher._prepare_oh_settings_for_container``;
    this helper is the local-mode counterpart.
    """
    try:
        raw = (
            json.loads(Path(oh_settings_path).read_text(encoding="utf-8"))
            if oh_settings_path
            else {}
        )
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    raw.setdefault("mobile_gui", {})["device_serial"] = device_serial
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "oh-settings.json"
    dest.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(dest)


@dataclass(frozen=True)
class TrialRunSpec:
    """Run-invariant CLI inputs for one CLI invocation's trials, frozen.

    Built once by cmd_run / cmd_batch and threaded through _run_one_trial /
    _run_task_all_trials_async. Per-task data (task, task_dir, device_serial,
    trials) stays out — those vary per task / per coroutine and sharing them
    via a frozen object would either break frozenness or cause concurrent
    overwrites under cmd_batch's asyncio parallelism. Parsed-result objects
    (cfg, judge, model_id, trace_dir as Path) also stay out — cmd_batch
    defers load_config to inside _run_task_all_trials_async (each task gets
    its own mutable cfg copy to avoid concurrent .prompt.* writes), so the
    spec cannot hold them at construction time.
    """

    config_path: str | None
    model: str | None
    api_key: str | None
    base_url: str | None
    trace_dir: str | None
    agent_type: str
    trial_in_container: bool
    no_judge: bool
    judge_model: str | None
    oh_settings: str | None
    docker_image: str | None
    oh_disable_builtin_tools: bool
    proxy: str | None
    skill_mode_override: bool | None


async def _run_one_trial(
    spec: TrialRunSpec,
    *,
    cfg,
    judge,
    model_id: str,
    trace_dir: Path,
    task,
    task_dir: Path,
    tasks_dir: Path,
    device_serial: str | None = None,
    quiet: bool = False,
) -> dict:
    """Run one trial — local or trial-in-container — then grade it.

    ``task`` is deep-copied so each trial is independent. In local mode the
    agent runs in-process and mock services are spawned as subprocesses by
    ``ServiceManager``; ``--trial-in-container`` instead runs the trial inside
    an isolated Docker container (the single sandbox mechanism). Returns a full
    per-trial result dict.
    """
    from .agents import make_agent
    from .graders.registry import get_grader
    from .models.scoring import compute_task_score, is_pass
    from .runner.providers.openai_compat import OpenAICompatProvider
    from .runner.services import ServiceManager
    from .runner.container_launcher import is_inside_trial_container
    from .trace.reader import load_trace

    task_copy = copy.deepcopy(task)
    trial_trace_id = str(uuid4())
    inside_tc = is_inside_trial_container()
    # Pre-declare so the linter doesn't flag the cross-branch reads below as
    # unbound. Each variable is only read on a path that also assigned it
    # (host_workspace whenever ``not inside_tc``; host_task_snapshot only
    # when ``not inside_tc and trial_in_container``), but Pylance can't see
    # through that.
    host_workspace: Path | None = None
    host_task_snapshot: Path | None = None

    # GUI init runs host-side, before the trial, against the allocated device.
    # The inner trial-container stage skips it (the host already did it).
    if device_serial and not inside_tc:
        _init_gui_task(
            task_dir,
            device_serial,
            adb_path=_read_info_from_oh_settings(spec.oh_settings, "adb_path"),
            screenshots_root=_gui_init_artifacts_dir(
                trace_dir, task_copy.task_id, trial_trace_id
            ),
        )

    # Host-side workspace + task-snapshot prep — only the host stage runs it.
    # The inner trial-container stage reads the host-prepared dirs via mounts
    # and must not re-prep (and could not: task_copy references container
    # paths, not host paths).
    if not inside_tc:
        host_workspace = _workspace_dir(trace_dir, task_copy.task_id, trial_trace_id)
        _prepare_workspace(task_dir, task_copy, host_workspace)
        if spec.trial_in_container:
            host_task_snapshot = _task_snapshot_dir(
                trace_dir, task_copy.task_id, trial_trace_id
            )
            _prepare_task_snapshot(task_dir, host_task_snapshot)

    if spec.trial_in_container and not inside_tc:
        # ---- Trial-in-container: the single sandbox mechanism ----
        from .runner.container_launcher import TrialLaunchSpec, run_trial_in_container

        # The `not inside_tc and trial_in_container` block above always
        # assigns both — assert so Pylance narrows away the None branch.
        assert host_task_snapshot is not None and host_workspace is not None
        # Resolve the image: explicit --docker-image wins, otherwise pick the
        # default for the active agent (loop / openharness / openharness-ext
        # have separate images so the OH-Ext-specific deps don't bloat the
        # loop runner, and the vanilla-OH image doesn't ship adb).
        effective_image = spec.docker_image or _default_image_for_agent(spec.agent_type)
        launch_spec = TrialLaunchSpec(
            task_id=task_copy.task_id,
            task_dir=host_task_snapshot,
            trace_dir=Path(trace_dir),
            trace_id=trial_trace_id,
            workspace_dir=host_workspace,
            image=effective_image,
            agent=spec.agent_type,
            config_path=Path(spec.config_path) if spec.config_path else None,
            oh_settings_path=Path(spec.oh_settings) if spec.oh_settings else None,
            disable_builtin_tools=spec.oh_disable_builtin_tools,
            device_serial=device_serial,
            timeout_seconds=task_copy.environment.timeout_seconds,
        )
        sem = _get_docker_create_sem()
        async with sem:
            trace_path, openai_data = await asyncio.to_thread(
                run_trial_in_container,
                launch_spec,
                container_cfg=cfg.container,
            )
    else:
        # ---- Local mode: agent in-process, mock services as subprocesses ----
        # (This branch is also the inner stage inside a trial container.)
        provider = OpenAICompatProvider(
            model_id=model_id,
            api_key=spec.api_key or cfg.model.api_key,
            base_url=spec.base_url or cfg.model.base_url,
            extra_body=cfg.model.extra_body,
            text_tool_call_mode=cfg.prompt.text_tool_call_mode,
            tls_verify=cfg.model.tls_verify,
        )
        # oh-settings is consumed by both OH agents (vanilla openharness and
        # openharness-ext) — it carries model / base-url / api_key for the
        # spawned ``oh`` subprocess. Other agents (loop) never read it. Only
        # openharness-ext drives a mobile device, so the device_serial pin
        # only applies there.
        settings_for_agent = None
        if spec.agent_type in ("openharness", "openharness-ext"):
            settings_for_agent = spec.oh_settings
            if spec.agent_type == "openharness-ext" and device_serial and not inside_tc:
                settings_for_agent = _pin_device_in_oh_settings(
                    spec.oh_settings,
                    _task_out_dir(trace_dir, task_copy.task_id, trial_trace_id),
                    device_serial=device_serial,
                )
        agent = make_agent(
            cfg,
            SimpleNamespace(
                agent=spec.agent_type,
                api_key=spec.api_key,
                base_url=spec.base_url,
                oh_settings=settings_for_agent,
                oh_disable_builtin_tools=spec.oh_disable_builtin_tools,
            ),
            model_id,
        )
        # workspace_root pinning:
        #   - inner stage: /workspace (the host-mounted dir; hard-wired, the
        #     launcher always mounts there).
        #   - standalone host: the per-trial dir we just prepared above. This
        #     path is host-only and never crosses into a container.
        # sandbox_tools is gated on LoopAgent — only it consumes them; OH-ext
        # has its own plugin tools and would just ignore the params.
        if inside_tc:
            workspace_root = Path("/workspace")
        else:
            assert host_workspace is not None  # the not-inside_tc block above set it
            workspace_root = host_workspace
        with ServiceManager(
            task_copy.services,
            execution_date=task_copy.execution_date,
            task_dir=Path(task_dir),
        ):
            # Inner trial-container stage only: now that every mock service
            # is healthy (and has loaded its fixtures into memory) and
            # ``task_copy`` already holds the parsed TaskDefinition, wipe both
            # ``fixtures/`` and ``task.yaml`` from the mounted snapshot — the
            # former so the agent can't ``cat`` raw fixture JSON via
            # sandbox_shell_exec, the latter because task.yaml carries the
            # grader rubric / scoring criteria / safety patterns. The snapshot
            # dir itself stays (removing it would break the mount). Local-mode
            # host has full fs access anyway, so the same redaction isn't a
            # meaningful boundary there.
            if inside_tc:
                import shutil
                fixtures_dir = task_dir / "fixtures"
                if fixtures_dir.exists():
                    try:
                        shutil.rmtree(fixtures_dir)
                        print(f"[sandbox] redacted {fixtures_dir} post-service-start", flush=True)
                    except OSError as exc:
                        print(f"[sandbox] WARN: could not redact fixtures: {exc}", flush=True)
                task_yaml_path = task_dir / "task.yaml"
                if task_yaml_path.exists():
                    try:
                        task_yaml_path.unlink()
                        print(f"[sandbox] redacted {task_yaml_path} post-load", flush=True)
                    except OSError as exc:
                        print(f"[sandbox] WARN: could not redact task.yaml: {exc}", flush=True)
            trace_path, openai_data = await asyncio.to_thread(
                agent.run_task,
                task_copy,
                trace_dir=trace_dir,
                provider=provider,
                prompt_cfg=cfg.prompt,
                model_cfg=cfg.model,
                task_dir=str(task_dir),
                sandbox_tools=(spec.agent_type == "loop"),
                # Always None today — sandbox tools run in-process via the
                # local handler. Threaded explicitly so the day we wire up
                # a split-process sandbox the change is one line, not a
                # signature audit.
                sandbox_url=None,
                workspace_root=workspace_root,
                agent_type=spec.agent_type,
                trace_id=trial_trace_id,
            )

    # Grading runs HOST-side only — never inside a trial container. Skipping
    # the inner stage's grade pass has two reasons:
    #   1. The outer host re-grades from the same trace afterwards, so an
    #      in-container pass is wasted work.
    #   2. ``sandbox_grader_files`` (verify scripts with embedded answers)
    #      live on the host's task_dir and are deliberately NOT copied into
    #      ``/workspace``. If grading also ran in-container, those files would
    #      have to be mounted in too — and then the agent could read them.
    # The inner cmd_run consumes ``trace`` and ``task_score`` (the latter only
    # in the trials>1 aggregation, which inner stage never enters — it always
    # runs exactly one trial). Everything else is for the host-side summary
    # which the host re-derives from the trace file. Use None — not 0 — for
    # the unmeasured fields so any accidental consumer (e.g. ``sum()`` of
    # tokens, ``f"{score:.2f}"``) blows up loudly instead of silently mixing a
    # fake zero into real measurements.
    if inside_tc:
        # The inner trial-container stage returns here and never reaches the
        # host-side _write_openai_trace below, so write the OpenAI-format dump
        # now (messages/tools, plus the system row for agents that set it). The
        # host reads this file back, then re-grades and overwrites only the
        # metadata block — messages/tools/system survive. Without this write the
        # host reads an empty file and the final openai.json keeps just metadata.
        _write_openai_trace(
            trace_path, openai_data,
            task_id=task_copy.task_id,
            model=model_id,
            trace_id=trial_trace_id,
        )
        return {
            "trace": str(trace_path),
            "openai_trace": None,
            "model_input_tokens": None,
            "model_output_tokens": None,
            "input_tokens": None,
            "output_tokens": None,
            "tokens": None,
            "model_time_s": None,
            "tool_time_s": None,
            "other_time_s": None,
            "wall_time_s": None,
            "completion": None,
            "robustness": None,
            "communication": None,
            "safety": None,
            "task_score": None,
            "passed": None,
        }

    start, messages, dispatches, end, audit_data = load_trace(trace_path)
    grader = get_grader(task_copy.task_id, tasks_dir=tasks_dir, task_dir=task_dir)
    scores = _grade_with_optional_params(
        grader, messages, dispatches, task_copy,
        audit_data=audit_data, judge=judge,
    )
    task_score = compute_task_score(scores)
    passed = is_pass(task_score)
    _append_grading_to_trace(
        trace_path,
        trace_id=start.trace_id,
        task_id=task_copy.task_id,
        scores=scores,
        task_score=task_score,
        passed=passed,
    )
    openai_path = _write_openai_trace(
        trace_path, openai_data,
        task_id=task_copy.task_id,
        model=model_id,
        trace_id=start.trace_id,
        scores=scores,
        task_score=task_score,
        passed=passed,
    )
    totals = _trace_totals(end)

    if not quiet:
        print(f"Trace: {trace_path}")
        print(f"  OpenAI trace: {openai_path}")
        print(f"  completion:     {scores.completion:.2f}")
        print(f"  robustness:     {scores.robustness:.2f}")
        print(f"  communication:  {scores.communication:.2f}")
        print(f"  safety:         {scores.safety:.1f}")
        for _r in getattr(scores, "rule_results", []) or []:
            _tag = "✓" if _r.get("passed") else "✗"
            print(
                f"  [{_tag}] {_r.get('name'):<18}"
                f" score={_r.get('score', 0.0):.2f}  {_r.get('detail', '')}"
            )
        for _r in getattr(scores, "answer_sheet_results", []) or []:
            _tag = "✓" if _r.get("passed") else "✗"
            print(
                f"  [{_tag}] sheet.{_r.get('id', '?'):<14}"
                f" score={_r.get('score', 0.0):.2f}  {_r.get('detail', '')[:80]}"
            )
        print(f"  task_score:     {task_score:.2f}")
        print(f"  passed:         {passed}")
        print(
            f"  model_tokens:   {totals['total_tokens']} "
            f"({totals['model_input_tokens']} in / {totals['model_output_tokens']} out)"
        )
        print(
            f"  time_s:         wall={totals['wall_time_s']:.2f} "
            f"model={totals['model_time_s']:.2f} tool={totals['tool_time_s']:.2f} "
            f"other={totals['other_time_s']:.2f}"
        )

    return {
        "trace": str(trace_path),
        "openai_trace": str(openai_path),
        "model_input_tokens": totals["model_input_tokens"],
        "model_output_tokens": totals["model_output_tokens"],
        "input_tokens": totals["model_input_tokens"],
        "output_tokens": totals["model_output_tokens"],
        "tokens": totals["total_tokens"],
        "model_time_s": totals["model_time_s"],
        "tool_time_s": totals["tool_time_s"],
        "other_time_s": totals["other_time_s"],
        "wall_time_s": totals["wall_time_s"],
        "completion": scores.completion,
        "robustness": scores.robustness,
        "communication": scores.communication,
        "safety": scores.safety,
        "task_score": task_score,
        "passed": passed,
        "rule_results": getattr(scores, "rule_results", []) or [],
        "answer_sheet_results": getattr(scores, "answer_sheet_results", []) or [],
        "filled_answer_sheet": getattr(scores, "filled_answer_sheet", {}) or {},
    }


def cmd_run(args: argparse.Namespace) -> None:
    """Run an agent on a task.

    Local mode (default) runs the agent in-process with mock services as
    subprocesses. ``--trial-in-container`` runs each trial inside an isolated
    Docker container — the single sandbox mechanism.
    """
    _apply_proxy(getattr(args, "proxy", None))

    from .config import load_config
    from .models.scoring import compute_pass_at_k, compute_pass_hat_k, is_pass
    from .models.task import TaskDefinition
    from .runner.container_launcher import is_inside_trial_container

    cfg = load_config(args.config)

    task_yaml = _resolve_task_yaml(args.task)
    task = TaskDefinition.from_yaml(task_yaml)
    tasks_dir = _resolve_tasks_dir(task_yaml)
    task_dir = task_yaml.parent

    # Resolve model_id early (used for trace dir naming)
    model_id = args.model or cfg.model.model_id
    base_trace_dir = args.trace_dir or cfg.defaults.trace_dir
    agent_type = getattr(args, "agent", None) or cfg.agent.agent_type

    inside_tc = is_inside_trial_container()
    trial_in_container = getattr(args, "trial_in_container", False)
    if trial_in_container and inside_tc:
        print("[ERROR] --trial-in-container set while already inside a trial "
              "container — these are mutually exclusive.")
        sys.exit(2)

    # Inside the trial container, --trace-dir (/out) is already the per-trial
    # dir; otherwise build <base>/<agent>_<model>_<date>/.
    if inside_tc:
        trace_dir = Path(base_trace_dir)
        trace_dir.mkdir(parents=True, exist_ok=True)
    else:
        trace_dir = _make_trace_dir(base_trace_dir, model_id, agent_type)

    os.environ["CLAW_ANYTHING_LLM_LOG_DIR"] = str(trace_dir / "llm_logs")

    mode = "trial-in-container" if trial_in_container else "local"
    print(f"Agent: {agent_type} | Model: {model_id} | mode: {mode}")

    if trial_in_container and not args.config:
        print("[WARNING] --trial-in-container without --config: the container "
              "will not receive your model/judge API settings.")

    if _is_kimi_model(model_id):
        cfg.prompt.text_tool_call_mode = True

    spec = TrialRunSpec(
        config_path=args.config,
        model=args.model,
        api_key=args.api_key,
        base_url=args.base_url,
        trace_dir=args.trace_dir,
        agent_type=agent_type,
        trial_in_container=trial_in_container,
        no_judge=getattr(args, "no_judge", False),
        judge_model=getattr(args, "judge_model", None),
        oh_settings=getattr(args, "oh_settings", None),
        docker_image=getattr(args, "docker_image", None),
        oh_disable_builtin_tools=getattr(args, "oh_disable_builtin_tools", False),
        proxy=getattr(args, "proxy", None),
        skill_mode_override=None,
    )
    judge = _make_judge(cfg, args)

    # Resolve a GUI device for mobile_gui tasks (pool-first; a single run takes
    # pool[0] — no parallelism here, so no occupancy tracking is needed). If
    # ``emulator_pool`` is empty and ``auto_launch_count > 0``, spin up a single
    # auto-launched emulator container for the duration of this run; the
    # try/finally below tears it down. ``--no-auto-launch`` forces "use only
    # statically-listed devices".
    device_serial: str | None = None
    emulator_pool = None  # EmulatorPool | RedroidPool, set on auto-launch below
    no_auto_launch = getattr(args, "no_auto_launch", False)
    if _task_needs_gui(task=task) and not inside_tc:
        static_pool = _resolve_device_pool(cfg, spec.oh_settings)
        if static_pool:
            device_serial = static_pool[0]
        elif not no_auto_launch and cfg.android.auto_launch_count > 0:
            from .runner.emulator_pool import make_android_pool
            emulator_pool = make_android_pool(cfg.android, size=1)
            device_serial = emulator_pool.start_all()[0]
        else:
            raise RuntimeError(
                f"Task {task.task_id} requires mobile_gui but no device is "
                "available: android.emulator_pool is empty in config.yaml, "
                "android.auto_launch_count is 0 (or --no-auto-launch was passed), "
                "and no mobile_gui.device_serial found in --oh-settings."
            )
        print(f"[gui] using device: {device_serial}")

    trials = args.trials or 1
    trial_scores: list[float] = []
    trace_paths: list[Path] = []

    async def _run_all() -> None:
        for i in range(trials):
            if trials > 1:
                print(f"\n--- Trial {i + 1}/{trials} ---")
            tr = await _run_one_trial(
                spec,
                cfg=cfg, judge=judge, model_id=model_id, trace_dir=trace_dir,
                task=task, task_dir=task_dir, tasks_dir=tasks_dir,
                device_serial=device_serial,
            )
            trace_paths.append(Path(tr["trace"]))
            trial_scores.append(tr["task_score"])

    try:
        asyncio.run(_run_all())
    finally:
        if emulator_pool is not None:
            emulator_pool.stop_all()

    if trials > 1:
        print(f"\n--- Multi-trial summary ({trials} trials) ---")
        for i, (score, path) in enumerate(zip(trial_scores, trace_paths)):
            print(f"  Trial {i+1}: score={score:.2f} pass={is_pass(score)} trace={path}")
        pass_at_1 = compute_pass_at_k(trial_scores, k=1)
        pass_hat_k = compute_pass_hat_k(trial_scores, k=trials)
        print(f"  pass@1:  {pass_at_1:.3f}")
        print(f"  pass^{trials}:  {pass_hat_k:.3f}")


_BUILD_IMAGE_AGENTS: dict[str, tuple[str, str]] = {
    "loop":            ("build_loop_image.sh",    "claw-anything-loop:latest"),
    "openharness":     ("build_oh_image.sh",      "claw-anything-oh:latest"),
    "openharness-ext": ("build_oh_ext_image.sh",  "claw-anything-oh-ext:latest"),
}


def cmd_build_image(args: argparse.Namespace) -> None:
    """Build the trial-in-container runner image for the selected agent backend."""
    import subprocess

    agent = args.agent
    script_name, default_image = _BUILD_IMAGE_AGENTS[agent]
    repo_root = Path(__file__).resolve().parents[2]
    script = repo_root / "scripts" / script_name
    if not script.exists():
        print(f"[build-image] build script not found: {script}")
        sys.exit(1)
    image = args.image or default_image
    print(f"[build-image] agent={agent}  script={script.name}  image={image}")
    sys.exit(subprocess.run(["bash", str(script), image]).returncode)


def cmd_grade(args: argparse.Namespace) -> None:
    """Grade an existing trace file."""
    _apply_proxy(getattr(args, "proxy", None))

    from .config import load_config
    from .graders.registry import get_grader
    from .models.scoring import compute_task_score, is_pass
    from .models.task import TaskDefinition
    from .trace.reader import load_trace

    cfg = load_config(args.config if hasattr(args, "config") else None)
    judge = _make_judge(cfg, args)

    start, messages, dispatches, end, audit_data = load_trace(args.trace)

    task_yaml = _resolve_task_yaml(args.task)
    task = TaskDefinition.from_yaml(task_yaml)
    tasks_dir = _resolve_tasks_dir(task_yaml)

    grader = get_grader(task.task_id, tasks_dir=tasks_dir, task_dir=task_yaml.parent)
    scores = _grade_with_optional_params(
        grader, messages, dispatches, task,
        audit_data=audit_data, judge=judge,
    )
    task_score = compute_task_score(scores)
    passed = is_pass(task_score)

    print(f"Trace:   {args.trace}")
    print(f"Task:    {task.task_id} ({task.task_name})")
    print(f"Model:   {start.model}")
    print(f"Turns:   {end.total_turns if end else '?'}")
    totals = _trace_totals(end)
    print(
        f"Tokens:  {totals['total_tokens']} "
        f"({totals['model_input_tokens']} in / {totals['model_output_tokens']} out)"
    )
    print(
        f"Time:    wall={totals['wall_time_s']:.2f}s "
        f"model={totals['model_time_s']:.2f}s "
        f"tool={totals['tool_time_s']:.2f}s "
        f"other={totals['other_time_s']:.2f}s"
    )
    print()
    print(f"completion:     {scores.completion:.2f}")
    print(f"robustness:     {scores.robustness:.2f}")
    print(f"communication:  {scores.communication:.2f}")
    for _r in getattr(scores, "rule_results", []) or []:
        _tag = "✓" if _r.get("passed") else "✗"
        print(
            f"  [{_tag}] {_r.get('name'):<18}"
            f" score={_r.get('score', 0.0):.2f}  {_r.get('detail', '')}"
        )
    for _r in getattr(scores, "answer_sheet_results", []) or []:
        _tag = "✓" if _r.get("passed") else "✗"
        print(
            f"  [{_tag}] sheet.{_r.get('id', '?'):<14}"
            f" score={_r.get('score', 0.0):.2f}  {_r.get('detail', '')[:80]}"
        )
    print(f"safety:         {scores.safety:.1f}")
    print(f"task_score:     {task_score:.2f}")
    print(f"passed:         {passed}")


def _append_grading_to_trace(
    trace_path: Path,
    trace_id: str,
    task_id: str,
    scores,
    task_score: float,
    passed: bool,
) -> None:
    """Append a grading_result event to the end of a trace JSONL file."""
    from .models.trace import GradingResult, DimensionScores

    rule_results = getattr(scores, "rule_results", []) or []
    answer_sheet_results = getattr(scores, "answer_sheet_results", []) or []
    filled_answer_sheet = getattr(scores, "filled_answer_sheet", {}) or {}
    event = GradingResult(
        trace_id=trace_id,
        task_id=task_id,
        scores=DimensionScores(
            completion=scores.completion,
            robustness=scores.robustness,
            communication=scores.communication,
            safety=scores.safety,
            rule_results=rule_results,
            answer_sheet_results=answer_sheet_results,
            filled_answer_sheet=filled_answer_sheet,
        ),
        task_score=task_score,
        passed=passed,
        rule_results=rule_results,
        answer_sheet_results=answer_sheet_results,
        filled_answer_sheet=filled_answer_sheet,
    )
    with open(trace_path, "a") as fh:
        fh.write(event.model_dump_json() + "\n")


def _write_openai_trace(
    trace_path: Path,
    openai_data: dict,
    *,
    task_id: str,
    model: str,
    trace_id: str,
    scores=None,
    task_score: float = 0.0,
    passed: bool = False,
) -> Path:
    """Write OpenAI-format trace JSON alongside the JSONL trace."""
    import json as _json

    metadata: dict = {
        "trace_id": trace_id,
        "task_id": task_id,
        "model": model,
        "task_score": task_score,
        "passed": passed,
    }
    if scores is not None:
        metadata["scores"] = {
            "completion": scores.completion,
            "robustness": scores.robustness,
            "communication": scores.communication,
            "safety": scores.safety,
        }
    openai_data["metadata"] = metadata

    # Replace .jsonl with .openai.json
    openai_path = trace_path.with_suffix("").with_suffix(".openai.json")
    with open(openai_path, "w", encoding="utf-8") as f:
        _json.dump(openai_data, f, indent=2, ensure_ascii=False)
    return openai_path


def _scan_completed_trials(trace_dir: Path) -> dict[str, int]:
    """Scan a trace directory and return {task_id: completed_trial_count}.

    A trial is considered complete if its JSONL file contains a grading_result event.
    """
    from collections import defaultdict

    completed: dict[str, int] = defaultdict(int)
    for f in trace_dir.glob("*.jsonl"):
        with open(f) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "grading_result":
                    task_id = ev.get("task_id", "")
                    if task_id:
                        completed[task_id] += 1
                    break  # one grading_result per file is enough
    return dict(completed)


def _load_completed_results(trace_dir: Path) -> list[dict]:
    """Load per-trial results from grading_result events in a trace directory.

    Returns a list of result dicts (one per task_id) with trials populated from
    the grading_result events found in JSONL files. This allows merging with
    new results when using --continue.
    """
    from collections import defaultdict

    # task_id -> list of trial info dicts
    task_trials: dict[str, list[dict]] = defaultdict(list)

    for f in sorted(trace_dir.glob("*.jsonl")):
        grading = None
        trace_end = None
        for line_str in open(f):
            line_str = line_str.strip()
            if not line_str:
                continue
            try:
                ev = json.loads(line_str)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "grading_result":
                grading = ev
            elif ev.get("type") == "trace_end":
                trace_end = ev

        if grading is None:
            continue

        task_id = grading.get("task_id", "")
        if not task_id:
            continue

        scores = grading.get("scores", {})
        trial_info = {
            "trace": str(f),
            "model_input_tokens": trace_end.get("model_input_tokens", 0) if trace_end else 0,
            "model_output_tokens": trace_end.get("model_output_tokens", 0) if trace_end else 0,
            "input_tokens": trace_end.get("model_input_tokens", 0) if trace_end else 0,
            "output_tokens": trace_end.get("model_output_tokens", 0) if trace_end else 0,
            "tokens": trace_end.get("total_tokens", 0) if trace_end else 0,
            "model_time_s": trace_end.get("model_time_s", 0.0) if trace_end else 0.0,
            "tool_time_s": trace_end.get("tool_time_s", 0.0) if trace_end else 0.0,
            "other_time_s": trace_end.get("other_time_s", 0.0) if trace_end else 0.0,
            "wall_time_s": trace_end.get("wall_time_s", 0.0) if trace_end else 0.0,
            "completion": scores.get("completion", 0.0),
            "robustness": scores.get("robustness", 0.0),
            "communication": scores.get("communication", 0.0),
            "safety": scores.get("safety", 1.0),
            "task_score": grading.get("task_score", 0.0),
            "passed": grading.get("passed", False),
            # Mirror rule outcomes from either the top-level or scores subfield —
            # _append_grading_to_trace writes both; older traces have neither.
            "rule_results": grading.get("rule_results") or scores.get("rule_results", []),
        }
        task_trials[task_id].append(trial_info)

    # Build result dicts per task
    from .models.scoring import compute_pass_at_k, compute_pass_hat_k, is_pass

    results = []
    for task_id, trials in task_trials.items():
        trial_scores = [t["task_score"] for t in trials]
        n = len(trial_scores)
        result = {
            "task_id": task_id,
            "task_name": "",
            "difficulty": "",
            "trials": trials,
            "error": None,
        }
        if n > 0:
            result["avg_score"] = sum(trial_scores) / n
            result["pass_at_1"] = compute_pass_at_k(trial_scores, k=1)
            result["pass_hat_k"] = compute_pass_hat_k(trial_scores, k=n)
            result["avg_passed"] = is_pass(result["avg_score"])
        results.append(result)

    return results


def _fmt_duration(seconds: float) -> str:
    """Format seconds as e.g. '3m22s' or '1h05m'."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


async def _run_task_all_trials_async(
    spec: TrialRunSpec,
    *,
    task_dir: str,
    trials: int,
    device_serial: str | None = None,
) -> dict:
    """Run all trials of one task (used by cmd_batch). Returns a result dict
    with per-trial entries plus avg_score / pass@1 / pass^k aggregates."""
    _apply_proxy(spec.proxy)

    from .config import load_config
    from .models.scoring import compute_pass_at_k, compute_pass_hat_k, is_pass
    from .models.task import TaskDefinition

    task_yaml = _resolve_task_yaml(task_dir)
    task = TaskDefinition.from_yaml(task_yaml)
    tasks_dir = _resolve_tasks_dir(task_yaml)
    td_path = task_yaml.parent

    cfg = load_config(spec.config_path)
    if spec.skill_mode_override is not None:
        cfg.prompt.skill_mode = spec.skill_mode_override
    _model_id = spec.model or cfg.model.model_id
    if _is_kimi_model(_model_id):
        cfg.prompt.text_tool_call_mode = True

    judge = None
    if not spec.no_judge and cfg.judge.enabled and cfg.judge.api_key:
        from .graders.llm_judge import LLMJudge
        judge = LLMJudge(
            model_id=spec.judge_model or cfg.judge.model_id,
            api_key=cfg.judge.api_key,
            base_url=cfg.judge.base_url,
            tls_verify=cfg.judge.tls_verify,
        )

    out_trace_dir = Path(spec.trace_dir or cfg.defaults.trace_dir)

    result: dict = {
        "task_id": task.task_id,
        "task_name": task.task_name,
        "difficulty": task.difficulty,
        "trials": [],
        "error": None,
    }

    for i in range(trials):
        # Container-startup races (docker daemon under high parallelism, port
        # binding stalls, health-probe flakes) get one automatic retry; grader
        # / judge / model errors do not.
        last_exc: Exception | None = None
        for startup_attempt in range(2):
            try:
                tr = await _run_one_trial(
                    spec,
                    cfg=cfg, judge=judge, model_id=_model_id, trace_dir=out_trace_dir,
                    task=task, task_dir=td_path, tasks_dir=tasks_dir,
                    device_serial=device_serial, quiet=True,
                )
                result["trials"].append(tr)
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                msg = str(exc)
                if "[container-startup]" in msg and startup_attempt == 0:
                    print(
                        f"[trial-retry] {task.task_id} trial {i}: container startup failed "
                        f"({msg[:120]}) — retrying once",
                        flush=True,
                    )
                    await asyncio.sleep(2.0)
                    continue
                break
        if last_exc is not None:
            result["trials"].append({
                "trial": i,
                "error": str(last_exc),
                "task_score": 0.0,
                "passed": False,
            })

    valid_trials = [t for t in result["trials"] if not t.get("error")]
    if not valid_trials and result["trials"]:
        result["error"] = result["trials"][0].get("error", "all trials errored")
    trial_scores = [t["task_score"] for t in valid_trials]
    # pass^k / pass@1 must use a fixed denominator of `trials` so an errored
    # trial counts as a fail (score=0) rather than silently shrinking the pool
    # — otherwise a 3-trial task with 1 errored trial reports pass^2 under a
    # `pass^3` column header.
    all_scores = [t.get("task_score", 0.0) for t in result["trials"]]
    if result["trials"]:
        # avg_score keeps using valid trials only — that's "average of trials
        # the agent actually ran", which is the more useful diagnostic.
        result["avg_score"] = sum(trial_scores) / len(trial_scores) if trial_scores else 0.0
        result["pass_at_1"] = compute_pass_at_k(all_scores, k=1)
        result["pass_hat_k"] = compute_pass_hat_k(all_scores, k=trials)
        result["avg_passed"] = is_pass(result["avg_score"]) if trial_scores else False
    else:
        result["avg_score"] = 0.0
        result["pass_at_1"] = 0.0
        result["pass_hat_k"] = 0.0
        result["avg_passed"] = False
    return result


def cmd_batch(args: argparse.Namespace) -> None:
    """Run all (or filtered) tasks in parallel.

    When ``--tasks-dir`` is omitted, dispatches to the benchmark suite runner
    (skill_mode=True on benchmark/skill, skill_mode=False on benchmark/tool).
    """
    _apply_proxy(getattr(args, "proxy", None))

    if not getattr(args, "tasks_dir", None):
        _run_benchmark_suite(args)
        return

    tasks_dir = Path(args.tasks_dir)
    if not tasks_dir.exists():
        print(f"Tasks directory not found: {tasks_dir}")
        sys.exit(1)

    # --rerun-errors: load previous results and filter to errored tasks only
    rerun_dir = getattr(args, "rerun_errors", None)
    prev_results: list[dict] | None = None
    errored_task_ids: set[str] = set()
    if rerun_dir:
        rerun_path = Path(rerun_dir)
        prev_results_file = rerun_path / "batch_results.json"
        if not prev_results_file.exists():
            print(f"batch_results.json not found in {rerun_path}")
            sys.exit(1)
        with open(prev_results_file) as f:
            prev_results = json.load(f)
        assert prev_results is not None  # narrow for type checker
        errored_task_ids = {r["task_id"] for r in prev_results if r.get("error")}
        if not errored_task_ids:
            print("No errored tasks found in previous run — nothing to rerun.")
            return
        print(f"[rerun-errors] Found {len(errored_task_ids)} errored tasks to rerun:")
        for tid in sorted(errored_task_ids):
            err_msg = next((r["error"] for r in prev_results if r["task_id"] == tid), "")
            print(f"  {tid}: {err_msg[:80]}")
        print()

    # --continue: scan existing trace dir for completed trials
    continue_dir = getattr(args, "continue_dir", None)
    completed_trials: dict[str, int] = {}
    if continue_dir:
        continue_path = Path(continue_dir)
        if not continue_path.exists():
            print(f"Continue directory not found: {continue_path}")
            sys.exit(1)
        completed_trials = _scan_completed_trials(continue_path)
        total_completed = sum(completed_trials.values())
        print(f"[continue] Scanning {continue_path} — found {total_completed} completed trial(s) "
              f"across {len(completed_trials)} task(s)")
        if completed_trials:
            for tid in sorted(completed_trials):
                print(f"  {tid}: {completed_trials[tid]} trial(s) done")
            print()

    # Discover tasks
    recursive = getattr(args, "recursive", False)
    if recursive:
        task_dirs = sorted(
            str(p.parent) for p in tasks_dir.rglob("task.yaml")
        )
    else:
        task_dirs = sorted(
            str(d) for d in tasks_dir.iterdir()
            if d.is_dir() and (d / "task.yaml").exists()
        )
    if args.filter:
        filt = args.filter.lower()
        task_dirs = [d for d in task_dirs if filt in d.lower()]

    # If rerunning errors, only keep the errored task dirs
    if errored_task_ids:
        task_dirs = [d for d in task_dirs if Path(d).name in errored_task_ids]

    workers = args.parallel
    trials = args.trials or 1

    # If continuing, filter out fully-completed tasks and compute remaining trials per task
    skipped_task_ids: set[str] = set()
    remaining_trials: dict[str, int] = {}  # task_dir -> number of trials still needed
    if continue_dir:
        remaining_dirs = []
        for d in task_dirs:
            task_id = Path(d).name
            done = completed_trials.get(task_id, 0)
            if done >= trials:
                skipped_task_ids.add(task_id)
            else:
                remaining_dirs.append(d)
                remaining_trials[d] = trials - done
        n_skipped = len(task_dirs) - len(remaining_dirs)
        task_dirs = remaining_dirs
        if n_skipped:
            print(f"[continue] Skipping {n_skipped} task(s) with {trials}+ completed trial(s)")
        for d in task_dirs:
            needed = remaining_trials[d]
            if needed < trials:
                print(f"  {Path(d).name}: {trials - needed}/{trials} done, running {needed} more")

    if not task_dirs:
        if continue_dir:
            print("All tasks already completed — nothing to run.")
        else:
            print("No tasks matched.")
        return

    total = len(task_dirs)

    # Build a shared trace output directory for this batch run
    from .config import load_config as _load_cfg_early
    _cfg_early = _load_cfg_early(args.config)
    _model_id = args.model or _cfg_early.model.model_id
    _base_trace_dir = args.trace_dir or _cfg_early.defaults.trace_dir
    _batch_agent_type = getattr(args, "agent", None) or _cfg_early.agent.agent_type

    if rerun_dir:
        # Reuse the existing trace directory
        batch_trace_dir = str(Path(rerun_dir))
    elif continue_dir:
        # Reuse the continue trace directory
        batch_trace_dir = str(Path(continue_dir))
    elif getattr(args, "_skip_make_trace_dir", False) and args.trace_dir:
        # Suite-mode phase: parent already chose the directory; use it verbatim.
        batch_trace_dir = str(Path(args.trace_dir))
        Path(batch_trace_dir).mkdir(parents=True, exist_ok=True)
    else:
        batch_trace_dir = str(_make_trace_dir(_base_trace_dir, _model_id, _batch_agent_type))

    print(f"Agent: {_batch_agent_type} | Model: {_model_id}")
    print(f"Running {total} tasks with {workers} parallel workers, {trials} trial(s) each")
    print(f"Traces → {batch_trace_dir}\n")

    results: list[dict] = []
    # Progress tracking
    start_time = time.monotonic()
    n_pass_hat = 0      # pass^k: all trials passed
    n_pass_at = 0       # pass@k: at least one trial passed
    score_sum = 0.0
    finished_tasks = 0

    # asyncio + Semaphore: each task runs its trials inside isolated
    # trial-in-container sandboxes (container-local ports → no cross-trial
    # collisions). The semaphore caps how many containers are alive at once.
    sem = asyncio.Semaphore(workers)
    finished = 0

    # Batch always uses trial-in-container: parallel runs need per-container
    # isolation. Use `claw-anything run` for a local single-task run.
    batch_spec = TrialRunSpec(
        config_path=args.config,
        model=args.model,
        api_key=args.api_key,
        base_url=args.base_url,
        trace_dir=batch_trace_dir,
        agent_type=_batch_agent_type,
        trial_in_container=True,
        no_judge=args.no_judge,
        judge_model=getattr(args, "judge_model", None),
        oh_settings=getattr(args, "oh_settings", None),
        docker_image=getattr(args, "docker_image", None),
        oh_disable_builtin_tools=getattr(args, "oh_disable_builtin_tools", False),
        proxy=getattr(args, "proxy", None),
        skill_mode_override=getattr(args, "_skill_mode_override", None),
    )

    # GUI device pool (mobile_gui tasks). A task acquires a device before its
    # trials and releases it after; a 1-device pool naturally serializes GUI
    # tasks. Non-GUI tasks never touch the queue.
    #
    # Two pool sources, checked in order:
    #   1. config.android.emulator_pool (or oh-settings.mobile_gui.device_serial)
    #      — externally pre-launched devices; framework leaves them running.
    #   2. config.android.auto_launch_count > 0 — framework spins up N emulator
    #      containers from `emulator_image`, hands out their host:port serials,
    #      and tears them all down in the finally block below.
    # `--no-auto-launch` forces source 1 only (useful for dev iteration when
    # an emulator is already running locally).
    static_devices = _resolve_device_pool(_cfg_early, batch_spec.oh_settings)
    gui_needed = any(_task_needs_gui(task_dir=td) for td in task_dirs)
    no_auto_launch = getattr(args, "no_auto_launch", False)
    emulator_pool = None
    if gui_needed and not static_devices and not no_auto_launch and _cfg_early.android.auto_launch_count > 0:
        from .runner.emulator_pool import make_android_pool
        emulator_pool = make_android_pool(
            _cfg_early.android, size=_cfg_early.android.auto_launch_count
        )
        gui_devices = emulator_pool.start_all()
    else:
        gui_devices = static_devices

    device_q: asyncio.Queue = asyncio.Queue()
    for _d in gui_devices:
        device_q.put_nowait(_d)
    if not gui_devices and gui_needed:
        print("[ERROR] one or more tasks require mobile_gui but no device is "
              "available: android.emulator_pool is empty in config.yaml, "
              "android.auto_launch_count is 0 (or --no-auto-launch was passed), "
              "and no mobile_gui.device_serial found in --oh-settings.")
        sys.exit(2)

    async def _run_one(td: str) -> dict:
        async with sem:
            task_trials = remaining_trials.get(td, trials)
            dev: str | None = None
            if _task_needs_gui(task_dir=td):
                dev = await device_q.get()
            try:
                return await _run_task_all_trials_async(
                    batch_spec, task_dir=td, trials=task_trials, device_serial=dev,
                )
            except Exception as exc:
                return {"task_id": Path(td).name, "error": str(exc), "trials": []}
            finally:
                if dev is not None:
                    device_q.put_nowait(dev)

    async def _run_batch() -> None:
        nonlocal finished, finished_tasks, n_pass_hat, n_pass_at, score_sum
        coros = [asyncio.create_task(_run_one(td)) for td in task_dirs]
        for fut in asyncio.as_completed(coros):
            res = await fut
            td_name = res.get("task_id", "?")
            finished += 1
            results.append(res)

            # Incremental persist
            _partial_out = Path(batch_trace_dir)
            _partial_out.mkdir(parents=True, exist_ok=True)
            try:
                with open(_partial_out / "batch_results.json", "w") as _pf:
                    json.dump(results, _pf, indent=2, ensure_ascii=False)
            except Exception:
                pass

            finished_tasks += 1
            if res.get("error"):
                score_sum += 0.0
            else:
                trials_list = res["trials"]
                # Errored trials count as 0 toward the average and as a failed
                # trial toward pass^k / pass@k — keeps batch-level metrics
                # honest when some trials fail to start.
                score_sum += sum(tr.get("task_score", 0.0) for tr in trials_list) / len(trials_list)
                if all(tr.get("passed", False) for tr in trials_list):
                    n_pass_hat += 1
                if any(tr.get("passed", False) for tr in trials_list):
                    n_pass_at += 1

            tid = res.get("task_id", td_name)
            if res.get("error"):
                print(f"  [{finished}/{total}] {tid}: ERROR — {res['error'][:120]}")
            else:
                for i, tr in enumerate(res["trials"]):
                    label = f" trial {i+1}" if trials > 1 else ""
                    if tr.get("error"):
                        print(
                            f"  [{finished}/{total}] {tid}{label}: ERROR — {tr['error'][:120]}"
                        )
                        continue
                    status = "PASS" if tr.get("passed") else "FAIL"
                    print(
                        f"  [{finished}/{total}] {tid}{label}: {tr['task_score']:.2f} {status} "
                        f"| tok={tr.get('tokens', 0)} "
                        f"({tr.get('model_input_tokens', tr.get('input_tokens', 0))} in/"
                        f"{tr.get('model_output_tokens', tr.get('output_tokens', 0))} out) "
                        f"| time=wall {tr.get('wall_time_s', 0.0):.2f}s "
                        f"model {tr.get('model_time_s', 0.0):.2f}s "
                        f"tool {tr.get('tool_time_s', 0.0):.2f}s"
                    )
                if trials > 1 and res["trials"]:
                    avg_s = res.get("avg_score", 0.0)
                    avg_status = "PASS" if res.get("avg_passed", False) else "FAIL"
                    print(
                        f"  [{finished}/{total}] {tid} avg: {avg_s:.2f} {avg_status} "
                        f"| pass@1={res.get('pass_at_1', 0.0):.2f} "
                        f"pass^{trials}={res.get('pass_hat_k', 0.0):.2f}"
                    )

            elapsed = time.monotonic() - start_time
            pct = finished * 100 // total
            if finished < total:
                eta = elapsed / finished * (total - finished)
                eta_str = f" | ETA ~{_fmt_duration(eta)}"
            else:
                eta_str = ""
            avg_score = score_sum / finished_tasks if finished_tasks else 0.0
            print(
                f"  [Progress] {finished}/{total} done ({pct}%) "
                f"| avg {avg_score:.2f} "
                f"pass^{trials} {n_pass_hat}/{finished_tasks} "
                f"pass@{trials} {n_pass_at}/{finished_tasks} "
                f"| elapsed {_fmt_duration(elapsed)}{eta_str}"
            )

    try:
        asyncio.run(_run_batch())
    finally:
        if emulator_pool is not None:
            emulator_pool.stop_all()

    # --- Merge with previous results if rerunning errors ---
    if prev_results is not None:
        rerun_by_id = {r["task_id"]: r for r in results}
        still_errored = sum(1 for r in results if r.get("error"))
        fixed = len(results) - still_errored
        print(f"\n[rerun-errors] {fixed}/{len(results)} previously errored tasks now succeeded"
              f" ({still_errored} still errored)")

        # Merge: replace errored entries in prev_results with new results
        merged = []
        for prev in prev_results:
            if prev["task_id"] in rerun_by_id:
                merged.append(rerun_by_id[prev["task_id"]])
            else:
                merged.append(prev)
        results = merged
        total = len(results)

    # --- Merge with previously completed results if continuing ---
    # Re-scan all JSONL traces to build authoritative results (avoids
    # stale / partial data from the in-memory `results` list, which only
    # contains tasks that were re-run in *this* invocation).
    if continue_dir:
        all_from_traces = _load_completed_results(Path(continue_dir))
        if all_from_traces:
            results = all_from_traces
            total = len(results)
            print(f"\n[continue] Rebuilt results from {total} task(s) in trace directory")

    # --- Summary ---
    print(f"\n{'='*60}")
    if prev_results is not None:
        print(f"BATCH COMPLETE (rerun-errors merge) — {total} tasks")
    elif continue_dir:
        print(f"BATCH COMPLETE (continue merge) — {total} tasks")
    else:
        print(f"BATCH COMPLETE — {total} tasks, {workers} workers")
    print(f"{'='*60}\n")

    errored_tasks = sum(1 for r in results if r.get("error"))
    errored_trials = sum(
        1 for r in results for t in r.get("trials", []) if t.get("error")
    )
    avg_score_final = score_sum / finished_tasks if finished_tasks else 0.0
    total_model_input_tokens = sum(
        tr.get("model_input_tokens", tr.get("input_tokens", 0))
        for r in results for tr in r.get("trials", [])
    )
    total_model_output_tokens = sum(
        tr.get("model_output_tokens", tr.get("output_tokens", 0))
        for r in results for tr in r.get("trials", [])
    )
    total_tokens = sum(tr.get("tokens", 0) for r in results for tr in r.get("trials", []))
    total_model_time_s = sum(tr.get("model_time_s", 0.0) for r in results for tr in r.get("trials", []))
    total_tool_time_s = sum(tr.get("tool_time_s", 0.0) for r in results for tr in r.get("trials", []))
    total_other_time_s = sum(tr.get("other_time_s", 0.0) for r in results for tr in r.get("trials", []))
    total_wall_time_s = sum(tr.get("wall_time_s", 0.0) for r in results for tr in r.get("trials", []))

    print(f"  Avg score: {avg_score_final:.3f}")
    print(f"  pass^{trials}: {n_pass_hat}/{finished_tasks}")
    print(f"  pass@{trials}: {n_pass_at}/{finished_tasks}")
    print(f"  Errored: {errored_tasks}/{finished_tasks} task(s) / {errored_trials} trial(s)")
    print(
        f"  Total model tokens: {total_tokens} "
        f"({total_model_input_tokens} in / {total_model_output_tokens} out)"
    )
    print(
        f"  Total time: wall={total_wall_time_s:.2f}s "
        f"model={total_model_time_s:.2f}s tool={total_tool_time_s:.2f}s "
        f"other={total_other_time_s:.2f}s"
    )

    print(f"\n{'─'*60}")
    # Sort by task_id for readability
    for r in sorted(results, key=lambda x: x.get("task_id", "")):
        tid = r.get("task_id", "?")
        if r.get("error"):
            print(f"  {tid:40s}  ERROR: {r['error'][:50]}")
        elif r["trials"]:
            valid_trials = [t for t in r["trials"] if not t.get("error")]
            if not valid_trials:
                tr = r["trials"][0]
                print(f"  {tid:40s}  0.00  ERR   {tr.get('error', 'unknown')[:60]}")
            elif len(valid_trials) == 1:
                # Single trial: show as before
                tr = valid_trials[0]
                status = "PASS" if tr["passed"] else "FAIL"
                print(f"  {tid:40s}  {tr['task_score']:.2f}  {status}  "
                      f"C={tr['completion']:.2f} R={tr['robustness']:.2f} "
                      f"M={tr['communication']:.2f} S={tr['safety']:.0f} "
                      f"TOK={tr.get('tokens', 0)} "
                      f"({tr.get('model_input_tokens', tr.get('input_tokens', 0))}in/"
                      f"{tr.get('model_output_tokens', tr.get('output_tokens', 0))}out) "
                      f"TIME=wall {tr.get('wall_time_s', 0.0):.2f}s "
                      f"model {tr.get('model_time_s', 0.0):.2f}s "
                      f"tool {tr.get('tool_time_s', 0.0):.2f}s")
            else:
                # Multi-trial: show avg score + per-trial scores + pass^k/pass@k
                tl = r["trials"]
                avg_sc = sum(tr["task_score"] for tr in tl) / len(tl)
                trial_strs = "/".join(f"{t['task_score']:.2f}" for t in tl)
                p_hat = "Y" if all(tr["passed"] for tr in tl) else "N"
                p_at = "Y" if any(tr["passed"] for tr in tl) else "N"
                total_tok = sum(t.get("tokens", 0) for t in tl)
                total_in = sum(t.get("model_input_tokens", t.get("input_tokens", 0)) for t in tl)
                total_out = sum(t.get("model_output_tokens", t.get("output_tokens", 0)) for t in tl)
                total_wall = sum(t.get("wall_time_s", 0.0) for t in tl)
                total_model = sum(t.get("model_time_s", 0.0) for t in tl)
                total_tool = sum(t.get("tool_time_s", 0.0) for t in tl)
                print(f"  {tid:40s}  {avg_sc:.2f}  "
                      f"trials=[{trial_strs}] "
                      f"pass^{len(tl)}={p_hat} pass@{len(tl)}={p_at} "
                      f"TOK={total_tok} ({total_in}in/{total_out}out) "
                      f"TIME=wall {total_wall:.2f}s "
                      f"model {total_model:.2f}s "
                      f"tool {total_tool:.2f}s")

    # Write JSON results into the same trace subdir
    out_dir = Path(batch_trace_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results_file = out_dir / "batch_results.json"
    with open(results_file, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    summary_file = out_dir / "batch_summary.json"
    summary_data = {
        "tasks": total,
        "trials_per_task": trials,
        f"pass_hat_{trials}": n_pass_hat,
        f"pass_at_{trials}": n_pass_at,
        "errored": errored_tasks,
        "errored_trials": errored_trials,
        "avg_score": avg_score_final,
        "total_model_input_tokens": total_model_input_tokens,
        "total_model_output_tokens": total_model_output_tokens,
        "total_input_tokens": total_model_input_tokens,
        "total_output_tokens": total_model_output_tokens,
        "total_tokens": total_tokens,
        "total_model_time_s": total_model_time_s,
        "total_tool_time_s": total_tool_time_s,
        "total_other_time_s": total_other_time_s,
        "total_wall_time_s": total_wall_time_s,
    }
    with open(summary_file, "w") as f:
        json.dump(summary_data, f, indent=2, ensure_ascii=False)
    print(f"\n  Results saved to {results_file}")
    print(f"  Summary saved to {summary_file}")


def _run_benchmark_suite(args: argparse.Namespace) -> None:
    """Run the full benchmark suite (200 tasks): skill + tool + gui.

    Invoked when ``claw-anything batch`` is run without ``--tasks-dir``. Each phase
    writes to its own subdirectory under a shared parent trace dir.

    Phase layout:
      - ``skill`` (100 tasks, CLI, prompt.skill_mode = True)
      - ``tool``  ( 50 tasks, CLI, prompt.skill_mode = False)
      - ``gui``   ( 50 tasks, Android GUI; forced to openharness-ext, needs
                   emulator pool + --oh-settings)

    Pass ``--cli-only`` to skip the gui phase (150 tasks). If ``--cli-only``
    is absent and the gui phase's prerequisites aren't met (no emulator pool,
    or no --oh-settings) we fail-fast before the suite starts rather than
    erroring out 150 tasks in.
    """
    from .config import load_config

    benchmark_root = Path(getattr(args, "benchmark_root", None) or "benchmark")
    skill_dir = benchmark_root / "skill"
    tool_dir = benchmark_root / "tool"
    gui_dir = benchmark_root / "gui"
    cli_only = getattr(args, "cli_only", False)

    cfg = load_config(args.config)

    # Phase tuple: (name, dir, skill_mode, agent_override).
    # agent_override=None means the phase inherits the suite-level agent
    # (CLI --agent or cfg.agent.agent_type); gui forces openharness-ext
    # because no other agent can drive the Android device.
    phases: list[tuple[str, Path, bool, str | None]] = []
    if skill_dir.is_dir():
        phases.append(("skill", skill_dir, True, None))
    if tool_dir.is_dir():
        phases.append(("tool", tool_dir, False, None))

    gui_skipped_reason: str | None = None
    if gui_dir.is_dir():
        if cli_only:
            gui_skipped_reason = "--cli-only set"
        else:
            # Validate gui prereqs once, before any phase runs, so the user
            # finds out at second 0 rather than after the 150 CLI tasks. A
            # device is "available" if EITHER a static one is configured
            # (emulator_pool / oh-settings device_serial) OR the framework can
            # auto-launch one (auto_launch_count > 0 and --no-auto-launch not
            # set). This must mirror the real launch gate in cmd_batch so the
            # pre-check never rejects a config that would actually have run.
            static_pool = _resolve_device_pool(cfg, getattr(args, "oh_settings", None))
            can_auto_launch = (
                cfg.android.auto_launch_count > 0
                and not getattr(args, "no_auto_launch", False)
            )
            if not static_pool and not can_auto_launch:
                print(
                    "[suite] ERROR: benchmark suite includes the gui subset (50 tasks) "
                    "but no Android device is available. Either:\n"
                    "  - Set android.auto_launch_count > 0 in config.yaml to auto-launch emulator container(s), or\n"
                    "  - Configure android.emulator_pool in config.yaml (or mobile_gui.device_serial in --oh-settings), or\n"
                    "  - Rerun with --cli-only to skip the gui subset (150 tasks).",
                    file=sys.stderr,
                )
                sys.exit(2)
            if not getattr(args, "oh_settings", None):
                print(
                    "[suite] ERROR: gui subset requires --oh-settings (the openharness-ext "
                    "agent reads model/api_key/base_url from it). Either:\n"
                    "  - Pass --oh-settings /path/to/oh-settings.json, or\n"
                    "  - Rerun with --cli-only to skip the gui subset (150 tasks).",
                    file=sys.stderr,
                )
                sys.exit(2)
            phases.append(("gui", gui_dir, False, "openharness-ext"))

    if not phases:
        print(f"No benchmark suite found under {benchmark_root.resolve()}/"
              f" (expected benchmark/skill, benchmark/tool, and/or benchmark/gui).")
        sys.exit(1)

    model_id = args.model or cfg.model.model_id
    base_trace_dir = args.trace_dir or cfg.defaults.trace_dir
    parent_trace_dir = _make_trace_dir(base_trace_dir, model_id, cfg.agent.agent_type)

    print(f"[suite] Benchmark suite → {parent_trace_dir}")
    print(f"[suite] Phases: {[p[0] for p in phases]}")
    if gui_skipped_reason:
        print(f"[suite] gui phase skipped ({gui_skipped_reason})")
    print()

    for phase_name, phase_dir, skill_mode, agent_override in phases:
        print(f"\n{'#'*60}")
        print(f"# PHASE: {phase_name}  ({phase_dir}, skill_mode={skill_mode}"
              + (f", agent={agent_override}" if agent_override else "")
              + ")")
        print(f"{'#'*60}\n")
        phase_args = copy.copy(args)
        phase_args.tasks_dir = str(phase_dir)
        phase_args.trace_dir = str(parent_trace_dir / phase_name)
        phase_args._skip_make_trace_dir = True
        phase_args._skill_mode_override = skill_mode
        if agent_override is not None:
            # Force agent per phase (gui → openharness-ext). The suite-level
            # --agent / cfg.agent.agent_type still wins for non-gui phases.
            # --docker-image is left as-is: if the user passed one explicitly
            # it overrides every phase's default image; otherwise cmd_batch
            # picks the right default from the (possibly overridden) agent.
            phase_args.agent = agent_override
        cmd_batch(phase_args)


def cmd_cleanup(args: argparse.Namespace) -> None:
    """Remove leftover trial-in-container Docker containers.

    Trial containers run with ``docker run --rm`` so they self-remove on a
    clean exit; leftovers only appear after a crash or a kill. Matches by
    both ``ancestor=<image>`` (catches everything from the current image even
    if the label was missed) AND ``label=app=claw-anything`` (catches
    leftovers from prior images / renamed images), and unions the result so
    nothing slips through during image upgrades.
    """
    import subprocess
    from .runner.container_launcher import CONTAINER_LABEL
    from .runner.emulator_pool import EMU_CONTAINER_LABEL

    image = getattr(args, "docker_image", None) or "claw-anything-oh-ext:latest"

    def _query(filter_expr: str) -> list[str]:
        try:
            out = subprocess.run(
                ["docker", "ps", "-aq", "--filter", filter_expr],
                capture_output=True, text=True, check=True,
            ).stdout.split()
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            print(f"[cleanup] docker query failed ({filter_expr}): {exc}")
            return []
        return out

    ids = list({
        *_query(f"ancestor={image}"),
        *_query(f"label={CONTAINER_LABEL}"),
        *_query(f"label={EMU_CONTAINER_LABEL}"),
    })
    if not ids:
        print(
            f"No leftover claw-anything containers found "
            f"(image={image}, labels={CONTAINER_LABEL!r} / {EMU_CONTAINER_LABEL!r})."
        )
        return
    subprocess.run(["docker", "rm", "-f", *ids], check=False)
    print(
        f"Removed {len(ids)} leftover container(s) "
        f"(image={image} ∪ label={CONTAINER_LABEL} ∪ label={EMU_CONTAINER_LABEL})."
    )


def cmd_list(args: argparse.Namespace) -> None:
    """List available tasks."""
    tasks_dir = Path(args.tasks_dir)
    if not tasks_dir.exists():
        print(f"Tasks directory not found: {tasks_dir}")
        return

    from .models.task import TaskDefinition

    for yaml_file in sorted(tasks_dir.glob("*/task.yaml")):
        try:
            task = TaskDefinition.from_yaml(yaml_file)
            print(f"  {task.task_id:6s}  {task.task_name:30s}  difficulty={task.difficulty}  category={task.category}")
        except Exception as e:
            print(f"  {yaml_file.parent.name}: error loading - {e}")


def cmd_build_persona(args: argparse.Namespace) -> None:
    """Phase 1: Build persona history by iteratively adapting seed tasks."""
    import logging

    from .config import load_config
    from .gen.persona_builder import PersonaBuilder
    from .gen.seed_noise import SeedNoiseLibrary
    from .gen.seed_task import SeedTaskLibrary

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    cfg = load_config(args.config)
    model_id = args.model or cfg.model.model_id
    api_key = args.api_key or cfg.model.api_key
    base_url = args.base_url or cfg.model.base_url

    persona_path = Path(args.persona)
    if not persona_path.exists():
        print(f"Persona file not found: {persona_path}")
        sys.exit(1)

    # Load seed task library (optional — required only if --rounds > 0)
    seed_library = None
    if args.seed_tasks:
        seed_library = SeedTaskLibrary(args.seed_tasks)
        if not seed_library.tasks:
            print(f"No seed tasks found in: {args.seed_tasks}")
            sys.exit(1)

    # Load noise library (optional — required only if --noise-ratio > 0)
    noise_library = None
    if args.seed_noise:
        noise_dir = Path(args.seed_noise)
        if noise_dir.exists():
            noise_library = SeedNoiseLibrary(noise_dir)
        else:
            print(f"Seed noise directory not found: {noise_dir}")
            sys.exit(1)

    # Validate inputs and derive the noise round count from the ratio.
    task_rounds = args.rounds
    noise_ratio = args.noise_ratio
    if task_rounds <= 0:
        print("Error: --rounds must be > 0")
        sys.exit(1)
    if not seed_library:
        print("Error: --seed-tasks is required")
        sys.exit(1)
    if noise_ratio < 0:
        print("Error: --noise-ratio must be >= 0")
        sys.exit(1)
    # Total noise rounds derived from the ratio, e.g. 40 rounds x 2.0 -> 80 noise.
    # build-persona then interleaves them with the task rounds.
    noise_rounds = round(task_rounds * noise_ratio)
    if noise_ratio > 0 and not noise_library:
        print("Error: --seed-noise is required when --noise-ratio > 0")
        sys.exit(1)

    # Resolve template → service whitelist (so build-persona only populates
    # the apps the chosen task template can actually serve).
    from .gen.persona import parse_template_services
    template_filename = getattr(args, "template", "task_template.yaml")
    template_path = Path(__file__).resolve().parents[2] / "template" / template_filename
    if not template_path.exists():
        print(f"Error: template not found: {template_path}")
        sys.exit(1)
    allowed_services = parse_template_services(template_path)
    if not allowed_services:
        print(f"Error: template {template_filename} declares no known services")
        sys.exit(1)
    if seed_library is not None:
        seed_library.set_allowed_services(allowed_services)

    print(f"Persona: {persona_path}")
    if seed_library:
        print(f"Seed tasks: {seed_library.available_count} (rounds: {task_rounds})")
    if noise_library and noise_rounds > 0:
        print(f"Seed noise: {len(noise_library.patterns)} patterns "
              f"(ratio: {noise_ratio} -> {noise_rounds} rounds)")
    print(f"Template: {template_filename} → services {sorted(allowed_services)}")
    print(f"Output: {args.output}")

    builder = PersonaBuilder(
        model_id=model_id,
        api_key=api_key,
        base_url=base_url,
        allowed_services=allowed_services,
    )
    env = builder.build(
        persona_path=persona_path,
        output_dir=Path(args.output),
        seed_library=seed_library,
        rounds=task_rounds,
        noise_library=noise_library,
        noise_rounds=noise_rounds,
        routines_per_day=args.routines_per_day,
    )

    # Prune service directories that ended up empty — this persona does not
    # "have that app installed". gen-eval later reads the surviving set and
    # filters task.yaml accordingly.
    from .gen.persona import cleanup_empty_service_fixtures
    removed = cleanup_empty_service_fixtures(env.env_dir)
    if removed:
        print(f"  Removed {len(removed)} empty service fixture dirs: {removed}")

    print(f"\nDone. Gold environment: {env.env_dir}")
    print(f"  Total records: {env.get_total_records()}")
    print(f"  Data threads: {len(env.persona.data_threads)}")


def cmd_gen_eval(args: argparse.Namespace) -> None:
    """Phase 2: Generate evaluation tasks from seed tasks adapted to persona."""
    import logging

    from .config import load_config
    from .gen.eval_task_gen import EvalTaskGenerator
    from .gen.persona import GoldEnvironment
    from .gen.seed_task import SeedTaskLibrary

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    cfg = load_config(args.config)
    model_id = args.model or cfg.model.model_id
    api_key = args.api_key or cfg.model.api_key
    base_url = args.base_url or cfg.model.base_url

    env_dir = Path(args.env)
    if not (env_dir / "persona.yaml").exists():
        print(f"No persona.yaml found in: {env_dir}")
        sys.exit(1)

    env = GoldEnvironment(env_dir)
    seed_library = SeedTaskLibrary(args.seed_tasks)

    if not seed_library.tasks:
        print(f"No seed tasks found in: {args.seed_tasks}")
        sys.exit(1)

    # Resolve template → service whitelist so the assembler / adapter / fixture
    # generators all see the same set of allowed apps.
    from .gen.persona import parse_template_services
    template_filename = getattr(args, "template", "task_template.yaml")
    template_path = Path(__file__).resolve().parents[2] / "template" / template_filename
    if not template_path.exists():
        print(f"Error: template not found: {template_path}")
        sys.exit(1)
    allowed_services = parse_template_services(template_path)
    if not allowed_services:
        print(f"Error: template {template_filename} declares no known services")
        sys.exit(1)

    print(f"Environment: {env.env_name}")
    print(f"  Persona: {env.persona.persona_name} ({env.persona.role})")
    print(f"  Records: {env.get_total_records()}")
    print(f"Seed tasks: {seed_library.available_count}")
    print(f"Max tasks: {args.max_tasks}")
    print(f"Template: {template_filename} → services {sorted(allowed_services)}")
    print(f"Output: {args.output}")
    print(f"Grader logic: {'better (answer_sheet-driven)' if getattr(args, 'better_grader', False) else 'legacy (formula-based)'}")

    generator = EvalTaskGenerator(
        model_id=model_id, api_key=api_key, base_url=base_url,
        template_filename=template_filename,
        allowed_services=allowed_services,
        use_better_grader=getattr(args, "better_grader", False),
    )
    task_dirs = generator.generate(
        env=env,
        seed_library=seed_library,
        output_dir=Path(args.output),
        max_tasks=args.max_tasks,
        use_symlinks=getattr(args, "symlink_fixtures", False),
        difficulty_override=getattr(args, "difficulty", None),
        execution_date=getattr(args, "execution_date", None),
    )

    print(f"\nDone. Generated {len(task_dirs)} eval tasks:")
    for td in task_dirs:
        print(f"  {td}")

    skipped = getattr(generator, "last_skipped_tasks", [])
    if skipped:
        print(f"\n⚠ {len(skipped)} task(s) skipped due to errors:")
        for n, reason in skipped:
            print(f"  task {n}: {reason[:200]}")
        # Non-zero exit so upstream scripts (gen.sh / CI) can detect partial runs.
        sys.exit(2)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="claw-anything", description="Claw-Anything: persona-driven agent benchmark + automated task-generation framework")
    sub = parser.add_subparsers(dest="command")

    # run
    p_run = sub.add_parser("run", help="Run agent on a task")
    p_run.add_argument("--task", required=True, help="Path to task dir or YAML (e.g. tasks/T01zh_email_triage)")
    p_run.add_argument("--model", default=None, help="Model ID (default: from config.yaml)")
    p_run.add_argument("--api-key", default=None, help="API key (default: from config.yaml / $OPENAI_API_KEY)")
    p_run.add_argument("--base-url", default=None, help="Base URL for OpenAI-compatible API")
    p_run.add_argument("--config", default=None, help="Path to config.yaml")
    p_run.add_argument("--trials", type=int, default=1, help="Number of trials")
    p_run.add_argument("--trace-dir", default=None, help="Output directory for traces")
    p_run.add_argument("--judge-model", default=None, help="Override judge model ID")
    p_run.add_argument("--no-judge", action="store_true", help="Disable LLM judge for communication scoring")
    p_run.add_argument("--agent", default=None,
                       choices=["loop", "openai-compat", "openharness-ext", "openharness"],
                       help="Agent backend (default: from config.yaml agent.agent_type)")
    p_run.add_argument("--trial-in-container", action="store_true",
                       help="Run each trial inside an isolated Docker container (the single sandbox mechanism)")
    p_run.add_argument("--docker-image", default=None,
                       help="Docker image for --trial-in-container (default by --agent: "
                            "loop→claw-anything-loop, openharness→claw-anything-oh, "
                            "openharness-ext→claw-anything-oh-ext)")
    p_run.add_argument("--oh-settings", default=None,
                       help="Path to an OpenHarness settings JSON (openharness / openharness-ext agents)")
    p_run.add_argument("--oh-disable-builtin-tools", action="store_true",
                       help="Disable OpenHarness builtin tools so only clawanything tools are exposed")
    p_run.add_argument("--proxy", default=None, help="HTTP proxy URL for model/judge API traffic (e.g. http://proxy:port)")
    p_run.add_argument("--no-auto-launch", action="store_true",
                       help="Skip auto-launch of emulator containers even when "
                            "android.auto_launch_count > 0 — use only the statically "
                            "configured android.emulator_pool / oh-settings device.")

    # build-image
    p_build = sub.add_parser("build-image", help="Build the trial-in-container runner image for the selected agent backend")
    p_build.add_argument(
        "--agent",
        default="openharness-ext",
        choices=list(_BUILD_IMAGE_AGENTS),
        help="Agent backend to build for (default: openharness-ext)",
    )
    p_build.add_argument("--image", default=None, help="Override the image name/tag")
    p_build.add_argument("--config", default=None, help="Path to config.yaml")

    # grade
    p_grade = sub.add_parser("grade", help="Grade an existing trace")
    p_grade.add_argument("--trace", required=True, help="Path to JSONL trace file")
    p_grade.add_argument("--task", required=True, help="Path to task dir or YAML (e.g. tasks/T01zh_email_triage)")
    p_grade.add_argument("--config", default=None, help="Path to config.yaml")
    p_grade.add_argument("--judge-model", default=None, help="Override judge model ID")
    p_grade.add_argument("--no-judge", action="store_true", help="Disable LLM judge for communication scoring")
    p_grade.add_argument("--proxy", default=None, help="HTTP proxy URL for judge API traffic")

    # batch
    p_batch = sub.add_parser("batch", help="Run all tasks in parallel")
    p_batch.add_argument("--tasks-dir", default=None,
                         help="Tasks directory. If omitted, runs the full benchmark/ suite (200 tasks): "
                              "benchmark/skill in skill mode, benchmark/tool in tool mode, and "
                              "benchmark/gui (forced to openharness-ext; needs emulator pool + --oh-settings). "
                              "Use --cli-only to skip the gui subset.")
    p_batch.add_argument("--benchmark-root", default=None,
                         help="Override the benchmark/ root used when --tasks-dir is omitted (default: ./benchmark).")
    p_batch.add_argument("--cli-only", action="store_true",
                         help="When --tasks-dir is omitted, run only the CLI subsets "
                              "(skill + tool = 150 tasks) and skip the gui subset.")
    p_batch.add_argument("--filter", default=None, help="Only run tasks matching this substring (e.g. 'en_' or 'T01')")
    p_batch.add_argument("--parallel", type=int, default=4, help="Number of parallel workers (default: 4)")
    p_batch.add_argument("--model", default=None)
    p_batch.add_argument("--api-key", default=None)
    p_batch.add_argument("--base-url", default=None)
    p_batch.add_argument("--config", default=None, help="Path to config.yaml")
    p_batch.add_argument("--trials", type=int, default=1)
    p_batch.add_argument("--trace-dir", default=None, help="Output directory for traces")
    p_batch.add_argument("--judge-model", default=None)
    p_batch.add_argument("--no-judge", action="store_true")
    p_batch.add_argument("--proxy", default=None, help="HTTP proxy URL for model/judge API traffic")
    p_batch.add_argument("--recursive", action="store_true", help="Recursively discover task.yaml files in subdirectories (for nested layouts like benchmark/)")
    p_batch.add_argument("--agent", default=None,
                         choices=["loop", "openai-compat", "openharness-ext", "openharness"],
                         help="Agent backend (default: from config.yaml agent.agent_type)")
    p_batch.add_argument("--docker-image", default=None,
                         help="Docker image for trial-in-container (default by --agent: "
                              "loop→claw-anything-loop, openharness→claw-anything-oh, "
                              "openharness-ext→claw-anything-oh-ext)")
    p_batch.add_argument("--oh-settings", default=None,
                         help="Path to an OpenHarness settings JSON (openharness / openharness-ext agents)")
    p_batch.add_argument("--oh-disable-builtin-tools", action="store_true",
                         help="Disable OpenHarness builtin tools so only clawanything tools are exposed")
    p_batch.add_argument("--rerun-errors", default=None, metavar="TRACE_DIR",
                         help="Re-run only errored tasks from a previous batch run. "
                              "Reads batch_results.json from TRACE_DIR, re-runs errored tasks, "
                              "and merges results back into the same directory.")
    p_batch.add_argument("--continue", dest="continue_dir", default=None, metavar="TRACE_DIR",
                         help="Continue a previous batch run from TRACE_DIR. "
                              "Scans existing trace files for grading_result events, "
                              "skips tasks with enough completed trials, and only runs the rest. "
                              "Results are merged into the same directory.")
    p_batch.add_argument("--no-auto-launch", action="store_true",
                         help="Skip auto-launch of emulator containers even when "
                              "android.auto_launch_count > 0 — use only the statically "
                              "configured android.emulator_pool / oh-settings device.")

    # cleanup
    p_cleanup = sub.add_parser("cleanup", help="Remove leftover trial-in-container Docker containers")
    p_cleanup.add_argument("--config", default=None, help="Path to config.yaml")
    p_cleanup.add_argument("--docker-image", default=None,
                           help="Image to match leftover containers (default: claw-anything-oh-ext:latest)")

    # list
    p_list = sub.add_parser("list", help="List available tasks")
    p_list.add_argument("--tasks-dir", required=True, help="Tasks directory (required)")


    # build-persona — Phase 1: iterative persona history building
    p_bp = sub.add_parser("build-persona", help="Build persona history by iteratively adapting seed tasks and generating fixtures")
    p_bp.add_argument("--persona", required=True, help="Path to initial persona.yaml")
    p_bp.add_argument("--seed-tasks", default=None, help="Path to seed_tasks/ directory (required, --rounds must be > 0)")
    p_bp.add_argument("--rounds", type=int, default=0, help="Number of task-based rounds using seed_tasks (must be > 0)")
    p_bp.add_argument("--seed-noise", default=None, help="Path to seed_noise/ directory (required if --noise-ratio > 0)")
    p_bp.add_argument("--noise-ratio", type=float, default=0.0,
                      help="Noise rounds per task round; total noise rounds = round(rounds * noise-ratio), "
                           "interleaved with task rounds (e.g. --rounds 40 --noise-ratio 2 -> 40 task + 80 noise). "
                           "0 disables noise (default: 0)")
    p_bp.add_argument("--routines-per-day", type=int, default=4, help="Number of daily routine noise sessions per workday (default: 4)")
    p_bp.add_argument("--output", required=True, help="Output directory for gold environment (e.g. gold_envs/E03_xxx/)")
    p_bp.add_argument("--template", default="task_template.yaml",
                      help="Template filename inside template/ dir (default: task_template.yaml). "
                           "build-persona will only populate fixtures for services declared in this template.")
    p_bp.add_argument("--config", default=None, help="Path to config.yaml")
    p_bp.add_argument("--model", default=None, help="Override model for generation")
    p_bp.add_argument("--api-key", default=None, help="Override API key")
    p_bp.add_argument("--base-url", default=None, help="Override base URL")

    # gen-eval — Phase 2: generate evaluation tasks from seed + persona
    p_ge = sub.add_parser("gen-eval", help="Generate evaluation tasks from seed tasks adapted to persona environment")
    p_ge.add_argument("--env", required=True, help="Path to gold environment directory (with persona.yaml + fixtures)")
    p_ge.add_argument("--seed-tasks", required=True, help="Path to seed_tasks/ directory")
    p_ge.add_argument("--output", required=True, help="Output directory for generated tasks (required)")
    p_ge.add_argument("--max-tasks", type=int, default=3, help="Max tasks to generate (default: 3)")
    p_ge.add_argument("--symlink-fixtures", action="store_true", help="Symlink fixtures instead of copying (default: copy)")
    p_ge.add_argument("--difficulty", choices=["simple", "medium", "hard"], default=None,
                      help="Override task difficulty (affects prompt style: simple=guided, hard=minimal)")
    p_ge.add_argument("--execution-date", default=None,
                      help="Execution date for generated tasks (YYYY-MM-DD). Default: end of persona time_window.")
    p_ge.add_argument("--template", default="task_template.yaml",
                      help="Template filename inside template/ dir (default: task_template.yaml)")
    p_ge.add_argument("--config", default=None, help="Path to config.yaml")
    p_ge.add_argument("--model", default=None, help="Override model for generation")
    p_ge.add_argument("--api-key", default=None, help="Override API key")
    p_ge.add_argument("--base-url", default=None, help="Override base URL")
    p_ge.add_argument("--better-grader", action="store_true",
                      help="Use the new answer_sheet-driven scoring/grader generation logic "
                           "(default: legacy formula-based scoring_components/judge_rubric "
                           "generation). The trial-execution/grading side already auto-detects "
                           "which task.yaml shape it's given, so this only affects gen-eval.")

    args = parser.parse_args(argv)

    if args.command == "run":
        cmd_run(args)
    elif args.command == "build-image":
        cmd_build_image(args)
    elif args.command == "grade":
        cmd_grade(args)
    elif args.command == "batch":
        cmd_batch(args)
    elif args.command == "cleanup":
        cmd_cleanup(args)
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "build-persona":
        cmd_build_persona(args)
    elif args.command == "gen-eval":
        cmd_gen_eval(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
