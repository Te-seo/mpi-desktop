"""PC-side client for the MPI app installed through Google Play (com.mpi.companion).

The companion is an on-device app that hosts a Unix abstract-namespace socket
(``localabstract:mpi_companion``) speaking newline-delimited JSON. The PC reaches
it by creating an ``adb forward`` rule to a local TCP port and connecting there.

What it buys us over the pure-adb path:
  * installed-app inventory (labels, versions, rasterized icons) in one call
    instead of parsing each APK's binary manifest/resources over adb;
  * precise battery (current µA, charge counter), thermal status/headroom,
    system memory and per-uid network readings a third-party app can read but
    ``adb shell`` cannot easily.

It does NOT replace adb-based per-app sampling: a normal app cannot read another
app's ``/proc`` or run ``dumpsys gfxinfo`` (see ``capabilities`` -> ``cannot``).

SECURITY NOTE: the on-device socket is an *abstract* Unix socket. The APK checks
kernel peer credentials before dispatch and accepts only adb shell (UID 2000)
or root (UID 0). Other app UIDs cannot read telemetry or submit captures that
would upload under the signed-in Cloudflare account.

All functions raise :class:`mpi.android.adb.AdbError` or
:class:`CompanionError` on failure; callers should catch and degrade gracefully. This client never installs the app.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence

from . import adb

logger = logging.getLogger(__name__)
PACKAGE = "com.mpi.companion"
SOCKET_NAME = "mpi_companion"
MAIN_ACTIVITY = f"{PACKAGE}/.MainActivity"
CAPTURE_ACTIVITY = f"{PACKAGE}/.CaptureBootstrapActivity"
MIN_PROTOCOL_VERSION = 4


class CompanionError(RuntimeError):
    pass


class _DesktopLoginRequired(CompanionError):
    """The companion is reachable but its desktop lease is unavailable."""


def status(serial: str) -> Dict[str, Any]:
    """Read-only presence check. Denied/unavailable USB is not a missing APK."""
    result = {"serial": serial, "package": PACKAGE, "installed": False, "state": "device_unreachable"}
    try:
        state = adb.run(["get-state"], serial=serial, timeout=5.0).strip()
        if state != "device":
            if state == "unauthorized":
                result["state"] = "usb_unauthorized"
            return result
        # pm list returns success even when absent; pm path can exit 1.
        packages = adb.shell(f"pm list packages {PACKAGE}", serial=serial, timeout=5.0)
        result["installed"] = any(line.strip() == f"package:{PACKAGE}" for line in packages.splitlines())
        result["state"] = "installed" if result["installed"] else "companion_not_installed"
    except adb.AdbError as exc:
        if re.search(r"\bunauthori[sz]ed\b", str(exc), re.IGNORECASE):
            result["state"] = "usb_unauthorized"
    return result


# --- socket RPC -----------------------------------------------------------


class _Rpc:
    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._reader = sock.makefile("r", encoding="utf-8", newline="\n")

    def call(self, cmd: str, args: Optional[Dict[str, Any]] = None) -> Any:
        request = json.dumps({"cmd": cmd, "args": args or {}}, ensure_ascii=False) + "\n"
        # Honor the module contract (AdbError/CompanionError only): a socket timeout
        # or broken pipe is an OSError, so wrap it rather than leak the raw type.
        try:
            self._sock.sendall(request.encode("utf-8"))
            line = self._reader.readline()
        except OSError as exc:
            raise CompanionError(f"companion RPC '{cmd}' failed: {exc}") from exc
        if not line:
            raise CompanionError(f"companion closed the connection during '{cmd}'")
        try:
            response = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CompanionError(f"invalid response to '{cmd}': {line!r}") from exc
        if not response.get("ok"):
            if response.get("error") == "desktop-login-required":
                raise _DesktopLoginRequired(f"companion error on '{cmd}': desktop-login-required")
            raise CompanionError(f"companion error on '{cmd}': {response.get('error')}")
        return response.get("data")


def _start_activity(serial: str, component: str) -> bool:
    # am start reports an absent component on stdout or as an adb error,
    # depending on the Android version.
    missing = r"Error\s+type\s+3|Activity\s+class\s+.*does\s+not\s+exist|unable\s+to\s+resolve"
    try:
        output = adb.shell(
            f"am start --activity-no-animation -n {component}", serial=serial, timeout=20.0
        )
    except adb.AdbError as exc:
        if re.search(missing, str(exc), re.IGNORECASE):
            return False
        raise
    return not re.search(missing, output, re.IGNORECASE)


def ensure_running(serial: str) -> None:
    """Explicit Connect may show Android's missing-permission screens."""
    if not _start_activity(serial, MAIN_ACTIVITY):
        raise CompanionError(f"companion app is not installed on {serial}")


