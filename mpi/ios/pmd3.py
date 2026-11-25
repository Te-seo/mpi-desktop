"""pymobiledevice3 CLI adapter for Windows-compatible iOS access."""

from __future__ import annotations

import base64
import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from ..schema import DeviceInfo, FrameStats, MetricsSnapshot, PackageInfo, Platform
from . import parsers


class Pymobiledevice3Error(RuntimeError):
    """Raised when the pymobiledevice3 CLI is missing or fails."""


logger = logging.getLogger(__name__)


_PID_PATTERNS = (
    re.compile(r"\bpid\s*[:=]\s*(\d+)\b", re.IGNORECASE),
    re.compile(r"\bpid\s+(\d+)\b", re.IGNORECASE),
    re.compile(r"\bprocess(?:Identifier|ID)?\s*[:=]\s*(\d+)\b", re.IGNORECASE),
    re.compile(r"\bProcess\s+(\d+)\b", re.IGNORECASE),
)
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_DLL_SEARCH_LOCK = threading.Lock()
_DEVICE_LIST_LOCK = threading.Lock()
_DEVICE_METADATA_LOCK = threading.Lock()
_LOCKDOWN_INFO_CACHE: dict[str, Any] = {}
_IDENTITY_RETRY_AT: dict[str, float] = {}

# tunneld's default HTTP API (pymobiledevice3.tunneld.api.TUNNELD_DEFAULT_ADDRESS).
# The timeout is a hard cap on the localhost probe: when tunneld is up the GET
# returns in ~ms, and when it is down this bounds the per-call cost on the
# (already failing) no-tunnel path. Some Windows hosts drop closed-port SYNs
# instead of refusing, so a missing tunneld costs the full timeout rather than
# failing instantly — keep it short.
_TUNNELD_DEFAULT_ADDRESS = ("127.0.0.1", 49151)
_TUNNELD_TIMEOUT = 1.0
_PMD3_REQUIREMENT = "pymobiledevice3>=9.13,<10"

# DVT subcommands that require a RemoteServiceDiscovery tunnel on iOS 17+. Each
# entry is (command prefix, length) so the --rsd HOST PORT pair can be spliced in
# right after the positional subcommand and before any of its own options.
_DVT_RSD_PREFIXES = (
    (["developer", "dvt", "process-id-for-bundle-id"], 3),
    (["developer", "dvt", "launch"], 3),
    (["developer", "dvt", "kill"], 3),
    (["developer", "dvt", "sysmon", "process", "single"], 5),
)
_DVT_TUNNEL_HINT = (
    "DVT metrics on recent iOS versions require a pymobiledevice3 tunnel. "
    "Use the iOS readiness badge to install/start the background tunnel, "
    "then keep the iPhone unlocked and connected before starting capture."
)


# Memoized result of probing an external Python for the pymobiledevice3 module
# in frozen (PyInstaller) builds: None = not probed yet, [] = probed and absent.
_FROZEN_FALLBACK: Optional[list[str]] = None


def _frozen_python_fallback() -> Optional[list[str]]:
    """In frozen builds, fall back to ``python -m pymobiledevice3`` when the
    standalone executable is not on PATH but a system Python has the module
    installed (e.g. after the in-app pip install). The probe spawns a Python
    once and is memoized; ``_reset_command_cache`` clears it after installs."""
    global _FROZEN_FALLBACK
    if _FROZEN_FALLBACK is not None:
        return _FROZEN_FALLBACK or None
    result: list[str] = []
    interpreter = _system_python()
    if interpreter:
        probe = (
            "import importlib.util as u, sys;"
            "sys.exit(0 if u.find_spec('pymobiledevice3') else 1)"
        )
        try:
            proc = subprocess.run(
                [interpreter, "-c", probe],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=20.0,
                check=False,
            )
            if proc.returncode == 0:
                result = [interpreter, "-m", "pymobiledevice3"]
        except (OSError, subprocess.SubprocessError):
            pass
    _FROZEN_FALLBACK = result
    return result or None


def _reset_command_cache() -> None:
    global _FROZEN_FALLBACK
    _FROZEN_FALLBACK = None


def _system_python() -> Optional[str]:
    """Locate a non-frozen Python interpreter for module probes / pip installs."""
    if not getattr(sys, "frozen", False):
        return sys.executable
    for name in ("python", "python3", "py"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _command() -> list[str]:
    override = os.environ.get("MPI_PYMOBILEDEVICE3") or os.environ.get(
        "MPI_PMD3"
    )
    if override:
        return [override]

    exe = shutil.which("pymobiledevice3")
    if exe:
        return [exe]

    if not getattr(sys, "frozen", False):
        try:
            if importlib.util.find_spec("pymobiledevice3") is not None:
                return [sys.executable, "-m", "pymobiledevice3"]
        except (ImportError, ValueError):
            pass
    else:
        fallback = _frozen_python_fallback()
        if fallback:
            return list(fallback)

    raise Pymobiledevice3Error(
        "pymobiledevice3 executable not found. Install pymobiledevice3 for "
        "Windows iOS capture and ensure it is on PATH, or set "
        "MPI_PYMOBILEDEVICE3 to the executable path."
    )


def _streamer_python() -> Optional[str]:
    """Resolve a Python *interpreter* that can ``import pymobiledevice3``.

    The persistent streamer (:mod:`mpi.ios.pmd3_stream`) runs as a
    separate process driven by the pymobiledevice3 Python API, so unlike
    :func:`_command` we need an actual interpreter, never the standalone CLI
    launcher. In source checkouts that is the running interpreter; in frozen
    builds pymobiledevice3 is not bundled, so reuse the system-Python probe
    that the CLI fallback already validated.
    """
    if not getattr(sys, "frozen", False):
        try:
            if importlib.util.find_spec("pymobiledevice3") is not None:
                return sys.executable
        except (ImportError, ValueError):
            pass
    fallback = _frozen_python_fallback()
    if fallback:
        return fallback[0]
    return None


def _streamer_script_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "pmd3_stream.py")


