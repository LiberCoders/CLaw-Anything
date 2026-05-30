"""Auto-launch + tear-down pool of Android emulator containers.

Used when ``config.android.auto_launch_count > 0`` and ``emulator_pool`` is
empty: claw-anything spins up N containers from ``config.android.emulator_image``
(default ``claw_anything:latest`` — MobileWorld-derived, ships a rooted
Pixel_8_API_34_x86_64 AVD with the claw-anything inject targets
pre-installed and DinD-loaded mock backends), waits for boot, hands out the
resulting ``localhost:<host_port>`` serials to ``cmd_batch`` / ``cmd_run``
workers, and on context exit kills + removes every container it created.

Per-trial state cleanup is **not** this module's job — ``task.mobile_gui``
already does ``am force-stop`` / ``pm clear`` / DB wipes inside the existing
inject pipeline. We do not snapshot-save/load AVDs.

Image-specific quirks handled here (configurable via ``AndroidConfig``):

  - Container ADB port is **5556**, not the upstream-standard 5555.
  - Container needs ``--privileged`` (it runs Docker-in-Docker internally).
  - Container needs ``--device /dev/kvm`` or the emulator never boots.
  - The shipped AVD directory has stale ``*.lock`` files baked into the
    image layer; the launcher wraps the entrypoint to delete them before
    the emulator starts, otherwise emulator FATALs with "Running multiple
    emulators with the same AVD".
  - Boot probing is done via ``docker exec <container> adb ...`` against the
    container's own SDK-bundled adb (no host adb dependency for readiness).
  - The emulator's adb transport binds to ``127.0.0.1:5555`` *inside* the
    container, so the published ``-p host:container_adb_port`` mapping reaches
    nothing until we start a ``socat`` bridge that re-exposes it on
    ``0.0.0.0:container_adb_port``. Only after that can the host's adb (and
    sibling trial containers via ``host.docker.internal``) drive the device.

References:
  - MobileWorld ``mw env run --count N --launch-interval 20``
    (https://github.com/Tongyi-MAI/MobileWorld) — same idea: pre-launch a
    container pool, distribute one per worker, kill at end.
"""

from __future__ import annotations

import concurrent.futures
import shlex
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass, field

#: Docker label applied to every emulator container so ``cmd_cleanup`` can
#: sweep leftovers (e.g. after a crash that bypassed ``stop_all``).
EMU_CONTAINER_LABEL = "app=claw-anything-emu"

#: ADB serial that the image's internal ``adb`` exposes for the single AVD.
#: Used for ``docker exec <container> adb -s <SERIAL> ...``.
_INNER_ADB_SERIAL = "emulator-5554"

#: TCP port the emulator's own adb transport binds to **inside** the container.
#: qemu hard-binds this to ``127.0.0.1`` (verified on claw_anything:latest), so
#: a published ``-p host:container_adb_port`` mapping reaches nothing on its own
#: — there is no listener on ``container_adb_port`` until we add the socat
#: bridge below. The host's adb (and sibling trial containers via
#: ``host.docker.internal``) can only drive the device once that bridge is up.
_QEMU_ADB_PORT = 5555

#: Post-boot bridge: expose the 127.0.0.1-bound qemu adb on all interfaces at
#: ``container_adb_port`` so the published host port becomes live. Started with
#: ``docker exec -d <container> socat ...`` (the image ships /usr/bin/socat).
#: NOTE: must be exec'd directly (no ``sh -c '... &'`` wrapper) or the detached
#: docker-exec session reaps the backgrounded child and the listener vanishes.
def _socat_bridge_argv(container_adb_port: int) -> list[str]:
    return [
        "socat",
        f"TCP-LISTEN:{container_adb_port},reuseaddr,fork",
        f"TCP:127.0.0.1:{_QEMU_ADB_PORT}",
    ]

