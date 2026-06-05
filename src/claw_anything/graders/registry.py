"""Task ID -> Grader dynamic loading from tasks/<id>/grader.py."""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import sys
import types
from pathlib import Path

from .base import AbstractGrader


def _install_claw_eval_compat() -> None:
    """Make legacy ``claw_eval``-rooted grader imports resolve to claw_anything.

    The ``benchmark/gui`` graders were generated against the predecessor
    package ``claw_eval`` and import exactly three modules:
    ``models.task`` (TaskDefinition), ``graders.base`` (AbstractGrader), and
    ``models.trace`` (DimensionScores, ToolDispatch, TraceMessage, MediaLoad).
    Everything but ``MediaLoad`` still exists under ``claw_anything``;
    ``MediaLoad`` is the multimodal type that was removed. Because those graders
    carry ``from __future__ import annotations``, ``MediaLoad`` only appears in
    lazy (string) annotations and never needs to be a real type at runtime, so a
    stub keeps the import line satisfied. Registering the aliases in
    ``sys.modules`` once lets every legacy grader load unmodified.
    """
    if "claw_eval" in sys.modules:
        return

    import claw_anything
    import claw_anything.graders
    import claw_anything.graders.base
    import claw_anything.models
    import claw_anything.models.task
    import claw_anything.models.trace as _trace

    # Non-mutating shim for models.trace: re-export the real symbols and add a
    # MediaLoad stub for the removed multimodal type (annotation-only at runtime).
    trace_shim = types.ModuleType("claw_eval.models.trace")
    for _attr in dir(_trace):
        if not _attr.startswith("__"):
            setattr(trace_shim, _attr, getattr(_trace, _attr))
    if not hasattr(trace_shim, "MediaLoad"):
        class MediaLoad:  # legacy multimodal type, removed from claw_anything
            pass

        trace_shim.MediaLoad = MediaLoad

    sys.modules["claw_eval"] = claw_anything
    sys.modules["claw_eval.models"] = claw_anything.models
    sys.modules["claw_eval.models.task"] = claw_anything.models.task
    sys.modules["claw_eval.models.trace"] = trace_shim
    sys.modules["claw_eval.graders"] = claw_anything.graders
    sys.modules["claw_eval.graders.base"] = claw_anything.graders.base


def get_grader(
    task_id: str,
    tasks_dir: str | Path = "tasks",
    task_dir: str | Path | None = None,
) -> AbstractGrader:
    """Dynamically load and instantiate a grader from tasks/<task_id>/grader.py.

    If ``task_dir`` is given, try loading from that directory first (handles
    cases where the directory name differs from the task_id in task.yaml).
    """
    grader_path = Path(tasks_dir) / task_id / "grader.py"

    # Fallback: use the actual task directory when task_id doesn't match dir name
    if not grader_path.exists() and task_dir is not None:
        alt_path = Path(task_dir) / "grader.py"
        if alt_path.exists():
            grader_path = alt_path

    if not grader_path.exists():
        raise FileNotFoundError(
            f"No grader found at {grader_path} for task_id={task_id!r}"
        )

    module_name = f"task_grader_{task_id}"
    spec = importlib.util.spec_from_file_location(module_name, grader_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load grader module from {grader_path}")

    module = importlib.util.module_from_spec(spec)
    _install_claw_eval_compat()  # legacy benchmark/gui graders import from claw_eval
    spec.loader.exec_module(module)

    # Find the AbstractGrader subclass in the module
    for _name, obj in inspect.getmembers(module, inspect.isclass):
        if obj.__module__ != module.__name__:
            continue
        if issubclass(obj, AbstractGrader) and obj is not AbstractGrader:
            return obj()

    raise ValueError(
        f"No AbstractGrader subclass found in {grader_path}"
    )