def build_streamer_command(
    serial: str,
    pid: int,
    *,
    bundle_id: str = "",
    interval_ms: int = 1000,
) -> list[str]:
    """Build the argv for the persistent metrics streamer.

    Raises :class:`Pymobiledevice3Error` when no suitable interpreter or the
    streamer script can be found, so callers can fall back to the slower
    single-shot path.
    """
    interpreter = _streamer_python()
    if not interpreter:
        raise Pymobiledevice3Error(
            "no Python interpreter with pymobiledevice3 found to run the iOS "
            "metrics streamer"
        )
    script = _streamer_script_path()
    if not os.path.isfile(script):
        raise Pymobiledevice3Error(f"iOS streamer script not found: {script}")

    host, port = _tunneld_address()
    cmd = [
        interpreter,
        script,
        "--udid",
        serial,
        "--pid",
        str(pid),
        "--tunneld",
        f"{host}:{port}",
        "--interval-ms",
        str(interval_ms),
    ]
    if bundle_id:
        cmd += ["--bundle-id", bundle_id]
    # Reuse the same RSD resolution as the CLI path (env override or tunneld
    # discovery) so an operator-pinned tunnel keeps working for streaming too.
    rsd = _rsd_args(serial)
    if rsd:  # ["--rsd", host, port]
        cmd += ["--rsd", rsd[1], rsd[2]]
    return cmd


def build_temperature_guard_command(
    serial: str,
    bundle_id: str,
    threshold_c: float,
    *,
    required_hits: int = 2,
    interval_ms: int = 1000,
    first_reading_timeout_sec: float = 45.0,
    preview_only: bool = False,
) -> list[str]:
    """Build argv for the iOS temperature sidecar.

    ``preview_only`` keeps the prepared diagnostics connection in a strictly
    read-only loop.  It must never enter the ProcessControl launch path.
    """
    if not bundle_id:
        raise Pymobiledevice3Error("bundle id required for iOS temperature guard")
    # Reuse build_streamer_command so interpreter/script/tunneld/RSD resolution
    # remains identical to ordinary persistent metrics capture.  pid=0 is safe:
    # guard mode prepares DVT without launching and obtains the real pid through
    # ProcessControl lookup-or-launch before Sysmontap starts.
    cmd = build_streamer_command(
        serial,
        0,
        bundle_id=bundle_id,
        interval_ms=interval_ms,
    )
    cmd += [
        "--temperature-preview" if preview_only else "--temperature-guard",
        "--threshold-c",
        str(float(threshold_c)),
        "--required-hits",
        str(int(required_hits)),
        "--temperature-timeout-sec",
        str(float(first_reading_timeout_sec)),
    ]
    return cmd


def _is_under(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.abspath(path), root]) == root
    except (OSError, ValueError):
        return False


def _subprocess_env(udid: Optional[str]) -> dict[str, str]:
    env = os.environ.copy()
    if udid:
        env["PYMOBILEDEVICE3_UDID"] = udid
    tunnel = os.environ.get("MPI_PYMOBILEDEVICE3_TUNNEL")
    if tunnel and not env.get("PYMOBILEDEVICE3_TUNNEL"):
        env["PYMOBILEDEVICE3_TUNNEL"] = tunnel
    home_override = os.environ.get("MPI_PYMOBILEDEVICE3_HOME")
    if home_override:
        env["USERPROFILE"] = home_override
        env["HOME"] = home_override

    meipass = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and meipass:
        root = os.path.abspath(str(meipass))
        parts = [
            part
            for part in env.get("PATH", "").split(os.pathsep)
            if part and not _is_under(part, root)
        ]
        env["PATH"] = os.pathsep.join(parts)
    return env


def _parse_rsd_spec(raw: str) -> list[str]:
    normalized = raw.replace(",", " ")
    if " " in normalized:
        parts = [part for part in normalized.split() if part]
    elif ":" in normalized:
        host, port = normalized.rsplit(":", 1)
        parts = [host, port]
    else:
        parts = []
    if len(parts) != 2:
        raise Pymobiledevice3Error(
            "MPI_PYMOBILEDEVICE3_RSD must be formatted as 'HOST PORT' "
            "or 'HOST:PORT'."
        )
    return ["--rsd", parts[0], parts[1]]


def _tunneld_address() -> tuple[str, int]:
    raw = os.environ.get("MPI_PYMOBILEDEVICE3_TUNNELD", "").strip()
    if not raw:
        return _TUNNELD_DEFAULT_ADDRESS
    host, sep, port = raw.rpartition(":")
    if sep and host and port.isdigit():
        return (host, int(port))
    return (raw, _TUNNELD_DEFAULT_ADDRESS[1])