def _start_capture_bridge(serial: str) -> None:
    """Start the service before sampling, returning focus to the measured app."""
    if _start_activity(serial, CAPTURE_ACTIVITY):
        return

    # Older Play builds only have the interactive bootstrap. It is safe here
    # when all grants it could prompt for are already in place for this user.
    overlay = adb.shell(
        f"cmd appops get --user current {PACKAGE} SYSTEM_ALERT_WINDOW", serial=serial, timeout=5.0
    )
    if not re.search(r"\bSYSTEM_ALERT_WINDOW:\s*allow\b", overlay):
        raise CompanionError("use Connect to prepare the installed companion")
    sdk = adb.shell("getprop ro.build.version.sdk", serial=serial, timeout=5.0).strip()
    if not sdk.isdecimal():
        raise CompanionError("could not determine Android version")
    if int(sdk) >= 33:
        notification = adb.shell(
            f"dumpsys package check-permission android.permission.POST_NOTIFICATIONS {PACKAGE} $(am get-current-user)",
            serial=serial, timeout=5.0,
        )
        if notification.strip() != "0":
            raise CompanionError("use Connect to prepare the installed companion")
    ensure_running(serial)


def _remove_socket_forward(serial: str) -> None:
    """Remove any host forward rule pointing at the companion socket for this
    device. Used to clean up when we couldn't parse the resolved port — adb keys
    the rule by the resolved ``tcp:<port>``, not the ``tcp:0`` we requested."""
    try:
        listing = adb.forward_list(serial=serial)
    except adb.AdbError:
        return
    target = f"localabstract:{SOCKET_NAME}"
    for line in listing.splitlines():
        parts = line.split()
        # "<serial> tcp:<port> localabstract:<name>"
        if len(parts) >= 3 and parts[0] == serial and parts[-1] == target:
            adb.forward_remove(parts[-2], serial=serial)


def _try_connect(port: int, timeout: float) -> Optional[socket.socket]:
    """Connect to the forwarded port AND confirm the device side is serving.

    adb accepts the local TCP even when nothing is listening on the device's
    abstract socket yet (the service hasn't bound), then immediately EOFs — so a
    bare connect is a false positive that yields a dead socket. Peek for that EOF
    and reject it; a live, idle server sends nothing, so the peek just times out."""
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    except OSError:
        return None
    try:
        sock.settimeout(0.5)
        if sock.recv(1, socket.MSG_PEEK) == b"":
            sock.close()
            return None
    except socket.timeout:
        pass  # no data within the window = healthy idle connection
    except OSError:
        sock.close()
        return None
    return sock


