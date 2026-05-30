from .init_gui_task import _resolve_adb_bin, init_gui_task

# Re-export ``_resolve_adb_bin`` so non-init modules (e.g. ``runner.emulator_pool``)
# resolve adb the same way as the inject pipeline — using the same binary on
# the same PATH avoids "different adb versions" warnings and the connection
# drops they cause when two daemons race at boot.
resolve_adb_bin = _resolve_adb_bin

__all__ = ["init_gui_task", "resolve_adb_bin"]