def _discover_rsd(udid: Optional[str]) -> Optional[tuple[str, str]]:
    """Best-effort lookup of a device's tunnel RSD address from tunneld.

    iOS 17+ developer (DVT) services are only reachable over a RemoteXPC tunnel.
    When ``pymobiledevice3 remote tunneld`` is running it exposes the active
    tunnels at ``http://127.0.0.1:49151/`` as ``{udid: [{tunnel-address,
    tunnel-port, interface}]}``. Returns ``(host, port)`` for the target device,
    or ``None`` when tunneld is unreachable or has no tunnel for it — in which
    case the caller falls back to pymobiledevice3's own tunneld auto-retry.
    """
    host, port = _tunneld_address()
    url = f"http://{host}:{port}/"
    try:
        with urllib.request.urlopen(url, timeout=_TUNNELD_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not isinstance(payload, dict) or not payload:
        return None
    entries = None
    if udid:
        entries = _tunneld_entries_for_udid(payload, udid)
    elif len(payload) == 1:
        # With no specific device requested, a lone active tunnel is unambiguous.
        entries = next(iter(payload.values()))
    return _first_valid_rsd(entries)


def _tunneld_entries_for_udid(payload: dict[Any, Any], udid: str) -> Any:
    if udid in payload:
        return payload[udid]
    normalized = udid.casefold()
    for key, value in payload.items():
        if str(key).casefold() == normalized:
            return value
    return None


def _lookup_tunneld_value(
    record: dict[Any, Any], *keys: str
) -> Optional[str]:
    wanted = {_normalize_tunnel_key(key) for key in keys}
    for raw_key, value in record.items():
        if _normalize_tunnel_key(raw_key) in wanted:
            if isinstance(value, (str, int)):
                return str(value)
            if value is not None and not isinstance(value, (list, dict, tuple, set)):
                return str(value)
    return None


def _normalize_tunnel_key(value: Any) -> str:
    return str(value).strip().replace("_", "-").casefold()


def _first_valid_rsd(entries: Any) -> Optional[tuple[str, str]]:
    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        rsd_spec = _lookup_tunneld_value(entry, "rsd")
        if rsd_spec:
            try:
                parsed = _parse_rsd_spec(rsd_spec)
            except Pymobiledevice3Error:
                parsed = []
            else:
                if len(parsed) == 3:
                    return parsed[1], parsed[2]

        address = _lookup_tunneld_value(
            entry,
            "tunnel-address",
            "tunnelAddress",
            "address",
            "rsd-host",
            "rsd-hostname",
            "ip",
            "host",
        )
        rsd_port = _lookup_tunneld_value(
            entry,
            "tunnel-port",
            "tunnelPort",
            "port",
            "rsd-port",
            "rsd_port",
        )
        if address and rsd_port:
            return str(address), str(rsd_port)
    return None


def _rsd_args(udid: Optional[str] = None) -> list[str]:
    raw = os.environ.get("MPI_PYMOBILEDEVICE3_RSD", "").strip()
    if raw:
        return _parse_rsd_spec(raw)
    discovered = _discover_rsd(udid)
    if discovered:
        return ["--rsd", discovered[0], discovered[1]]
    return []


def _inject_rsd_args(args: list[str], udid: Optional[str] = None) -> list[str]:
    for prefix, length in _DVT_RSD_PREFIXES:
        if args[:length] == prefix:
            rsd = _rsd_args(udid)
            return [*args[:length], *rsd, *args[length:]] if rsd else args
    return args


@contextmanager
def _external_program_context():
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        yield
        return

    with _DLL_SEARCH_LOCK:
        kernel32 = None
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.SetDllDirectoryW(None)
            yield
        finally:
            meipass = getattr(sys, "_MEIPASS", None)
            if meipass and kernel32 is not None:
                try:
                    kernel32.SetDllDirectoryW(str(meipass))
                except Exception:
                    pass


def run(args: Iterable[str], *, udid: Optional[str] = None, timeout: float = 60.0) -> str:
    command_args = _inject_rsd_args(list(args), udid)
    cmd = [*_command(), *command_args]
    env = _subprocess_env(udid)
    try:
        # SetDllDirectory is process-global in a frozen Windows build, so only
        # the short process-creation window must be serialized. Waiting for the
        # child inside this context made otherwise-parallel USB/network/lockdown
        # commands run one after another. Popen lets us restore the DLL setting
        # immediately after creation and wait outside the lock.
        with _external_program_context():
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
    except FileNotFoundError as exc:
        raise Pymobiledevice3Error(
            f"pymobiledevice3 executable not found: {cmd[0]}"
        ) from exc
    try:
        stdout_text, stderr_text = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.communicate()
        raise Pymobiledevice3Error(
            f"pymobiledevice3 command timed out after {timeout:.1f}s: {' '.join(cmd)}"
        ) from exc

    stderr = _strip_ansi(stderr_text or "").strip()
    stdout = _strip_ansi(stdout_text or "").strip()
    if proc.returncode != 0:
        msg = stderr or stdout
        raise Pymobiledevice3Error(
            _format_error(cmd, msg or f"exit {proc.returncode}")
        )
    if _contains_cli_error(stderr) or (
        _contains_cli_error(stdout) and not stdout.lstrip().startswith(("{", "["))
    ):
        raise Pymobiledevice3Error(_format_error(cmd, stderr or stdout))
    return stdout_text


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text)


def _contains_cli_error(text: str) -> bool:
    return bool(re.search(r"\b(ERROR|CRITICAL)\b", _strip_ansi(text), re.IGNORECASE))


def _format_error(cmd: list[str], message: str) -> str:
    clean = " ".join(_strip_ansi(message).split())
    detail = f"pymobiledevice3 failed ({' '.join(cmd)}): {clean}"
    command_text = " ".join(cmd)
    if "developer dvt" in command_text and _looks_like_tunnel_error(clean):
        detail += f". {_DVT_TUNNEL_HINT}"
    return detail


def _looks_like_tunnel_error(message: str) -> bool:
    text = message.lower()
    return any(
        needle in text
        for needle in (
            "device not found",
            "tunneld",
            "tunnel",
            "rsd",
            "remoteservicediscovery",
            "remotexpc",
            "connection refused",
            "winerror 10061",
            "no route to host",
        )
    )


def _decode_json_document(text: str) -> Any:
    stripped = text.strip()
    if not stripped:
        return {}
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for idx, char in enumerate(stripped):
        if char not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(stripped[idx:])
            return value
        except json.JSONDecodeError:
            continue
    raise Pymobiledevice3Error("pymobiledevice3 did not return JSON output")


def run_json(
    args: Iterable[str], *, udid: Optional[str] = None, timeout: float = 60.0
) -> Any:
    return _decode_json_document(run(args, udid=udid, timeout=timeout))


def _pmd3_version_major(value: Optional[str]) -> Optional[int]:
    parsed = _pmd3_version_tuple(value)
    return parsed[0] if parsed is not None else None


def _pmd3_version_tuple(value: Optional[str]) -> Optional[tuple[int, int]]:
    if not value:
        return None
    match = re.search(r"(?<!\d)(\d+)\.(\d+)", value)
    return (int(match.group(1)), int(match.group(2))) if match else None


def _unsupported_version_message(version: str) -> str:
    return (
        "unsupported_pymobiledevice3: iOS temperature guard requires "
        f"pymobiledevice3 >=9.13,<10 (found {version})"
    )


def preflight() -> dict:
    """Report whether the pymobiledevice3 CLI is usable, without raising.

    Returns ``{"available": bool, "command": [...], "version": str | None}``
    on success, or ``{"available": False, "error": str, "installable": bool}``
    when the CLI cannot be resolved. ``installable`` tells the UI whether an
    in-app pip install could fix it (a Python interpreter is reachable).
    """
    try:
        cmd = _command()
    except Pymobiledevice3Error as exc:
        return {
            "available": False,
            "error": str(exc),
            "installable": _system_python() is not None,
        }
    version: Optional[str] = None
    try:
        output = run(["version"], timeout=30.0).strip()
        if output:
            version = output.splitlines()[-1].strip()
    except Pymobiledevice3Error:
        pass
    return {"available": True, "command": cmd, "version": version}