def _open_forwarded_socket(
    serial: str, connect_timeout: float, start_if_cold: Optional[Callable[[str], None]]
) -> tuple:
    """Create the ``adb forward`` rule and return ``(socket, forward_rule)``.

    Tries to connect first — if the foreground service is already running (the
    common case after the first call) we skip ``am start`` entirely, so repeated
    calls (e.g. icon batches) don't keep bringing the companion activity to the
    foreground on the device. Only a cold connect pokes the activity and retries
    while the socket binds.

    With no startup callback a warm connect is the only option: callers
    running against a live capture cannot afford ``am start``, which foregrounds
    the companion and makes the measured app look backgrounded.

    On any failure the rule is removed here; on success the caller owns it."""
    local = "tcp:0"
    port_out = adb.forward(local, f"localabstract:{SOCKET_NAME}", serial=serial)
    try:
        port = int(port_out.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        # adb keys the rule by the resolved port, not the "tcp:0" we requested, so
        # look it up in `adb forward --list` rather than removing the wrong key.
        _remove_socket_forward(serial)
        raise CompanionError(f"adb forward did not return a port: {port_out!r}") from exc

    forward_rule = f"tcp:{port}"
    try:
        # Fast path: the service may already be running from a previous call.
        sock = _try_connect(port, timeout=1.5)
        if sock is None:
            if start_if_cold is None:
                raise CompanionError(
                    f"companion bridge is not listening on {serial} and a cold "
                    "start is not allowed here"
                )
            # Cold: bring the activity up (it starts the FGS that binds the
            # socket), then retry while it initializes.
            start_if_cold(serial)
            deadline = time.monotonic() + connect_timeout
            while sock is None and time.monotonic() < deadline:
                sock = _try_connect(port, timeout=4.0)
                if sock is None:
                    time.sleep(0.25)
            if sock is None:
                raise CompanionError(
                    f"companion did not come up on 127.0.0.1:{port} within {connect_timeout:.0f}s"
                )
        sock.settimeout(15.0)
        return sock, forward_rule
    except BaseException:
        adb.forward_remove(forward_rule, serial=serial)
        raise


@contextmanager
def connect(
    serial: str, connect_timeout: float = 6.0, allow_cold_start: bool = True
) -> Iterator[_Rpc]:
    """``adb forward`` to the bridge and yield an RPC client, tearing down the
    forward rule on exit. One-shot: use :class:`OverlaySession` for anything that
    talks to the device repeatedly."""
    sock, forward_rule = _open_forwarded_socket(
        serial, connect_timeout, ensure_running if allow_cold_start else None
    )
    try:
        yield _Rpc(sock)
    finally:
        try:
            sock.close()
        except OSError:
            pass
        adb.forward_remove(forward_rule, serial=serial)


# --- high-level convenience -----------------------------------------------


def prepare_capture_bridge(serial: str) -> None:
    """Warm the service before a temperature guard launches the target app."""
    sock, rule = _open_forwarded_socket(serial, 6.0, _start_capture_bridge)
    try:
        sock.close()
    finally:
        adb.forward_remove(rule, serial=serial)


def ping(serial: str) -> Dict[str, Any]:
    with connect(serial) as rpc:
        return rpc.call("ping")


def fetch_app_list(serial: str, user_only: bool = True) -> List[Dict[str, Any]]:
    with connect(serial, connect_timeout=1.0, allow_cold_start=False) as rpc:
        return rpc.call("list_apps", {"user_only": user_only})


def fetch_icons(serial: str, packages: List[str], size: int = 96) -> Dict[str, Any]:
    with connect(serial, connect_timeout=1.0, allow_cold_start=False) as rpc:
        return rpc.call("get_icons", {"packages": packages, "size": size})


def fetch_metrics(serial: str, package: Optional[str] = None) -> Dict[str, Any]:
    with connect(serial, connect_timeout=1.0, allow_cold_start=False) as rpc:
        return rpc.call("metrics", {"package": package or ""})


def fetch_capabilities(serial: str) -> Dict[str, Any]:
    with connect(serial, connect_timeout=1.0, allow_cold_start=False) as rpc:
        return rpc.call("capabilities")


# --- on-device HUD overlay ------------------------------------------------

# Metric ids the HUD understands, in display order. "net" is computed on-device
# by the companion (adb cannot attribute per-app traffic); the other five are
# pushed from the PC once per sample.
OVERLAY_METRICS = ("elapsed", "fps", "cpu", "gpu", "temp", "net")

# Pushes carry one tiny JSON line. The 15 s default of a one-shot connection
# would let a single wedged write outlive the capture that owns it.
OVERLAY_SOCKET_TIMEOUT_SEC = 5.0

# Consecutive failed pushes before a session gives up for good. A phone that was
# unplugged mid-capture must not make every remaining sample pay a socket
# timeout, and recovering would need the ``am start`` we are forbidden to run.
_OVERLAY_PUSH_FAILURE_LIMIT = 3
_OVERLAY_LOGIN_RETRY_SEC = 2.0


def _normalize_overlay_metrics(metrics: Optional[Sequence[str]]) -> List[str]:
    """Canonical order, unknown ids dropped. ``None`` means "everything"."""
    if metrics is None:
        return list(OVERLAY_METRICS)
    wanted = {str(m).strip().lower() for m in metrics}
    return [m for m in OVERLAY_METRICS if m in wanted]


class OverlaySession:
    """A companion connection held open for one capture's dashboard and HUD.

    :func:`connect` builds and tears down an ``adb forward`` per call — two adb
    process spawns, an order of magnitude more than the 0.5 s sample cadence can
    absorb. This keeps a single forward rule plus socket for the whole capture,
    and pushes samples from its own daemon thread through a one-slot drop-latest
    mailbox, so a slow or vanished device can never stretch that cadence.
    """

    def __init__(
        self,
        serial: str,
        sock: socket.socket,
        forward_rule: str,
        package: str = "",
        metrics: Optional[Sequence[str]] = None,
        enabled: bool = True,
        session_id: Optional[str] = None,
        label: Optional[str] = None,
    ) -> None:
        self.serial = serial
        self._sock = sock
        self._forward_rule = forward_rule
        self._rpc = _Rpc(sock)
        # Guards the wire: the pusher thread and desktop-driven config calls
        # share one socket, and interleaved lines would desync the framing.
        self._rpc_lock = threading.Lock()
        self._package = package
        self._session_id = session_id or str(uuid.uuid4())
        self._label = label
        self._recording = True
        self._metrics = _normalize_overlay_metrics(metrics)
        self._enabled = bool(enabled)
        self._hud_enabled = False
        self._failures = 0
        self._dead = False
        self._closed = False
        self._close_lock = threading.Lock()
        self._mailbox: Optional[Dict[str, Any]] = None
        self._cond = threading.Condition()
        self._config_retry_at: Optional[float] = None
        try:
            sock.settimeout(OVERLAY_SOCKET_TIMEOUT_SEC)
        except OSError:
            pass
        self._pusher = threading.Thread(
            target=self._push_loop, name=f"overlay-push-{serial}", daemon=True
        )
        self._pusher.start()

    # -- state --
    @property
    def metrics(self) -> List[str]:
        return list(self._metrics)

    @property
    def enabled(self) -> bool:
        """What the desktop last asked for."""
        return self._enabled

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def hud_enabled(self) -> bool:
        """Whether the companion reported the HUD as actually showing. False when
        the overlay permission is missing even though we asked for it."""
        return self._hud_enabled

    @property
    def login_pending(self) -> bool:
        return self._config_retry_at is not None and self.alive

    @property
    def alive(self) -> bool:
        return not self._closed and not self._dead

    # -- wire --
    def configure(
        self,
        enabled: Optional[bool] = None,
        metrics: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        """Send a new HUD configuration and return the companion's answer.

        Refused once the session is closed or given up on: a toggle can land
        while :meth:`close` is already disabling the HUD, and a re-enable that
        slips in behind it would leave a window on the phone that nothing can
        take down through this session: its socket and forward rule are gone
        by then, and the review dashboard does not control the HUD."""
        if self._closed or self._dead:
            raise CompanionError("overlay session is closed")
        with self._rpc_lock:
            # Re-checked on the wire lock, and the requested state is only
            # applied here: close() sets _closed before it reaches for this lock
            # and flips _enabled to False while holding it, so whichever of the
            # two acquires it second sees the other's decision and the disable
            # always gets the last word.
            if self._closed or self._dead:
                raise CompanionError("overlay session is closed")
            if metrics is not None:
                self._metrics = _normalize_overlay_metrics(metrics)
            if enabled is not None:
                self._enabled = bool(enabled)
            if self._config_retry_at is None:
                try:
                    return self._send_config()
                except _DesktopLoginRequired:
                    self._defer_config()
            return {
                "enabled": False,
                "requested": self._enabled,
                "metrics": list(self._metrics),
                "reason": "companion_sign_in_required",
            }

    def _send_config(self) -> Dict[str, Any]:
        data = self._rpc.call("overlay_config", self._config_args())
        if isinstance(data, dict):
            self._hud_enabled = bool(data.get("enabled"))
            return data
        self._hud_enabled = self._enabled
        return {
            "enabled": self._hud_enabled,
            "requested": self._enabled,
            "metrics": list(self._metrics),
        }

    def set_enabled(self, enabled: bool) -> Dict[str, Any]:
        return self.configure(enabled=enabled)

    def set_metrics(self, metrics: Sequence[str]) -> Dict[str, Any]:
        return self.configure(metrics=metrics)

    def update(self, values: Dict[str, Any]) -> None:
        """Hand one sample to the pusher thread.

        Never blocks on the device and never raises: the capture pump owns the
        measurement cadence, and a HUD that skips an update is worth nothing next
        to a stalled or aborted measurement."""
        if self._dead or self._closed:
            return
        try:
            with self._cond:
                if self._closed or self._dead:
                    return
                # One slot, drop-latest: an unsent sample is already stale, and
                # queueing would only make the HUD drift further behind.
                self._mailbox = values
                self._cond.notify()
        except Exception:  # pragma: no cover - a lock op should never fail
            logger.debug("overlay mailbox update failed", exc_info=True)

    def close(self) -> None:
        """Turn the HUD off, drop the socket and remove the forward rule — exactly
        once, from whichever thread ends the capture first."""
        with self._close_lock:
            if self._closed:
                return
            with self._cond:
                self._closed = True
                self._cond.notify_all()
            if self._pusher is not threading.current_thread():
                self._pusher.join(timeout=OVERLAY_SOCKET_TIMEOUT_SEC)
            # Best-effort: the review dashboard cannot dismiss a floating HUD.
            # Bounded acquire because a wedged push still holds the wire; closing
            # the socket below unblocks it either way. _closed is already set, so
            # any toggle still queued for the wire drops out instead of joining
            # the contention and pushing this acquire past its deadline.
            if not self._dead:
                if self._rpc_lock.acquire(timeout=1.0):
                    try:
                        self._enabled = False
                        self._recording = False
                        self._rpc.call("overlay_config", self._config_args())
                    except (CompanionError, OSError) as exc:
                        logger.debug("overlay disable on close failed: %s", exc)
                    finally:
                        self._rpc_lock.release()
                else:
                    # A call was mid-flight and outlasted the wait: the HUD may
                    # stay on screen until the companion notices the dropped
                    # socket, which is worth a line when a user reports one.
                    logger.info(
                        "overlay disable on close skipped for %s: wire still busy",
                        self.serial,
                    )
            self._enabled = False
            self._recording = False
            self._hud_enabled = False
            with self._cond:
                self._config_retry_at = None
                self._mailbox = None
            # makefile() retains a socket reference. Shutdown first to unblock
            # a wedged RPC and let Android observe the disconnect immediately.
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            if not self._pusher.is_alive() and isinstance(self._rpc, _Rpc):
                self._rpc._reader.close()
            adb.forward_remove(self._forward_rule, serial=self.serial)

    # -- internals --
    def _config_args(self) -> Dict[str, Any]:
        args = {
            "enabled": self._enabled,
            "metrics": list(self._metrics),
            "package": self._package,
            "session_id": self._session_id,
            "recording": self._recording,
        }
        if self._label:
            args["label"] = self._label
        return args

    def _defer_config(self, values: Optional[Dict[str, Any]] = None) -> None:
        # Keep this command socket: Android pins it to the first verified lease.
        # Reopening could let a capture move to another account after a sign-out.
        self._hud_enabled = False
        with self._cond:
            self._config_retry_at = time.monotonic() + _OVERLAY_LOGIN_RETRY_SEC
            if self._mailbox is None:
                self._mailbox = values
            self._cond.notify()

    def _wait_for_push(self) -> bool:
        with self._cond:
            while True:
                if self._config_retry_at is None:
                    # Closing drains the newest authorized sample before stop.
                    if self._mailbox is not None:
                        return True
                    delay = None
                else:
                    delay = self._config_retry_at - time.monotonic()
                    if delay <= 0:
                        return not self._closed
                if self._closed:
                    return False
                self._cond.wait(delay)

    def _push_once(self) -> None:
        with self._rpc_lock:
            if not self._recording:
                return
            values = None
            try:
                if self._config_retry_at is not None:
                    if self._closed or self._config_retry_at > time.monotonic():
                        return
                    self._send_config()
                    with self._cond:
                        self._config_retry_at = None
                with self._cond:
                    values, self._mailbox = self._mailbox, None
                if values is not None:
                    self._rpc.call(
                        "overlay_update",
                        {"session_id": self._session_id, "values": values},
                    )
            except _DesktopLoginRequired:
                self._defer_config(values)
            except (CompanionError, OSError):
                if self._config_retry_at is not None:
                    self._defer_config()
                raise

    def _push_loop(self) -> None:
        while self._wait_for_push():
            try:
                self._push_once()
            except (CompanionError, OSError):
                self._failures += 1
                if self._failures >= _OVERLAY_PUSH_FAILURE_LIMIT:
                    self._dead = True
                    logger.info(
                        "device overlay on %s stopped answering; HUD updates disabled",
                        self.serial,
                    )
                    return
            else:
                self._failures = 0


def open_overlay_session(
    serial: str,
    package: str = "",
    metrics: Optional[Sequence[str]] = None,
    enabled: bool = True,
    allow_cold_start: bool = True,
    connect_timeout: float = 6.0,
    session_id: Optional[str] = None,
    label: Optional[str] = None,
) -> OverlaySession:
    """Open the HUD session for a capture and send its initial configuration.

    ``allow_cold_start=False`` forbids ``am start`` entirely: bringing the
    companion activity up while a capture runs foregrounds it over the measured
    app, which the Android backend reads as the target being backgrounded and
    ends the measurement. Raises :class:`CompanionError` / ``AdbError`` — callers
    treat the HUD as optional and swallow both."""
    sock, forward_rule = _open_forwarded_socket(
        serial, connect_timeout, _start_capture_bridge if allow_cold_start else None
    )
    try:
        sock.settimeout(OVERLAY_SOCKET_TIMEOUT_SEC)
        identity = _Rpc(sock).call("ping")
        protocol = identity.get("protocol") if isinstance(identity, dict) else None
        if (
            not isinstance(protocol, (int, float))
            or isinstance(protocol, bool)
            or protocol < MIN_PROTOCOL_VERSION
            or identity.get("app") != "mpi-companion"
        ):
            raise CompanionError("companion is too old for the mobile dashboard")
        session = OverlaySession(
            serial,
            sock,
            forward_rule,
            package=package,
            metrics=metrics,
            enabled=enabled,
            session_id=session_id,
            label=label,
        )
    except BaseException:
        try:
            sock.close()
        except OSError:
            pass
        adb.forward_remove(forward_rule, serial=serial)
        raise
    try:
        session.configure()
    except BaseException:
        session.close()
        raise
    return session


def overlay_support(serial: str, allow_cold_start: bool = False) -> Dict[str, Any]:
    """Report whether ``serial`` can show the on-device HUD.

    Degrades to a structured answer instead of raising: no companion, a companion
    too old to know ``overlay_status``, and a companion that simply is not
    listening are all states the desktop renders as "unavailable" — none of them
    is an error the user can act on through a stack trace."""
    result: Dict[str, Any] = {
        "serial": serial,
        "installed": False,
        "supported": False,
        "can_draw_overlays": False,
        "protocol": None,
        "reason": None,
    }
    presence = status(serial)
    if not presence["installed"]:
        result["reason"] = presence["state"]
        return result
    result["installed"] = True

    try:
        # Bootstrap only on the user's explicit desktop Connect action.
        with connect(serial, connect_timeout=6.0 if allow_cold_start else 1.0,
                     allow_cold_start=allow_cold_start) as rpc:
            data = rpc.call("overlay_status")
    except (CompanionError, adb.AdbError) as exc:
        # A pre-overlay companion answers "unknown cmd: overlay_status"; anything
        # else means the bridge never came up.
        message = str(exc)
        result["reason"] = (
            "companion_sign_in_required" if "desktop-login-required" in message else
            "companion_too_old" if "unknown cmd" in message else "companion_unreachable"
        )
        logger.info("overlay status unavailable on %s: %s", serial, exc)
        return result

    if not isinstance(data, dict):
        result["reason"] = "companion_too_old"
        return result
    protocol = data.get("protocol")
    if isinstance(protocol, (int, float)) and not isinstance(protocol, bool):
        result["protocol"] = int(protocol)
    if result["protocol"] is None or result["protocol"] < MIN_PROTOCOL_VERSION:
        result["reason"] = "companion_too_old"
        return result
    result["supported"] = bool(data.get("supported"))
    result["can_draw_overlays"] = bool(data.get("can_draw_overlays"))
    if not result["supported"]:
        result["reason"] = "overlay_unsupported_os"
    elif not result["can_draw_overlays"]:
        result["reason"] = "overlay_permission_denied"
    return result
