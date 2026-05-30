"""Load config.yaml with env-var expansion."""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


_ENV_RE = re.compile(r"\$\{(\w+)\}")

# Search order: CWD -> project root (where pyproject.toml lives)
_SEARCH_PATHS = [
    Path.cwd() / "config.yaml",
    Path(__file__).resolve().parent.parent.parent / "config.yaml",
]


def _expand_env(value: str) -> str | None:
    """Replace ${VAR} with os.environ[VAR]. Returns None if var is unset."""
    m = _ENV_RE.fullmatch(value.strip())
    if m:
        return os.environ.get(m.group(1))
    return value


def _walk_expand(obj):
    """Recursively expand ${ENV} references in string values."""
    if isinstance(obj, str):
        return _expand_env(obj)
    if isinstance(obj, dict):
        return {k: _walk_expand(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk_expand(v) for v in obj]
    return obj


class ModelConfig(BaseModel):
    api_key: str | None = None
    base_url: str | None = None
    model_id: str = "anthropic/claude-opus-4-6"
    input_modalities: list[str] = Field(default_factory=lambda: ["text"])
    system_prompt_prefix: str | None = None
    extra_body: dict | None = None
    session_cache_enabled: bool = False
    tls_verify: bool = True


class JudgeConfig(BaseModel):
    api_key: str | None = None
    base_url: str = "https://openrouter.ai/api/v1"
    model_id: str = "google/gemini-2.5-flash"
    enabled: bool = True
    tls_verify: bool = True


class DefaultsConfig(BaseModel):
    trace_dir: str = "traces"
    tasks_dir: str = "tasks"


class ContainerConfig(BaseModel):
    """Container resource limits for ``--trial-in-container`` Docker execution.

    The image itself is chosen per-trial by the active agent (see
    ``cli._default_image_for_agent``) and is overridable via ``--docker-image``;
    it's not a config concern. This block carries only the docker-run resource
    knobs that genuinely belong in static config.
    """

    memory_limit: str = "4g"
    cpu_limit: float = 2.0


class PromptFilesConfig(BaseModel):
    """Workspace markdown files to inject into system prompt."""

    agents_md: str | None = None
    soul_md: str | None = None
    user_md: str | None = None
    tools_md: str | None = None


class SkillEntry(BaseModel):
    """A skill descriptor shown in the default skills list."""

    name: str
    description: str
    path: str


class SkillsConfig(BaseModel):
    """Skills configuration for prompt composition."""

    default: list[SkillEntry] = Field(default_factory=list)
    load_via_tool_call: bool = True
    read_tool_name: str = "read"


class BehaviorRulesConfig(BaseModel):
    """Behavior-policy text included in system prompt."""

    safety: str = "No independent objective; do not pursue self-preservation, replication, or resource acquisition."
    tool_call_style: str = "For low-risk actions, call tools directly without narration; narrate only for complex tasks."
    reply_tags: str = "Use [[reply_to_current]] to control reply relationship when needed."
    silent_reply: str = "If no reply is needed, output NO_REPLY."
    heartbeat: str = "Heartbeat checks should return HEARTBEAT_OK when no action is needed."


class PromptConfig(BaseModel):
    """Configuration for dynamic system prompt construction."""

    enabled: bool = True
    strict_file_check: bool = False
    include_tool_schema: bool = True
    skill_mode: bool = False
    text_tool_call_mode: bool = False
    files: PromptFilesConfig = PromptFilesConfig()
    behavior_rules: BehaviorRulesConfig = BehaviorRulesConfig()
    skills: SkillsConfig = SkillsConfig()


class AgentConfig(BaseModel):
    """Configuration for agent selection and behavior."""

    agent_type: str = "loop"
    """Agent backend: ``loop`` (in-process), ``openharness`` (vanilla ``oh`` CLI),
    or ``openharness-ext`` (the OH-Ext fork with Android/GUI support). OH-family
    agents read model / base_url / api_key from their own settings file passed
    via ``--oh-settings``, so the top-level ``model`` block is ignored for them."""


class AndroidConfig(BaseModel):
    """Android emulator pool for GUI task evaluation.

    Two pool sources, checked in order:

      1. ``emulator_pool`` — externally pre-launched devices (USB/TCP serials).
         Non-empty wins; framework does not touch their lifecycle.
      2. ``auto_launch_count > 0`` — framework spins up that many emulator
         containers from ``emulator_image`` at batch start, distributes the
         allocated ``host:port`` serials to workers via the existing
         ``device_q`` queue, and kills+removes them at batch end. State
         cleanup between trials still relies on
         ``task.mobile_gui.init_gui_task`` (force-stop / pm clear / DB wipe);
         this layer does not save/load AVD snapshots.
    """

    emulator_pool: list[str] = Field(default_factory=list)
    emulator_image: str = "claw_anything:latest"
    """Image used for auto-launched emulator containers.

    Defaults to the MobileWorld-derived ``claw_anything:latest`` (underscore)
    that ships:
      - Docker-in-Docker daemon + pre-loaded service backends (mattermost,
        mastodon, postgres, redis, ...).
      - A rooted Android 14 AVD (``Pixel_8_API_34_x86_64``) with the
        claw-anything inject targets pre-installed (Fossify Calendar /
        Messages / Notes, Loop Habits, My Expenses, Markor, OpenTracks,
        Gmail clone, ...).
      - ADB listening on container port ``container_adb_port`` (5556 by
        default — MobileWorld's convention, not the upstream 5555).
    """

    auto_launch_count: int = 0
    """0 disables auto-launch (legacy behaviour). >0 = number of emulator
    containers to spin up at batch start."""

    container_adb_port: int = 5556
    """Port inside the emulator container that ADB listens on. ``-p
    <host_port>:<container_adb_port>`` maps it out. claw_anything:latest uses
    5556; vanilla AVD images use 5555."""

    privileged: bool = True
    """Pass ``--privileged`` to ``docker run`` for the emulator container.
    Required by the default image — it runs Docker-in-Docker for its
    backend service stack."""

    kvm: bool = True
    """Mount ``/dev/kvm`` into the emulator container. Without it the inner
    emulator falls back to software rendering and never finishes booting in
    any reasonable wall time. Set False only for hosts without KVM (which
    cannot meaningfully run this image regardless)."""

    preclean_avd_locks: bool = True
    """The shipped image's AVD directory has stale ``multiinstance.lock`` /
    ``hardware-qemu.ini.lock`` files baked into the image layer; without
    removal the emulator FATALs with "Running multiple emulators with the
    same AVD". When True the launcher wraps the entrypoint with a
    ``rm -f /root/.android/avd/*.avd/*.lock`` prelude."""

    host_port_start: int = 5556
    """Lowest host port the launcher will try when mapping each container's
    ADB port. Actual ports are allocated dynamically via bind-on-0 so this
    is only a hint / lower bound for human-readable logs."""

    boot_timeout_s: int = 600
    """Max seconds to wait for each emulator's ``sys.boot_completed`` + package
    manager readiness before giving up and rolling back the pool.
    claw_anything:latest takes ~3 min on first boot (DinD image-load + AVD
    cold boot), so the default sits at 10 minutes."""

    emulator_memory: str = ""
    """``docker run --memory`` for each emulator container. Empty string =
    daemon default (no limit). The default image is heavy (DinD + multiple
    backend services + AVD), so leave unset unless you've measured."""

    emulator_cpus: float = 0.0
    """``docker run --cpus`` for each emulator container. 0 = no limit."""


class Config(BaseModel):
    model: ModelConfig = ModelConfig()
    judge: JudgeConfig = JudgeConfig()
    defaults: DefaultsConfig = DefaultsConfig()
    container: ContainerConfig = ContainerConfig()
    prompt: PromptConfig = PromptConfig()
    agent: AgentConfig = AgentConfig()
    android: AndroidConfig = AndroidConfig()


def load_config(path: str | Path | None = None) -> Config:
    """Load config from YAML file with ${ENV} expansion.

    Searches config.yaml in CWD then project root if path is not given.
    Returns defaults if no file is found.
    """
    if path is not None:
        candidates = [Path(path)]
    else:
        candidates = _SEARCH_PATHS

    for p in candidates:
        if p.exists():
            with open(p) as f:
                raw = yaml.safe_load(f) or {}
            expanded = _walk_expand(raw)
            return Config.model_validate(expanded)

    return Config()