def guard_preflight() -> dict:
    """Strict readiness check for the temperature-guard sidecar.

    Ordinary CLI capture keeps :func:`preflight`'s tolerant historical
    behavior when the CLI has no usable ``version`` subcommand. The guard uses
    version-sensitive async APIs, so it must prove the installed version is in
    the supported ``>=9.13,<10`` range before reporting ready.
    """
    result = preflight()
    if not result.get("available"):
        return result
    raw_version = result.get("version")
    parsed_version = _pmd3_version_tuple(
        raw_version if isinstance(raw_version, str) else None
    )
    if parsed_version is None or not ((9, 13) <= parsed_version < (10, 0)):
        found = raw_version if isinstance(raw_version, str) and raw_version else "unknown"
        return {
            **result,
            "available": False,
            "error": _unsupported_version_message(found),
            "installable": _system_python() is not None,
            "guard_supported": False,
        }
    return {**result, "guard_supported": True}


def install_cli(timeout: float = 900.0) -> dict:
    """Install/upgrade pymobiledevice3 via pip so the user never opens a shell.

    Uses the running interpreter in source checkouts and a system Python in
    frozen builds. Verifies the CLI resolves afterwards and returns the new
    ``preflight()`` result.
    """
    interpreter = _system_python()
    if not interpreter:
        raise Pymobiledevice3Error(
            "no Python interpreter found to install pymobiledevice3 into. "
            "Install Python 3.11+ from python.org first."
        )
    # zeroconf powers tunneld's Wi-Fi / mobdev2 monitors; without it the
    # background tunnel can only run over USB and wireless capture never forms a
    # Wi-Fi tunnel. Install it alongside pymobiledevice3 so wireless works after
    # the one-click setup.
    cmd = [
        interpreter,
        "-m",
        "pip",
        "install",
        "--upgrade",
        _PMD3_REQUIREMENT,
        "zeroconf",
    ]
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise Pymobiledevice3Error(
            f"pip install pymobiledevice3 timed out after {timeout:.0f}s"
        ) from exc
    except OSError as exc:
        raise Pymobiledevice3Error(f"failed to run pip: {exc}") from exc
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or proc.stdout or "").strip().splitlines()[-8:])
        raise Pymobiledevice3Error(f"pip install pymobiledevice3 failed: {tail}")

    _reset_command_cache()
    result = preflight()
    if not result.get("available"):
        raise Pymobiledevice3Error(
            "pip install succeeded but the pymobiledevice3 CLI still cannot "
            "be resolved. Restart the app, or set MPI_PYMOBILEDEVICE3 "
            "to the executable path."
        )
    return result


def _tunnel_entries_are_usb(entries: Any) -> bool:
    """True when every tunnel interface for a device is a USB interface.

    tunneld names tunnels by transport suffix: ``usbmux-<udid>-USB`` for a USB
    tunnel vs ``usbmux-<udid>-Network`` for a Wi-Fi/network tunnel (the mobdev2
    path), or a raw Wi-Fi adapter name. BOTH usbmux forms contain "usbmux", so
    classify by the trailing transport token, not by "usb" appearing anywhere —
    otherwise "usbmux-...-Network" is wrongly treated as USB.
    """
    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, list) or not entries:
        return True  # unknown shape -> don't mislabel as wireless
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        iface = str(_lookup_tunneld_value(entry, "interface", "name") or "").lower()
        # USB only when the interface ends in the USB transport token; "-Network"
        # and bare adapter names are wireless.
        if not iface.rstrip("-_ ").endswith("usb"):
            return False
    return True


def list_tunneld_devices() -> list[DeviceInfo]:
    """Devices reachable through the pymobiledevice3 tunnel (tunneld).

    On Windows the usbmux daemon only advertises USB devices, so a phone reached
    purely over a Wi-Fi RemoteXPC tunnel never shows up in ``usbmux list
    --network``. tunneld, however, tracks every active tunnel (USB or Wi-Fi) keyed
    by UDID, so read it directly and mark any device whose tunnel is not a USB
    interface as a wireless connection. DVT capture already runs over whatever
    tunnel tunneld holds (see :func:`_discover_rsd`), so a wireless tunnel device
    is fully capturable.
    """
    host, port = _tunneld_address()
    url = f"http://{host}:{port}/"
    try:
        with urllib.request.urlopen(url, timeout=_TUNNELD_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return []
    if not isinstance(payload, dict):
        return []
    devices: list[DeviceInfo] = []
    for udid, entries in payload.items():
        serial = str(udid).strip()
        if not serial:
            continue
        connection = "usb" if _tunnel_entries_are_usb(entries) else "wireless"
        devices.append(
            DeviceInfo(serial=serial, platform=Platform.IOS, connection_type=connection)
        )
    return devices


def _clear_device_metadata_cache() -> None:
    """Test/support hook; normal processes retain static lockdown info."""
    with _DEVICE_METADATA_LOCK:
        _LOCKDOWN_INFO_CACHE.clear()
        _IDENTITY_RETRY_AT.clear()


def list_devices():
    # Multiple renderer windows can poll together. Serialize whole refreshes so
    # they share the metadata cache instead of launching duplicate CLI bursts.
    with _DEVICE_LIST_LOCK:
        # USB discovery, network discovery, and tunneld's localhost view do not
        # depend on one another. A partial source failure must not hide devices
        # found by the other transports; only re-raise when both CLI sources fail
        # and tunneld has no usable fallback.
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="ios-discovery") as pool:
            source_futures = {
                "usb": pool.submit(_list_usbmux_simple, "--usb", "usb"),
                "network": pool.submit(_list_usbmux_simple, "--network", "wireless"),
                "tunneld": pool.submit(list_tunneld_devices),
            }
            source_results: dict[str, list[DeviceInfo]] = {}
            source_errors: dict[str, Pymobiledevice3Error] = {}
            for name in ("usb", "network", "tunneld"):
                try:
                    source_results[name] = source_futures[name].result()
                except Pymobiledevice3Error as exc:
                    source_results[name] = []
                    source_errors[name] = exc

        devices = [*source_results["usb"], *source_results["network"]]
        # usbmux on Windows can't see a phone reached only over a Wi-Fi tunnel,
        # so fold in tunneld's view without duplicating an already visible UDID.
        seen = {device.serial for device in devices}
        for device in source_results["tunneld"]:
            if device.serial not in seen:
                devices.append(device)
                seen.add(device.serial)
        if not devices and "usb" in source_errors and "network" in source_errors:
            raise source_errors["usb"]

        # Lockdown metadata (name/OS) is static for the process lifetime. Fetch
        # only cache misses, once per UDID, and overlap different devices. Any
        # individual failure preserves the basic transport entry.
        serials = list(dict.fromkeys(device.serial for device in devices))
        with _DEVICE_METADATA_LOCK:
            now = time.monotonic()
            misses = [serial for serial in serials if serial not in _LOCKDOWN_INFO_CACHE
                      or _IDENTITY_RETRY_AT.get(serial, float("inf")) <= now]
            # Retain previous metadata if a refresh fails after device lock.
            # A denied MG read can recover after unlocking without restarting.
            for serial in misses:
                _IDENTITY_RETRY_AT[serial] = now + 60.0

        def load_info(serial: str) -> tuple[str, Any | None]:
            try:
                info = run_json(["lockdown", "info"], udid=serial, timeout=30.0)
            except Pymobiledevice3Error:
                return serial, None
            # Read only these public hardware properties, never the full MG
            # dump (which can include device-specific/private identifiers).
            # A locked/unsupported device must still be discoverable. Cache
            # this bounded best-effort query along with lockdown metadata.
            try:
                identity = run_json(
                    ["diagnostics", "mg", "--keys", "MarketingProductName",
                     "--keys", "RegulatoryModelNumber"],
                    udid=serial, timeout=5.0,
                )
            except Pymobiledevice3Error:
                identity = {}
            return serial, {"lockdown": info, "identity": identity}

        if misses:
            with ThreadPoolExecutor(
                max_workers=min(4, len(misses)), thread_name_prefix="ios-lockdown"
            ) as pool:
                for serial, info in pool.map(load_info, misses):
                    if info is not None:
                        with _DEVICE_METADATA_LOCK:
                            _LOCKDOWN_INFO_CACHE[serial] = info
                            resolved = parsers.enrich_pymobiledevice3_device(
                                DeviceInfo(serial=serial, platform=Platform.IOS), info)
                            if resolved.model_number:
                                _IDENTITY_RETRY_AT.pop(serial, None)

        enriched: list[DeviceInfo] = []
        for device in devices:
            with _DEVICE_METADATA_LOCK:
                info = _LOCKDOWN_INFO_CACHE.get(device.serial)
            if info is None:
                enriched.append(device)
            else:
                enriched.append(parsers.enrich_pymobiledevice3_device(device, info))
        return enriched


