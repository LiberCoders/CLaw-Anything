"""Launch a claw-anything trial inside a Docker container (trial-in-container sandbox mode).

The container runs ``claw-anything run --agent <agent>`` with the task
mounted at /task and output written to /out (mounted volume).

Phase 1: basic docker run — no URL rewriting, no emulator pool semaphore.
Phase 2 will add: ADB pre-connect.
Phase 3 will add: emulator pool semaphore for concurrent Android tasks.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..config import ContainerConfig

_LOCALHOST_RE = re.compile(r"(https?://)(?:localhost|127\.0\.0\.1)(:\d+)")
_ADB_SERIAL_RE = re.compile(r"(?:localhost|127\.0\.0\.1):(\d+)")


# --------------------------------------------------------------------------- #
#  Inner-trial-container marker                                                #
# --------------------------------------------------------------------------- #
# This module (the launcher) injects ``CLAW_ANYTHING_INSIDE_TRIAL_CONTAINER=1``
# into the inner ``claw-anything`` process it spawns. That inner process
# (``cli.py``) branches on the signal to skip host-only setup (device-pool
# re-resolution, oh-settings re-materialization, GUI init) and to avoid wrapping
# the already-final ``/out`` dir in another nested layer. Keeping the env-var
# name and its reading in one place stops the literal and the idiom from
# drifting apart across the writer (here) and the reader (cli.py).

#: Env var the launcher sets on the inner stage. The launcher builds its
#: ``-e NAME=1`` flag from this same constant.
INSIDE_TRIAL_CONTAINER_ENV = "CLAW_ANYTHING_INSIDE_TRIAL_CONTAINER"


def is_inside_trial_container() -> bool:
    """True when this process is the inner stage launched by this module.

    When True, the mounted config / oh-settings / GUI artifacts are already
    host-prepared and final, and ``--trace-dir`` (/out) IS the per-trial dir.
    """
    return bool(os.environ.get(INSIDE_TRIAL_CONTAINER_ENV))


def _rewrite_oh_settings_for_container(settings: dict) -> dict:
    """Rewrite localhost/127.0.0.1 URLs in OH settings so the inner claw-anything
    can reach host-side services (model API, Android emulator ADB, GUI backend)
    via the host.docker.internal gateway.

    Mutates and returns a deep copy. Covers:
      - mobile_gui.device_serial (ADB host:port)
      - mobile_gui.adb_path (host path → container path)
      - mobile_gui.gui_backend.base_url
      - profiles[*].base_url (LLM endpoints)
    """
    s = copy.deepcopy(settings)
    mg = s.get("mobile_gui", {})
    if "device_serial" in mg:
        mg["device_serial"] = _ADB_SERIAL_RE.sub(r"host.docker.internal:\1", mg["device_serial"])
    if "adb_path" in mg:
        mg["adb_path"] = "/usr/local/bin/adb"
    gb = mg.get("gui_backend", {})
    if "base_url" in gb:
        gb["base_url"] = _LOCALHOST_RE.sub(r"\1host.docker.internal\2", gb["base_url"])
    for prof in s.get("profiles", {}).values():
        if "base_url" in prof:
            prof["base_url"] = _LOCALHOST_RE.sub(r"\1host.docker.internal\2", prof["base_url"])
    return s


def _rewrite_config_for_container(cfg: dict, *, device_serial: str | None = None) -> dict:
    """Rewrite localhost/127.0.0.1 URLs in a claw-anything config dict.

    Covers model.base_url and judge.base_url so the container can reach
    a host-side inference server via host.docker.internal.

    When ``device_serial`` is provided, the allocated device becomes the sole
    ``android.emulator_pool`` entry (localhost/127.0.0.1 → host.docker.internal,
    matching the device_serial rewrite in ``_rewrite_oh_settings_for_container``).
    The inner claw-anything runs ``cmd_run`` (single, non-container) which is
    pool-first, so it picks up exactly this device.
    """
    c = copy.deepcopy(cfg)
    for section in ("model", "judge"):
        sec = c.get(section)
        if isinstance(sec, dict) and "base_url" in sec:
            sec["base_url"] = _LOCALHOST_RE.sub(r"\1host.docker.internal\2", sec["base_url"])
    if device_serial:
        container_serial = _ADB_SERIAL_RE.sub(r"host.docker.internal:\1", device_serial)
        c.setdefault("android", {})["emulator_pool"] = [container_serial]
    return c


def _prepare_config_for_container(
    config_path: Path,
    dest_dir: Path,
    *,
    device_serial: str | None = None,
) -> Path:
    """Copy config.yaml to dest_dir with localhost URLs rewritten and the
    allocated device pinned as the sole emulator_pool entry.

    Returns the path to the rewritten copy, which should be mounted
    into the container instead of the original file.
    """
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    rewritten = _rewrite_config_for_container(raw, device_serial=device_serial)
    dest = dest_dir / "claw-anything.yaml"
    dest.write_text(yaml.dump(rewritten, allow_unicode=True, default_flow_style=False), encoding="utf-8")
    return dest


def _prepare_oh_settings_for_container(
    settings_path: Path,
    dest_dir: Path,
    *,
    device_serial: str | None = None,
) -> Path:
    """Copy oh-settings to dest_dir, patch fields, and rewrite localhost URLs.

    Returns the path to the per-trial copy, which is mounted into the container
    instead of the original file so callers can inspect what was actually used.

    Transformations applied (in order):
      1. Patch mobile_gui.device_serial from the caller.
      2. Rewrite localhost / 127.0.0.1 → host.docker.internal in profile/GUI
         backend URLs and the ADB device_serial. The inner claw-anything then
         consumes the settings as-is — it does not need to know it's running
         inside a sandbox.

    Note: prompt_meta (apps/today) is intentionally NOT injected here. The
    inner claw-anything re-derives it from the mounted task.yaml via
    ``_build_settings`` (openharness_plugin_gen), which is the single source of
    truth for both container and non-container modes. Patching it here too
    would be redundant — OH consumes ``_build_settings`` output, not this file.
    """
    raw = json.loads(settings_path.read_text(encoding="utf-8"))
    if device_serial:
        raw.setdefault("mobile_gui", {})["device_serial"] = device_serial
    rewritten = _rewrite_oh_settings_for_container(raw)
    dest = dest_dir / "oh-settings.json"
    dest.write_text(json.dumps(rewritten, ensure_ascii=False, indent=2), encoding="utf-8")
    return dest


CONTAINER_LABEL = "app=claw-anything"


@dataclass(frozen=True)
class TrialLaunchSpec:
    """Per-trial inputs for ``run_trial_in_container``.

    This is the launcher's view of what one trial needs — deliberately a
    narrower window than ``cli.TrialRunSpec`` (which carries CLI-only fields
    like ``api_key`` / ``proxy`` / ``judge_model`` that the launcher must
    not see). Defining it here keeps the dependency arrow correct: CLI
    imports launcher, never the reverse.

    ``task_dir`` should point at the per-trial ``task_snapshot`` dir staged
    by the host — mounted **rw** so the inner stage can wipe ``fixtures/``
    once mock services have loaded their data.

    ``task_id`` is required (the snapshot dir is named ``task_snapshot``, so
    it can no longer be inferred from the dir name).
    """

    task_id: str
    task_dir: Path
    trace_dir: Path
    trace_id: str
    workspace_dir: Path
    image: str
    agent: str = "loop"
    config_path: Path | None = None
    oh_settings_path: Path | None = None
    disable_builtin_tools: bool = False
    device_serial: str | None = None
    timeout_seconds: int = 1800


def run_trial_in_container(
    spec: TrialLaunchSpec,
    *,
    container_cfg: ContainerConfig,
) -> tuple[Path, dict]:
    """Run one task trial in an isolated Docker container (trial-in-container).

    The container runs the full claw-anything pipeline internally:
    mock services start on container-local ports (no port_offset needed),
    the agent runs against them, trace and grading results are written to /out
    (mounted volume), and the container exits cleanly.

    ``spec`` bundles per-trial inputs (paths, ids, agent, device).
    ``container_cfg`` carries docker-run resource knobs (image, memory, cpu).

    Returns (trace_path, openai_data) read back from the mounted out volume,
    matching what ``cli._run_one_trial`` expects from the local-mode path.

    Raises RuntimeError if:
    - Container exits with non-zero code
    - No trace file is found after the container exits
    """
    effective_image = spec.image
    task_id = spec.task_id
    task_dir = Path(spec.task_dir).resolve()
    trace_dir = Path(spec.trace_dir).resolve()
    trace_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir = Path(spec.workspace_dir).resolve()
    workspace_dir.mkdir(parents=True, exist_ok=True)
    trace_id = spec.trace_id
    # Short log tag so parallel batch runs stay disambiguated in interleaved
    # logs without bloating each line. ``task_id`` follows the
    # ``<short>_<descriptive>`` convention (T14_T02_..., T01zh_email_triage),
    # so the first underscore segment is a stable, human-readable id.
    _short_task = task_id.split("_", 1)[0] if task_id else "?"
    tag = f"trial {_short_task}:{trace_id[:8]}" if trace_id else f"trial {_short_task}"
    config_path = spec.config_path
    oh_settings_path = spec.oh_settings_path
    disable_builtin_tools = spec.disable_builtin_tools
    timeout_seconds = spec.timeout_seconds
    device_serial = spec.device_serial
    agent = spec.agent

    # Per-task output dir on the host. The /out volume still maps the whole
    # trace_dir (the inner claw-anything generates its own trace_id subdir under
    # it and stays trace_id-unaware), but claw-anything's own copies (rewritten
    # config / patched oh-settings) live together under this per-task dir
    # instead of colliding at the trace_dir root across batch tasks.
    if trace_id:
        task_out = trace_dir / f"{task_id}_{trace_id[:8]}"
        task_out.mkdir(parents=True, exist_ok=True)
    else:
        task_out = trace_dir  # legacy layout (back-compat)

    # oh-settings carries OH's model / base-url / API key — it is consumed by
    # both the openharness (vanilla) and openharness-ext agents. For other
    # agents (loop) mounting the file and passing --oh-settings would be inert
    # dead weight, so gate it on the agent, mirroring the
    # --oh-disable-builtin-tools guard further down.
    _OH_AGENTS = ("openharness", "openharness-ext")
    use_oh_settings = bool(oh_settings_path) and agent in _OH_AGENTS

    # Vanilla openharness has no GUI/Android plugin and the claw-anything-oh
    # image ships no ``adb`` binary, so a device_serial passed for that agent
    # cannot be acted on. Warn-and-ignore rather than failing — batch runs may
    # be mixing GUI tasks (openharness-ext) and non-GUI tasks under a shared
    # device pool, and the openharness ones should still proceed unblocked.
    if device_serial and agent == "openharness":
        print(
            f"[{tag}] note: --agent openharness does not support GUI/Android; "
            f"ignoring device_serial={device_serial}"
        )
        device_serial = None

    cmd = ["docker", "run", "--rm"]

    # ── Resource + identity flags ────────────────────────────────────────────
    # Label every trial container so `cleanup` can find leftovers regardless of
    # which image they came from. Memory / CPU limits are best-effort: a
    # falsy value leaves the daemon default (unlimited) in place.
    cmd += ["--label", CONTAINER_LABEL]
    if container_cfg.memory_limit:
        cmd += ["--memory", str(container_cfg.memory_limit)]
    if container_cfg.cpu_limit:
        cmd += ["--cpus", str(container_cfg.cpu_limit)]

    # ── Volume mounts ────────────────────────────────────────────────────────
    # Mount the per-trial task snapshot at /opt/claw-anything/tasks/{task_id}/
    # so that relative paths in task.yaml service commands resolve correctly
    # against the container's WORKDIR (/opt/claw-anything). The mount is rw so
    # the inner stage can wipe fixtures/ after mock services have loaded it
    # (prevents agent from cat'ing fixtures via sandbox_shell_exec to bypass
    # the service-API boundary). The host-side snapshot was copied from
    # task_dir into <task_out>/task_snapshot/ before launching.
    task_mount = f"/opt/claw-anything/tasks/{task_id}"
    cmd += ["-v", f"{task_dir}:{task_mount}"]
    # Mount the per-trial task dir (not the whole model-date trace_dir) as /out
    # so the inner claw-anything writes alongside the host-side artifacts
    # (gui_init_artifacts, claw-anything.yaml, oh-settings.json) instead of under a
    # redundant second <model>_<date> layer.
    cmd += ["-v", f"{task_out}:/out"]
    # Agent-facing workspace, mounted writable so sandbox_file_write / shell
    # tools can produce artifacts. Host prepared its contents (sandbox_files +
    # logs/) before launching, and the inner cli decides workspace_root from
    # is_inside_trial_container() — so /workspace is hard-wired to this mount,
    # no extra CLI flag / env var needed.
    cmd += ["-v", f"{workspace_dir}:/workspace"]
    if config_path:
        # Rewrite localhost URLs so the container's judge/model reach host-side
        # inference via host.docker.internal, and pin the allocated device as
        # the sole emulator_pool entry so the (pool-first) inner claw-anything
        # picks it up. Copy lands in the per-task dir for easy inspection.
        rewritten_config = _prepare_config_for_container(
            Path(config_path).resolve(),
            task_out,
            device_serial=device_serial,
        )
        print(f"[{tag}] config rewritten for container: {rewritten_config}")
        cmd += ["-v", f"{rewritten_config}:/etc/claw-anything.yaml:ro"]
        # Temporary compat shim: keep a copy at the legacy trace_dir-root path
        # so anything still looking there does not break. Removable once all
        # consumers read from the per-task dir.
        if task_out != trace_dir:
            shutil.copyfile(rewritten_config, trace_dir / "claw-anything.yaml")
    if use_oh_settings:
        # Per-task copy so the exact settings used are inspectable.
        # Also patches device_serial if provided.
        assert oh_settings_path is not None  # use_oh_settings is True ⇒ non-None
        settings_copy = _prepare_oh_settings_for_container(
            Path(oh_settings_path).resolve(),
            task_out,
            device_serial=device_serial,
        )
        print(f"[{tag}] oh-settings patched for container: {settings_copy}")
        cmd += ["-v", f"{settings_copy}:/etc/oh-settings.json:ro"]
        if task_out != trace_dir:
            shutil.copyfile(settings_copy, trace_dir / "oh-settings.json")

    # ── Host networking ──────────────────────────────────────────────────────
    # Allows the container to reach host services (Android emulator ADB port,
    # model API server, etc.) via host.docker.internal (Linux requires explicit
    # --add-host; on Mac/Windows it's automatic).
    cmd += ["--add-host=host.docker.internal:host-gateway"]

    # ── User mapping ─────────────────────────────────────────────────────────
    # Run as the current user so trace files written to the mounted /out volume
    # are owned by the caller, not root.
    cmd += ["--user", f"{os.getuid()}:{os.getgid()}"]
    # The mapped UID often has no home directory in the container, causing ADB
    # to crash when it tries to create ~/.android. Point HOME at /tmp instead.
    cmd += ["-e", "HOME=/tmp"]
    # Redirect LLM call logs to the mounted /out volume so the container user
    # (non-root) can write them; the default /opt/llm_logs is root-owned.
    cmd += ["-e", "CLAW_ANYTHING_LLM_LOG_DIR=/out/llm_logs"]
    # Force unbuffered stdout/stderr so the inner claw-anything's logs stream to the
    # host in real time. Without this, Python block-buffers when stdout is a
    # pipe (not a TTY), so progress is invisible until the process exits and
    # the run looks hung even while it is working.
    cmd += ["-e", "PYTHONUNBUFFERED=1"]
    # Positive "I am the inner stage" marker. The inner claw-anything is launched
    # WITHOUT --trial-in-container, but absence of that flag is ambiguous (a
    # plain standalone run also lacks it). This env var is the unambiguous
    # signal that the mounted /etc/claw-anything.yaml and /etc/oh-settings.json are
    # already host-prepared and final: the inner stage must NOT re-resolve the
    # device pool, re-materialize oh-settings, or re-run GUI init (the host did
    # all of that before launching this container). This subsumes the old
    # --skip-gui-init flag, which has been removed.
    cmd += ["-e", f"{INSIDE_TRIAL_CONTAINER_ENV}=1"]

    # ── Image + container command ─────────────────────────────────────────────
    # Build the claw-anything subcommand args.
    claw_cmd = ["claw-anything", "run", "--task", task_mount, "--trace-dir", "/out", "--agent", agent]
    if config_path:
        claw_cmd += ["--config", "/etc/claw-anything.yaml"]
    if use_oh_settings:
        claw_cmd += ["--oh-settings", "/etc/oh-settings.json"]
    if disable_builtin_tools and agent in _OH_AGENTS:
        claw_cmd += ["--oh-disable-builtin-tools"]
    # GUI init, device-pool resolution and oh-settings materialization are all
    # host-side concerns the host already did before launching this container.
    # The inner stage skips them by reading CLAW_ANYTHING_INSIDE_TRIAL_CONTAINER
    # (injected as a docker env above) — no CLI flag needs to be threaded
    # through for this anymore.

    if device_serial:
        # TCP/IP serials (host:port) require adb connect before use; USB /
        # emulator serials are auto-discovered and must NOT be passed to connect.
        container_serial = re.sub(r"\b(?:localhost|127\.0\.0\.1)\b", "host.docker.internal", device_serial)
        if re.search(r":\d+$", container_serial):
            adb_pre = f"adb connect {shlex.quote(container_serial)}"
            inner = adb_pre + " && " + " ".join(shlex.quote(a) for a in claw_cmd)
        else:
            inner = " ".join(shlex.quote(a) for a in claw_cmd)
        cmd += ["--entrypoint", "/bin/bash", effective_image, "-c", inner]
    else:
        cmd.append(effective_image)
        # Strip leading "claw-anything" — it's the image ENTRYPOINT.
        cmd += claw_cmd[1:]

    print(f"[{tag}] launching: {shlex.join(cmd)}")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # merge stderr into stdout for unified streaming
        text=True,
        bufsize=1,
    )

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            print(f"[{tag}] {line}", end="", flush=True)
    finally:
        try:
            proc.wait(timeout=timeout_seconds + 60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)

    if proc.returncode != 0:
        raise RuntimeError(
            f"Container exited with code {proc.returncode}. "
            f"Check [{tag}] output above for details."
        )

    # ── Locate trace file ────────────────────────────────────────────────────
    # Container's claw-anything may create a model/date subdirectory inside /out,
    # so search recursively. Sort by mtime to get the latest file.
    candidates = sorted(
        trace_dir.rglob(f"{task_id}_*.jsonl"),
        key=lambda p: p.stat().st_mtime,
    )
    if not candidates:
        # Fallback: task_id might not match dir name exactly — try any .jsonl.
        candidates = sorted(
            trace_dir.rglob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
        )
    if not candidates:
        raise RuntimeError(
            f"No trace files found in {trace_dir} after container exit. "
            "The container may have crashed before writing output."
        )

    trace_path = candidates[-1]

    # ── Read back openai_data ─────────────────────────────────────────────────
    # The container's claw-anything run writes <trace>.openai.json alongside the trace.
    # Return it as openai_data so the caller can add host-side grading metadata
    # via _write_openai_trace (consistent with the local-mode trace API).
    openai_path = trace_path.with_suffix("").with_suffix(".openai.json")
    openai_data: dict = {}
    if openai_path.exists():
        try:
            openai_data = json.loads(openai_path.read_text(encoding="utf-8"))
        except Exception:
            pass  # non-fatal: caller will re-grade and overwrite anyway

    print(f"[{tag}] trace: {trace_path}")
    return trace_path, openai_data
