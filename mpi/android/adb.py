from __future__ import annotations

import subprocess
import sys
import time
from contextvars import ContextVar
from typing import List, Optional


class AdbError(RuntimeError):
    pass


# A live capture worker can issue several shell commands for one observation.
# Give them a shared deadline without changing timeouts for other adb users.
shell_deadline: ContextVar[Optional[float]] = ContextVar("shell_deadline", default=None)


# On Windows, hide the brief console flash that pops up every time we shell out
# to adb (which is itself a console subsystem exe). 0x08000000 is
# CREATE_NO_WINDOW. The flag is silently ignored on non-Windows platforms,
# but to be safe we only set it when we know we're on Windows.
_NO_WINDOW_KW = (
    {"creationflags": 0x08000000} if sys.platform == "win32" else {}
)


def run(
    args: List[str],
    serial: Optional[str] = None,
    timeout: float = 30.0,
) -> str:
    cmd = ["adb"]
    if serial:
        cmd += ["-s", serial]
    cmd += list(args)

    try:
        proc = subprocess.run(
            cmd,
            # Desktop owns stdin for JSON requests. An adb shell listening on
            # the same pipe can consume part of the next stop or export request.
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
            **_NO_WINDOW_KW,
        )
    except FileNotFoundError as exc:
        raise AdbError(
            "adb executable not found. Install Android Platform Tools "
            "(https://developer.android.com/tools/releases/platform-tools) "
            "and ensure `adb` is on PATH."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise AdbError(
            f"adb command timed out after {timeout:.1f}s: {' '.join(cmd)}"
        ) from exc

    # Windows adb can emit CRLF even for newlines printed by the device shell.
    # Give text parsers the same line boundaries on every host.
    stdout = proc.stdout.decode("utf-8", errors="replace").replace("\r\n", "\n")
    stderr = proc.stderr.decode("utf-8", errors="replace")

    if proc.returncode != 0:
        msg = stderr.strip() or stdout.strip() or f"exit code {proc.returncode}"
        raise AdbError(f"adb failed ({' '.join(cmd)}): {msg}")

    return stdout


def shell(
    command: str,
    serial: Optional[str] = None,
    timeout: float = 30.0,
) -> str:
    deadline = shell_deadline.get()
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AdbError("live sample deadline elapsed")
        timeout = min(remaining, timeout)
    return run(["shell", command], serial=serial, timeout=timeout)


def run_bytes(
    args: List[str],
    serial: Optional[str] = None,
    timeout: float = 30.0,
) -> bytes:
    """Like ``run()`` but returns raw stdout bytes — for binary payloads
    (e.g. ``exec-out`` of a PNG icon) where UTF-8 decoding would corrupt."""
    cmd = ["adb"]
    if serial:
        cmd += ["-s", serial]
    cmd += list(args)

    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
            **_NO_WINDOW_KW,
        )
    except FileNotFoundError as exc:
        raise AdbError(
            "adb executable not found. Install Android Platform Tools "
            "(https://developer.android.com/tools/releases/platform-tools) "
            "and ensure `adb` is on PATH."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise AdbError(
            f"adb command timed out after {timeout:.1f}s: {' '.join(cmd)}"
        ) from exc

    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        msg = stderr or f"exit code {proc.returncode}"
        raise AdbError(f"adb failed ({' '.join(cmd)}): {msg}")
    return proc.stdout


def exec_out(
    command: str,
    serial: Optional[str] = None,
    timeout: float = 30.0,
) -> bytes:
    """Run ``adb exec-out <command>`` and return raw stdout bytes.

    ``exec-out`` skips the TTY layer that ``adb shell`` uses, so binary
    output is delivered intact (no LF/CRLF translation, no ANSI mangling).
    """
    return run_bytes(["exec-out", command], serial=serial, timeout=timeout)


def install(
    apk_path: str,
    serial: Optional[str] = None,
    reinstall: bool = True,
    grant_runtime: bool = True,
    timeout: float = 120.0,
) -> str:
    """``adb install`` an APK. ``-r`` keeps app data across upgrades; ``-g``
    pre-grants runtime permissions (ignored by adb on devices that don't
    support it). Raises :class:`AdbError` on failure."""
    args = ["install"]
    if reinstall:
        args.append("-r")
    if grant_runtime:
        args.append("-g")
    args.append(apk_path)
    return run(args, serial=serial, timeout=timeout)


def uninstall(package: str, serial: Optional[str] = None, timeout: float = 60.0) -> str:
    return run(["uninstall", package], serial=serial, timeout=timeout)


def push(local: str, remote: str, serial: Optional[str] = None, timeout: float = 120.0) -> str:
    return run(["push", local, remote], serial=serial, timeout=timeout)


def forward(
    local: str,
    remote: str,
    serial: Optional[str] = None,
    timeout: float = 15.0,
) -> str:
    """Create an ``adb forward`` rule and return adb's stdout. Use ``local="tcp:0"``
    to let adb pick a free host port — the chosen port is returned on stdout."""
    return run(["forward", local, remote], serial=serial, timeout=timeout).strip()


def forward_remove(local: str, serial: Optional[str] = None, timeout: float = 15.0) -> None:
    try:
        run(["forward", "--remove", local], serial=serial, timeout=timeout)
    except AdbError:
        # Removing a rule that no longer exists is not an error worth surfacing.
        pass


def forward_list(serial: Optional[str] = None, timeout: float = 15.0) -> str:
    """List active forward rules (``<serial> tcp:<port> localabstract:<name>`` per
    line). With a serial, adb still prints all rules — callers filter by serial."""
    return run(["forward", "--list"], serial=serial, timeout=timeout)