def _list_usbmux_simple(filter_arg: str, connection_type: str) -> list[DeviceInfo]:
    payload = run_json(["usbmux", "list", filter_arg, "--simple"], timeout=30.0)
    devices: list[DeviceInfo] = []
    for item in payload if isinstance(payload, list) else []:
        serial = str(item or "").strip()
        if not serial:
            continue
        devices.append(
            DeviceInfo(
                serial=serial,
                platform=Platform.IOS,
                connection_type=connection_type,
            )
        )
    return devices


def list_packages(serial: str, user_only: bool = True):
    app_type = "User" if user_only else "Any"
    payload = run_json(["apps", "list", "--type", app_type], udid=serial, timeout=90.0)
    return parsers.parse_pymobiledevice3_apps(payload, user_only=user_only)


def _icons_script_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "pmd3_icons.py")


def fetch_app_icons(
    serial: str,
    bundle_ids: Iterable[str],
    *,
    timeout: float = 45.0,
) -> dict[str, Optional[bytes]]:
    """Fetch home-screen PNG icons for several iOS apps in a single batch.

    Spawns the standalone fetcher (:mod:`mpi.ios.pmd3_icons`) once. It
    connects to SpringBoardServices over usbmux — a plain lockdown service that
    needs no developer tunnel/RSD even on iOS 17+ — and streams one PNG per
    bundle id, amortizing the ~1 s interpreter-import + handshake across the
    whole batch (each subsequent icon is ~15 ms).

    Returns ``{bundle_id: png_bytes | None}`` with every requested id present;
    an id maps to ``None`` when its icon is unavailable so the caller can show a
    placeholder. Never raises — a missing interpreter, a connection failure, or
    a timeout degrades to all-``None`` so the dropdown stays responsive.
    """
    result: dict[str, Optional[bytes]] = {}
    for bid in bundle_ids:
        if bid:
            result.setdefault(str(bid), None)
    if not result:
        return result

    interpreter = _streamer_python()
    script = _icons_script_path()
    if not interpreter or not os.path.isfile(script):
        return result

    cmd = [interpreter, script, "--udid", serial]
    # Hand the icon fetcher the same tunnel/RSD coordinates the streamer gets, so
    # it can fall back from usbmux (USB-only on Windows) to the RemoteServiceDiscovery
    # tunnel for a wirelessly-connected device. usbmux stays the primary path.
    host, port = _tunneld_address()
    cmd += ["--tunneld", f"{host}:{port}"]
    rsd = _rsd_args(serial)
    if rsd:  # ["--rsd", host, port]
        cmd += ["--rsd", rsd[1], rsd[2]]
    payload = json.dumps(list(result.keys()))
    env = _subprocess_env(serial)
    try:
        with _external_program_context():
            proc = subprocess.run(
                cmd,
                input=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
                env=env,
            )
    except (OSError, subprocess.SubprocessError):
        return result

    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(record, dict):
            continue
        bid = record.get("bundle_id")
        b64 = record.get("png_b64")
        if bid in result and isinstance(b64, str) and b64:
            try:
                result[bid] = base64.b64decode(b64)
            except (ValueError, TypeError):
                result[bid] = None
    return result


def _looks_like_cli_usage_error(message: str) -> bool:
    """True when an error reads like argument parsing rejected the argv shape
    (a wrong flag/positional), as opposed to a real device/connection failure."""
    text = message.lower()
    return any(
        needle in text
        for needle in (
            "no such option",
            "unexpected extra argument",
            "unrecognized arguments",
        )
    )