#: The entrypoint wrapper that pre-cleans stale AVD locks. The image's
#: original entrypoint is ``/usr/local/bin/entrypoint.sh`` and CMD tails the
#: emulator + server logs (which keeps PID 1 alive). We chain through the
#: same entrypoint + CMD so the inner mobile-world API server, viewer, and
#: emulator boot identically — we only insert the lock-cleanup prelude.
_ENTRYPOINT_WRAPPER_CMD = (
    "rm -f /root/.android/avd/*.avd/*.lock; "
    "exec /usr/local/bin/entrypoint.sh "
    "/bin/sh -c 'tail -f /var/log/emulator.log /var/log/server.log'"
)


@dataclass
class _Instance:
    container_name: str
    host_port: int
    serial: str  # e.g. "localhost:32772"


@dataclass
class EmulatorPool:
    """Container pool for auto-launched Android emulators.

    Use as a context manager so ``stop_all`` runs even if the caller raises.

    Parameters
    ----------
    image:
        Docker image; expected to expose ADB on ``container_adb_port``.
    size:
        Number of containers to start.
    container_adb_port:
        Port inside the container that ADB listens on (5556 for the default
        ``claw_anything:latest`` image).
    privileged:
        Pass ``--privileged`` (required for the default DinD image).
    kvm:
        Pass ``--device /dev/kvm`` (required for any reasonable boot time).
    preclean_avd_locks:
        Wrap the entrypoint so stale AVD ``*.lock`` files are removed before
        the emulator starts.
    host_port_start:
        Hint / lower-bound for log readability — actual host ports are picked
        dynamically via ``socket.bind(("", 0))`` to avoid race conditions.
    boot_timeout_s:
        Max seconds to wait for each emulator's boot probe.
    memory, cpus:
        Optional ``--memory`` / ``--cpus`` per container. Empty / 0 = no limit.
    boot_stagger_s:
        Per-container delay added to the start of its boot poll, so N
        concurrent boots don't all hammer the CPU at the same instant.
    """

    image: str
    size: int
    container_adb_port: int = 5556
    privileged: bool = True
    kvm: bool = True
    preclean_avd_locks: bool = True
    host_port_start: int = 5556
    boot_timeout_s: int = 600
    memory: str = ""
    cpus: float = 0.0
    boot_stagger_s: int = 10

    _instances: list[_Instance] = field(default_factory=list, init=False)
    _started: bool = field(default=False, init=False)

    # ── lifecycle ──────────────────────────────────────────────────────────

    def __enter__(self) -> "EmulatorPool":
        try:
            self.start_all()
        except BaseException:
            self.stop_all()
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop_all()

    def serials(self) -> list[str]:
        """Serials of every booted instance, in start order."""
        return [inst.serial for inst in self._instances]

    # ── start ──────────────────────────────────────────────────────────────

    def start_all(self) -> list[str]:
        """Launch ``size`` containers in parallel and block until all are ready.

        Returns the list of allocated ``localhost:<host_port>`` serials. On any
        failure (docker run nonzero, boot timeout, KeyboardInterrupt) the pool
        is rolled back via ``stop_all`` and the underlying exception is
        re-raised.
        """
        if self._started:
            raise RuntimeError("EmulatorPool.start_all called twice")
        self._started = True

        if self.size <= 0:
            return []

        print(f"[emu-pool] starting {self.size} emulator container(s) from {self.image}")

        # 1. Reserve N free host ports up-front, holding the sockets until we
        #    `docker run -p` against them.
        held_sockets: list[socket.socket] = []
        ports: list[int] = []
        try:
            for _ in range(self.size):
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.bind(("", 0))
                _, port = s.getsockname()
                held_sockets.append(s)
                ports.append(port)
        except OSError:
            for s in held_sockets:
                s.close()
            raise

        # 2. docker run -d each container (release the held socket immediately
        #    before each run so docker can bind the port).
        try:
            for port, sock in zip(ports, held_sockets):
                sock.close()
                inst = self._docker_run(port)
                self._instances.append(inst)
                print(f"[emu-pool] launched {inst.container_name} on {inst.serial}")
        except BaseException:
            for s in held_sockets:
                try:
                    s.close()
                except OSError:
                    pass
            self.stop_all()
            raise

        # 3. Wait for each emulator to boot. Stagger poll start to avoid
        #    pegging the CPU with N concurrent boots.
        try:
            self._wait_all_booted()
        except BaseException:
            self.stop_all()
            raise

        return self.serials()

    def _docker_run(self, host_port: int) -> _Instance:
        name = f"claw-emu-{uuid.uuid4().hex[:8]}"
        cmd: list[str] = [
            "docker", "run", "-d",
            "--rm",
            "--label", EMU_CONTAINER_LABEL,
            "--name", name,
            "-p", f"{host_port}:{self.container_adb_port}",
        ]
        if self.privileged:
            cmd.append("--privileged")
        if self.kvm:
            cmd += ["--device", "/dev/kvm"]
        if self.memory:
            cmd += ["--memory", str(self.memory)]
        if self.cpus and self.cpus > 0:
            cmd += ["--cpus", str(self.cpus)]
        if self.preclean_avd_locks:
            # Override entrypoint with a /bin/bash -c wrapper that removes
            # stale AVD locks then execs the image's real entrypoint with the
            # image's real CMD. The original CMD is hardcoded to match the
            # claw_anything image — if the image changes its CMD we'd need to
            # introspect it; for now this is the lowest-risk static fix.
            cmd += ["--entrypoint", "/bin/bash", self.image, "-c", _ENTRYPOINT_WRAPPER_CMD]
        else:
            cmd.append(self.image)

        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(
                f"[emu-pool] docker run failed for {name}: "
                f"{res.stderr.strip() or res.stdout.strip()}"
            )
        return _Instance(
            container_name=name,
            host_port=host_port,
            serial=f"localhost:{host_port}",
        )

    # ── boot probe ─────────────────────────────────────────────────────────

    def _wait_all_booted(self) -> None:
        def _wait_one(idx_inst: tuple[int, _Instance]) -> None:
            idx, inst = idx_inst
            time.sleep(idx * self.boot_stagger_s)
            self._poll_ready(inst)
            print(f"[emu-pool] booted: {inst.serial} ({inst.container_name})")
            # Bridge the 127.0.0.1-bound qemu adb out to the published port,
            # then confirm the host's adb can actually reach the device — that
            # host-reachable serial is what workers / inject consume.
            self._expose_adb(inst)
            self._host_connect(inst)
            print(f"[emu-pool] adb reachable: {inst.serial} ({inst.container_name})")

        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, self.size)) as ex:
            futures = [ex.submit(_wait_one, pair) for pair in enumerate(self._instances)]
            errors: list[BaseException] = []
            for fut in concurrent.futures.as_completed(futures):
                try:
                    fut.result()
                except BaseException as exc:
                    errors.append(exc)
        if errors:
            raise errors[0]

    def _exec_adb(self, inst: _Instance, *adb_args: str) -> tuple[int, str]:
        """Run ``adb <args>`` inside the emulator container.

        The host typically lacks adb; the container ships
        ``/opt/android-sdk/platform-tools/adb`` on $PATH. Returns
        ``(returncode, stdout+stderr stripped)``.
        """
        cmd = ["docker", "exec", inst.container_name, "adb", *adb_args]
        res = subprocess.run(cmd, capture_output=True, text=True)
        return res.returncode, (res.stdout + res.stderr).strip()

    def _expose_adb(self, inst: _Instance) -> None:
        """Start the socat bridge inside the container (idempotent-ish).

        Kills any prior bridge first so a re-run doesn't stack listeners, then
        launches a fresh detached ``socat``. The emulator's adb is bound to
        ``127.0.0.1:_QEMU_ADB_PORT``; this re-publishes it on
        ``0.0.0.0:container_adb_port`` so the ``-p`` mapping resolves.
        """
        subprocess.run(
            ["docker", "exec", inst.container_name,
             "pkill", "-f", f"socat.*{self.container_adb_port}"],
            capture_output=True, text=True,
        )
        argv = ["docker", "exec", "-d", inst.container_name, *_socat_bridge_argv(self.container_adb_port)]
        res = subprocess.run(argv, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(
                f"[emu-pool] failed to start adb bridge in {inst.container_name}: "
                f"{res.stderr.strip() or res.stdout.strip()}"
            )

    def _host_connect(self, inst: _Instance) -> None:
        """Make the host's adb attach to ``inst.serial`` and reach ``device``.

        The bridge needs a moment to bind; retry ``adb connect`` + ``get-state``
        until the device is online or we exhaust the boot deadline. Uses the
        same adb binary as the inject pipeline (``resolve_adb_bin``) to avoid
        version-skew daemon restarts mid-run.
        """
        from ..task.mobile_gui import resolve_adb_bin

        adb = resolve_adb_bin(None)
        deadline = time.monotonic() + self.boot_timeout_s
        last = ""
        while time.monotonic() < deadline:
            subprocess.run([adb, "connect", inst.serial], capture_output=True, text=True)
            res = subprocess.run(
                [adb, "-s", inst.serial, "get-state"],
                capture_output=True, text=True,
            )
            last = (res.stdout + res.stderr).strip()
            if res.returncode == 0 and last == "device":
                return
            subprocess.run([adb, "disconnect", inst.serial], capture_output=True, text=True)
            time.sleep(2)
        raise TimeoutError(
            f"[emu-pool] host adb could not reach {inst.serial} "
            f"within {self.boot_timeout_s}s (last state: {last!r})"
        )

    def _poll_ready(self, inst: _Instance) -> None:
        deadline = time.monotonic() + self.boot_timeout_s
        while time.monotonic() < deadline:
            rc, out = self._exec_adb(
                inst, "-s", _INNER_ADB_SERIAL, "shell", "getprop", "sys.boot_completed",
            )
            if rc == 0 and out.strip() == "1":
                rc2, pm_out = self._exec_adb(
                    inst, "-s", _INNER_ADB_SERIAL, "shell", "pm", "path", "android",
                )
                if rc2 == 0 and pm_out.strip().startswith("package:"):
                    return
            time.sleep(3)
        raise TimeoutError(
            f"[emu-pool] {inst.serial} did not become ready within {self.boot_timeout_s}s"
        )

    # ── stop ───────────────────────────────────────────────────────────────

    def stop_all(self) -> None:
        """Best-effort tear-down. Never raises.

        Tries ``adb emu kill`` (via ``docker exec``) first for a graceful
        shutdown, then ``docker rm -f`` unconditionally so a stuck emu kill
        cannot leak a container.
        """
        if not self._instances:
            return
        try:
            from ..task.mobile_gui import resolve_adb_bin
            host_adb = resolve_adb_bin(None)
        except Exception:  # noqa: BLE001 - adb resolution must not block teardown
            host_adb = None
        for inst in self._instances:
            if host_adb:
                try:
                    subprocess.run(
                        [host_adb, "disconnect", inst.serial],
                        capture_output=True, text=True, timeout=10,
                    )
                except (subprocess.SubprocessError, OSError) as exc:
                    print(f"[emu-pool] adb disconnect {inst.serial} failed (ignored): {exc}")
            try:
                subprocess.run(
                    ["docker", "exec", inst.container_name, "adb",
                     "-s", _INNER_ADB_SERIAL, "emu", "kill"],
                    capture_output=True, text=True, timeout=10,
                )
            except (subprocess.SubprocessError, OSError) as exc:
                print(f"[emu-pool] adb emu kill on {inst.container_name} failed (ignored): {exc}")
            try:
                subprocess.run(
                    ["docker", "rm", "-f", inst.container_name],
                    capture_output=True, text=True, timeout=30,
                )
            except (subprocess.SubprocessError, OSError) as exc:
                print(f"[emu-pool] docker rm -f {inst.container_name} failed: {exc}")
        n = len(self._instances)
        self._instances.clear()
        print(f"[emu-pool] stop_all: removed {n} container(s)")


def cleanup_emulator_containers() -> list[str]:
    """Return container IDs labelled as claw-anything emulators.

    Used by ``cli.cmd_cleanup`` to sweep leftovers from crashes. Returns the
    IDs but does not remove them — the caller batches the removal with its
    other cleanup targets.
    """
    try:
        res = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"label={EMU_CONTAINER_LABEL}"],
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"[cleanup] docker query failed (emu label): {exc}")
        return []
    return res.stdout.split()
