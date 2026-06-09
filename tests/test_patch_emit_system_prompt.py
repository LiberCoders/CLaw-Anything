"""Tests for the build-time ``patch_emit_system_prompt`` patch script.

The patch makes OpenHarness's ``run_print_mode`` emit a one-shot
``{"type": "system_prompt", "text": ...}`` stream-json event. The anchor line
(``await start_runtime(bundle)``) also appears in the interactive path, so the
patch must splice out only the ``run_print_mode`` body and inject exactly once
there. It runs against both vanilla openharness-ai and the OH-Ext fork.

We load the shipped patch with a fake ``openharness`` module pointing at a
temp package so the test exercises the real script without the package
installed.
"""

from __future__ import annotations

import importlib.util
import py_compile
import sys
import types
from pathlib import Path

import pytest

PATCH_PATH = Path(__file__).resolve().parents[1] / "docker" / "oh" / "patch_emit_system_prompt.py"

MARKER = '"type": "system_prompt"'

# Two top-level async functions both carrying the anchor (mirrors the real
# app.py where run_interactive and run_print_mode share it), plus a nested
# helper def to confirm the slice stops at the next column-0 def, not the
# indented one.
SAMPLE_APP = (
    "import json\n"
    "\n"
    "async def run_interactive(*, output_format='text'):\n"
    "    bundle = await build_runtime()\n"
    "    await start_runtime(bundle)\n"
    "    return None\n"
    "\n"
    "async def run_print_mode(*, output_format='text'):\n"
    "    bundle = await build_runtime()\n"
    "    await start_runtime(bundle)\n"
    "    async def _helper(x):\n"
    "        return x\n"
    "    return None\n"
    "\n"
    "def other():\n"
    "    return None\n"
)


def _load_patch_for(pkg_dir: Path):
    """Load the shipped patch with ``openharness.__file__`` → pkg_dir."""
    fake = types.ModuleType("openharness")
    fake.__file__ = str(pkg_dir / "__init__.py")
    sys.modules["openharness"] = fake
    spec = importlib.util.spec_from_file_location("patch_emit_sp_under_test", PATCH_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # PATH is computed here from the fake module
    return mod


def _setup(tmp_path: Path, content: str = SAMPLE_APP) -> Path:
    ui = tmp_path / "ui"
    ui.mkdir()
    appfile = ui / "app.py"
    appfile.write_text(content)
    return appfile


def test_injects_exactly_once_inside_run_print_mode(tmp_path: Path) -> None:
    appfile = _setup(tmp_path)
    mod = _load_patch_for(tmp_path)
    mod.main()
    out = appfile.read_text()
    assert out.count(MARKER) == 1
    start, end = mod._slice_run_print_mode(out)
    assert MARKER in out[start:end]            # inside run_print_mode
    assert MARKER not in out[:start] + out[end:]  # run_interactive untouched
    py_compile.compile(str(appfile), doraise=True)


def test_idempotent(tmp_path: Path) -> None:
    appfile = _setup(tmp_path)
    mod = _load_patch_for(tmp_path)
    mod.main()
    mod.main()  # second build / re-run must not double-inject
    assert appfile.read_text().count(MARKER) == 1


def test_raises_when_anchor_missing_in_run_print_mode(tmp_path: Path) -> None:
    # Drop run_print_mode's anchor only (keep run_interactive's).
    broken = SAMPLE_APP.replace(
        "    await start_runtime(bundle)\n    async def _helper",
        "    async def _helper",
    )
    _setup(tmp_path, broken)
    mod = _load_patch_for(tmp_path)
    with pytest.raises(SystemExit):
        mod.main()


def test_raises_when_run_print_mode_missing(tmp_path: Path) -> None:
    nofunc = (
        "import json\n\n"
        "async def something_else():\n"
        "    await start_runtime(bundle)\n"
        "    return None\n"
    )
    _setup(tmp_path, nofunc)
    mod = _load_patch_for(tmp_path)
    with pytest.raises(SystemExit):
        mod.main()