def _set_wifi_connections(serial: str, state: str, *, timeout: float = 30.0) -> None:
    """Toggle iOS Wi-Fi connections, tolerating pymobiledevice3's CLI drift.

    The ``lockdown wifi-connections`` argument shape flipped between releases:
    newer builds (incl. 9.16) take the state as ``--state on/off`` while older
    ones took a bare positional (``wifi-connections on``). The two forms are
    mutually exclusive and the bundled CLI is upgraded in place, so try the
    current ``--state`` form first and fall back to the positional form only
    when the CLI rejects the option shape (not on a genuine device error). If
    every known form is rejected, surface a clear "update + rebuild" error
    instead of the last (misleading) argument-parse message."""
    forms = (
        ["lockdown", "wifi-connections", "--state", state],
        ["lockdown", "wifi-connections", state],
    )
    usage_errors: list[str] = []
    for args in forms:
        try:
            run(args, udid=serial, timeout=timeout)
            return
        except Pymobiledevice3Error as exc:
            if not _looks_like_cli_usage_error(str(exc)):
                # Genuine device/connection failure — surface it unchanged.
                raise
            usage_errors.append(str(exc))

    detail = " | ".join(usage_errors[-2:])
    raise Pymobiledevice3Error(
        "could not toggle iOS Wi-Fi connections: this pymobiledevice3 build "
        "rejected every known 'lockdown wifi-connections' argument form. "
        f"Update pymobiledevice3 and rebuild the app. Attempts: {detail}"
    )


def _safe_list_devices() -> list[DeviceInfo]:
    """list_devices() that swallows transient CLI failures (returns [])."""
    try:
        return list_devices()
    except Pymobiledevice3Error:
        return []


def _no_wireless_device_message(seen: list[DeviceInfo]) -> str:
    visible = ", ".join(f"{d.serial}:{d.connection_type}" for d in seen) or "none"
    if sys.platform == "win32":
        # The toggle succeeded (we reached here without _set_wifi_connections
        # raising). On Windows usbmux never advertises a wireless device, so the
        # phone only reconnects wirelessly through the pymobiledevice3 tunnel,
        # which forms over Wi-Fi once the USB cable is removed. Guide the user to
        # unplug it; list_devices() picks the device back up from tunneld then.
        return (
            "iOS Wi-Fi connections are enabled. To finish switching to wireless on "
            "Windows, UNPLUG the USB cable now while keeping the iPhone unlocked and "
            "on the same Wi-Fi network as this PC. The background iOS tunnel then "
            "re-forms over Wi-Fi and the device reconnects (it will not show up as a "
            "USB device). If it never reconnects, confirm the iOS tunnel is running "
            "(readiness badge) and that the PC and iPhone share one Wi-Fi network. "
            f"Visible devices: {visible}"
        )
    return (
        "enabled iOS Wi-Fi connections, but no wireless usbmux device appeared. "
        "Keep the iPhone unlocked, on the same Wi-Fi network, and paired over USB "
        f"first. Visible devices: {visible}"
    )


def prepare_wireless(serial: str, timeout: float = 60.0) -> dict:
    current = _safe_list_devices()
    existing = next(
        (device for device in current if device.serial == serial and device.connection_type == "wireless"),
        None,
    )
    if existing is not None:
        return {"serial": serial, "device": existing, "message": "already wireless"}

    # A genuine toggle failure raises here with an actionable message; reaching
    # past this line means EnableWifiConnections was set on the device. On Windows
    # the wireless device then reappears via tunneld (a Wi-Fi tunnel) once the USB
    # cable is unplugged — list_devices() includes tunneld devices, so the poll
    # below finds it without needing a wireless usbmux entry.
    _set_wifi_connections(serial, "on")

    deadline = time.monotonic() + timeout
    seen = current
    while time.monotonic() < deadline:
        # A transient lockdown/usbmux hiccup mid-poll must not abort the whole
        # operation with a raw error — keep the last good list and keep polling.
        seen = _safe_list_devices() or seen
        wireless = next(
            (
                device
                for device in seen
                if device.serial == serial and device.connection_type == "wireless"
            ),
            None,
        )
        if wireless is not None:
            return {
                "serial": wireless.serial,
                "device": wireless,
                "message": "wireless device is reachable",
            }
        time.sleep(0.75)

    raise Pymobiledevice3Error(_no_wireless_device_message(seen))


def _parse_pid_from_text(text: str) -> int:
    stripped = text.strip()
    if stripped.isdigit():
        return int(stripped)
    for pattern in _PID_PATTERNS:
        match = pattern.search(text)
        if match:
            return int(match.group(1))
    return 0


def resolve_or_launch_pid(serial: str, package: PackageInfo) -> int:
    errors: list[str] = []
    try:
        output = run(
            ["developer", "dvt", "process-id-for-bundle-id", package.bundle_id],
            udid=serial,
            timeout=45.0,
        )
        pid = _parse_pid_from_text(output)
        if pid:
            return pid
    except Pymobiledevice3Error as exc:
        errors.append(str(exc))
        if _looks_like_tunnel_error(str(exc)):
            raise

    try:
        output = run(
            ["developer", "dvt", "launch", package.bundle_id],
            udid=serial,
            timeout=90.0,
        )
    except Pymobiledevice3Error as exc:
        errors.append(str(exc))
        details = "; ".join(errors[-2:])
        raise Pymobiledevice3Error(
            f"could not launch {package.bundle_id} on {serial}: {details}"
        ) from exc
    pid = _parse_pid_from_text(output)
    if pid:
        return pid

    payload = _sample_sysmon(serial, timeout=90.0)
    process = parsers.find_pymobiledevice3_process(
        payload, pid=0, bundle_id=package.bundle_id, label=package.label or ""
    )
    pid = parsers.lookup_pymobiledevice3_pid(process or {})
    if pid:
        return pid
    details = "; ".join(errors[-2:])
    suffix = f": {details}" if details else ""
    raise Pymobiledevice3Error(
        f"could not resolve running pid for {package.bundle_id} on {serial}{suffix}"
    )


def terminate_pid(serial: str, pid: int) -> None:
    """Terminate an already-known DVT process without resolving or launching.

    Auto-stop must never use :func:`resolve_or_launch_pid`: if the app exited
    between its final sample and teardown, that helper could start it again.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError("pid must be a positive integer")
    run(["developer", "dvt", "kill", str(pid)], udid=serial, timeout=45.0)


def lookup_running_pid(serial: str, bundle_id: str) -> int:
    """Read the currently running PID for a bundle without launching it."""
    if not bundle_id:
        raise ValueError("bundle_id required")
    output = run(
        ["developer", "dvt", "process-id-for-bundle-id", bundle_id],
        udid=serial,
        timeout=45.0,
    )
    pid = _parse_pid_from_text(output)
    return pid if pid > 0 else 0


_SYSMON_SINGLE = ["developer", "dvt", "sysmon", "process", "single"]


def _looks_like_no_filter_match(message: str) -> bool:
    """True when ``sysmon process single --filter`` reported that no process in the
    snapshot matched (i.e. the target pid is not currently running), as opposed to
    the option being unsupported."""
    return "matching the given filters" in message.lower()


def _sample_sysmon(serial: str, *, timeout: float, pid: int = 0) -> Any:
    # `sysmon process single` snapshots every process. pymobiledevice3 9.x then
    # issues ONE `execnameForPid:` DVT round-trip *per process* in that snapshot
    # (cli/developer/dvt/sysmon/process.py). On a phone with ~500 processes that is
    # ~500 sequential round-trips: ~1 s over USB, but 35-90+ s over a high-latency
    # Wi-Fi tunnel — enough to blow the 90 s timeout and starve live capture. When
    # we already know the target pid, pass `--filter pid=<pid>` so the device only
    # resolves execName for that one process, collapsing the round-trips to ~1.
    if pid and pid > 0:
        try:
            return run_json(
                [*_SYSMON_SINGLE, "--filter", f"pid={pid}"], udid=serial, timeout=timeout
            )
        except Pymobiledevice3Error as exc:
            message = str(exc)
            if _looks_like_no_filter_match(message):
                # pid not in the snapshot (process gone) — report empty so the
                # caller raises its own clear "process not found" message.
                return []
            if not _looks_like_cli_usage_error(message):
                raise
            # Older pymobiledevice3 without `--filter`: fall back to the full
            # snapshot (slower, but the parser still selects the pid downstream).
    return run_json(_SYSMON_SINGLE, udid=serial, timeout=timeout)


def sample_process(
    serial: str,
    package: PackageInfo,
    *,
    pid: int,
    duration_sec: float,
):
    if duration_sec > 0:
        time.sleep(duration_sec)
    timeout = max(90.0, duration_sec + 30.0)
    payload = _sample_sysmon(serial, timeout=timeout, pid=pid)
    parsed = parsers.parse_pymobiledevice3_process_snapshot(
        payload,
        pid=pid,
        bundle_id=package.bundle_id,
        label=package.label or "",
    )
    if parsed.get("process") or not pid:
        return parsed

    raise Pymobiledevice3Error(
        f"could not find sysmon process sample for {package.bundle_id} "
        f"(pid {pid}) on {serial}"
    )


def build_snapshot(device, package: PackageInfo, duration_sec: float, parsed: dict) -> MetricsSnapshot:
    return MetricsSnapshot(
        device=device,
        package=package,
        captured_at=datetime.now(timezone.utc).isoformat(),
        duration_sec=duration_sec,
        frames=parsed.get("frames") or FrameStats(),
        memory=parsed["memory"],
        cpu=parsed["cpu"],
        raw={
            "pymobiledevice3_process": parsed.get("process") or {},
            "pymobiledevice3_note": (
                "Windows iOS capture uses pymobiledevice3 DVT sysmon. "
                "FPS/jank fields are populated only when the sysmon payload exposes them."
            ),
        },
    )


class SysmonStreamReader:
    """Own the persistent streamer subprocess and buffer its NDJSON output.

    The streamer (:mod:`mpi.ios.pmd3_stream`) holds one DVT connection
    open and prints one metrics frame per interval. A daemon thread reads those
    frames into a bounded buffer; the capture session drains the buffer once per
    sample window. This keeps the expensive tunnel/DVT handshake out of the
    per-sample path, so iOS captures sample once per second like Android instead
    of once per ~30 s.
    """

    def __init__(
        self,
        serial: str,
        pid: int,
        *,
        bundle_id: str = "",
        interval_ms: int = 1000,
        max_frames: int = 600,
    ) -> None:
        self._serial = serial
        self._pid = pid
        self._bundle_id = bundle_id
        self._interval_ms = interval_ms
        self._proc: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._frames: deque[dict] = deque(maxlen=max_frames)
        self._raw_buffer_dropped = 0
        self._events: deque[dict] = deque(maxlen=max_frames)
        self._temperatures: deque[dict] = deque(maxlen=max_frames)
        self._event_condition = threading.Condition(self._lock)
        self._stderr_tail: deque[str] = deque(maxlen=50)
        self._ready = threading.Event()
        self._prepared = threading.Event()
        self._triggered = threading.Event()
        self._trigger_pid: Optional[int] = None
        self._error: Optional[str] = None
        self._closed = False
        self.command: list[str] = []

    def start(self) -> None:
        """Spawn the streamer. Raises :class:`Pymobiledevice3Error` if it cannot
        be launched, so the caller can fall back to single-shot sampling."""
        self.command = build_streamer_command(
            self._serial,
            self._pid,
            bundle_id=self._bundle_id,
            interval_ms=self._interval_ms,
        )
        env = _subprocess_env(self._serial)
        try:
            with _external_program_context():
                self._proc = subprocess.Popen(  # noqa: S603 - argv is built internally
                    self.command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=env,
                )
        except (OSError, ValueError) as exc:
            raise Pymobiledevice3Error(
                f"failed to start iOS metrics streamer: {exc}"
            ) from exc

        self._reader_thread = threading.Thread(
            target=self._read_stdout, name="ios-sysmon-stream", daemon=True
        )
        self._reader_thread.start()
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, name="ios-sysmon-stream-err", daemon=True
        )
        self._stderr_thread.start()

    def _read_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(payload, dict):
                    continue
                event = payload.get("event")
                if event in ("fatal", "error"):
                    with self._event_condition:
                        self._error = str(
                            payload.get("error") or payload.get("code") or event
                        )
                        self._events.append(payload)
                        self._event_condition.notify_all()
                    self._ready.set()  # unblock waiters so they can fall back
                    self._prepared.set()
                    self._triggered.set()
                    continue
                if event == "ready":
                    continue
                if event:
                    with self._event_condition:
                        self._events.append(payload)
                        if event in ("temperature", "temperature_reset"):
                            self._temperatures.append(payload)
                        elif event == "triggered":
                            pid = payload.get("pid")
                            if isinstance(pid, int) and not isinstance(pid, bool):
                                self._trigger_pid = pid
                        self._event_condition.notify_all()
                    if event == "guard_ready":
                        self._prepared.set()
                    elif event == "triggered":
                        self._triggered.set()
                    continue
                with self._event_condition:
                    if len(self._frames) == self._frames.maxlen:
                        self._raw_buffer_dropped += len(self._frames[0].get("raw_frame_events", []))
                    self._frames.append(payload)
                    self._event_condition.notify_all()
                self._ready.set()
        except (OSError, ValueError):
            pass

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for line in proc.stderr:
                line = line.rstrip()
                if line:
                    with self._lock:
                        self._stderr_tail.append(line)
                    # Tee to the sidecar's own stderr so streamer diagnostics
                    # (kdebug tap status, battery temperature-source probe
                    # results) are visible in the engine log even when the
                    # capture SUCCEEDS — the tail buffer above only surfaces on
                    # failures. The streamer is quiet (~a dozen status lines per
                    # session; pymobiledevice3 loggers are set to ERROR).
                    logger.info("ios-streamer: %s", line)
        except (OSError, ValueError):
            pass

    def wait_ready(self, timeout: float) -> bool:
        """Block until the first frame (or a terminal error) arrives. The one-off
        tunnel/DVT handshake happens here, not on every sample."""
        return self._ready.wait(timeout)

    def wait_prepared(self, timeout: float) -> bool:
        """Wait until guard DVT + Diagnostics are prepared (or an error occurs)."""
        return self._prepared.wait(timeout)

    def next_temperature(self, timeout: float) -> Optional[dict]:
        """Return the next typed guard temperature event, blocking up to timeout."""
        deadline = time.monotonic() + max(float(timeout), 0.0)
        with self._event_condition:
            while not self._temperatures:
                if self._error is not None or self._closed:
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._event_condition.wait(remaining)
            return self._temperatures.popleft()

    def wait_triggered(self, timeout: float) -> Optional[int]:
        """Wait for the guard's target attach-or-launch and return its pid."""
        if not self._triggered.wait(timeout):
            return None
        with self._lock:
            return self._trigger_pid

    def drain_events(self) -> list[dict]:
        with self._lock:
            events = list(self._events)
            self._events.clear()
        return events

    def drain(self) -> list[dict]:
        with self._lock:
            frames = list(self._frames)
            self._frames.clear()
            if frames and self._raw_buffer_dropped:
                frames[0]["raw_buffer_events_dropped"] = self._raw_buffer_dropped
                self._raw_buffer_dropped = 0
        return frames

    def failed(self) -> bool:
        with self._lock:
            if self._error is not None:
                return True
        proc = self._proc
        if proc is None:
            return True
        # A streamer that has exited can never produce more frames, so treat any
        # exit as failure — whether it died before the first frame (handshake
        # failure) or mid-capture (tunnel reset / OS kill). The previous
        # `and not self._ready.is_set()` guard masked post-ready deaths, leaving
        # the session to emit empty 0-value snapshots forever instead of falling
        # back to single-shot sampling.
        return proc.poll() is not None

    @property
    def error(self) -> Optional[str]:
        with self._lock:
            if self._error:
                return self._error
            proc = self._proc
            if self._stderr_tail and (proc is None or proc.poll() is not None):
                # Join the last few stderr lines (not just the last one) so a
                # surfaced process failure carries surrounding context. While
                # the child is alive these are ordinary status breadcrumbs, not
                # an error signal (guard preparation logs before it becomes ready).
                return "\n".join(list(self._stderr_tail)[-3:])
        return None

    def is_running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Unblock anyone waiting on the one-off connection handshake so a stop
        # during connect returns promptly instead of hanging out the timeout.
        self._ready.set()
        self._prepared.set()
        self._triggered.set()
        with self._event_condition:
            self._event_condition.notify_all()
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
            except (OSError, ValueError):
                pass
            try:
                proc.wait(timeout=5.0)
            except (subprocess.TimeoutExpired, OSError, ValueError):
                try:
                    proc.kill()
                except (OSError, ValueError):
                    pass
        for thread in (self._reader_thread, self._stderr_thread):
            if thread is not None:
                thread.join(timeout=2.0)


class TemperatureGuardStreamReader(SysmonStreamReader):
    """Parent-side adapter for the guard/launch/promote sidecar mode.

    The child prepares DVT and Diagnostics, emits typed temperature events,
    attaches to an existing target or launches an absent one at
    ``required_hits``, then keeps the same process and DVT connection alive as
    an ordinary metrics reader.
    """

    def __init__(
        self,
        serial: str,
        bundle_id: str,
        threshold_c: float,
        *,
        required_hits: int = 2,
        interval_ms: int = 1000,
        first_reading_timeout_sec: float = 45.0,
        max_frames: int = 600,
        preview_only: bool = False,
    ) -> None:
        super().__init__(
            serial,
            0,
            bundle_id=bundle_id,
            interval_ms=interval_ms,
            max_frames=max_frames,
        )
        self.threshold_c = float(threshold_c)
        self.required_hits = int(required_hits)
        self.first_reading_timeout_sec = float(first_reading_timeout_sec)
        self.preview_only = bool(preview_only)

    def start(self) -> None:
        self.command = build_temperature_guard_command(
            self._serial,
            self._bundle_id,
            self.threshold_c,
            required_hits=self.required_hits,
            interval_ms=self._interval_ms,
            first_reading_timeout_sec=self.first_reading_timeout_sec,
            preview_only=self.preview_only,
        )
        env = _subprocess_env(self._serial)
        try:
            with _external_program_context():
                self._proc = subprocess.Popen(  # noqa: S603 - argv is internal
                    self.command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=env,
                )
        except (OSError, ValueError) as exc:
            raise Pymobiledevice3Error(
                f"failed to start iOS temperature guard: {exc}"
            ) from exc

        self._reader_thread = threading.Thread(
            target=self._read_stdout,
            name="ios-temperature-guard",
            daemon=True,
        )
        self._reader_thread.start()
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            name="ios-temperature-guard-err",
            daemon=True,
        )
        self._stderr_thread.start()
