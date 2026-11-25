"""iOS backend using Xcode tools on macOS and pymobiledevice3 elsewhere."""

from __future__ import annotations

import logging
import hashlib
import math
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ..backend import AppBackgroundedError, AppExitedError, DeviceBackend
from ..migration import cache_root
from ..schema import (
    BatteryTemperatureReading,
    BatteryStats,
    CpuStats,
    DeviceInfo,
    FrameStats,
    GpuStats,
    MemStats,
    MetricsSnapshot,
    PackageInfo,
    Platform,
    ThermalStats,
)
from . import parsers, pmd3 as pymobiledevice, xcrun
from .capture import _find_pid_for_bundle, record_trace
from .pmd3 import Pymobiledevice3Error
from .xcrun import XcrunError

logger = logging.getLogger(__name__)

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _icon_cache_path(bundle_id: str) -> Optional[Path]:
    """Disk-cache path for an iOS app icon. Kept in a dedicated ``ios-icons``
    directory (separate from the Android icon cache) so a bundle id shared by an
    app shipped on both platforms — e.g. ``com.spotify.client`` — never serves
    the wrong platform's icon."""
    if not bundle_id:
        return None
    base = cache_root() / "ios-icons"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return base / (re.sub(r"[^A-Za-z0-9._-]", "_", bundle_id) + ".png")


def _icon_cache_read(bundle_id: str) -> Optional[bytes]:
    path = _icon_cache_path(bundle_id)
    if path is None:
        return None
    try:
        if not path.is_file():
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if not data.startswith(_PNG_MAGIC):
        try:
            path.unlink()
        except OSError:
            pass
        return None
    return data


def _icon_cache_write(bundle_id: str, data: bytes) -> None:
    path = _icon_cache_path(bundle_id)
    if path is None or not data:
        return
    try:
        path.write_bytes(data)
    except OSError as exc:
        logger.debug("ios icon cache write failed for %s: %s", bundle_id, exc)


def _label_cache_write(bundle_id: str, label: str) -> None:
    """Persist iOS display names for offline legacy-session recovery."""
    text = re.sub(r"\s+", " ", label).strip() if label else ""
    if not bundle_id or not text:
        return
    base = cache_root() / "ios-labels"
    try:
        base.mkdir(parents=True, exist_ok=True)
        path = base / (re.sub(r"[^A-Za-z0-9._-]", "_", bundle_id) + ".txt")
        path.write_text(text, encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        logger.debug("ios label cache write failed for %s: %s", bundle_id, exc)


class IOSBackend(DeviceBackend):
    def __init__(self) -> None:
        # The renderer always lists a device and its apps before capture. Reuse
        # that authoritative metadata so start_capture does not immediately pay
        # another usbmux/lockdown scan and another full app inventory command.
        self._cache_lock = threading.RLock()
        self._device_cache: Dict[str, DeviceInfo] = {}
        self._package_cache: Dict[tuple[str, str], PackageInfo] = {}
        self._temperature_monitors: Dict[str, IOSBatteryTemperatureMonitor] = {}

    def _use_pymobiledevice(self) -> bool:
        if sys.platform != "darwin":
            return True
        return False

    def _require_macos(self) -> None:
        if sys.platform != "darwin":
            raise XcrunError("iOS capture requires macOS")

    def list_devices(self) -> List[DeviceInfo]:
        if self._use_pymobiledevice():
            devices = pymobiledevice.list_devices()
        else:
            self._require_macos()
            data = xcrun.run_json(["devicectl", "list", "devices"], timeout=60.0)
            devices = parsers.parse_devicectl_devices(data)
        with self._cache_lock:
            # Preserve the first transport for duplicate USB/network entries,
            # matching list_devices()'s stable preference order.
            for device in devices:
                cached = self._device_cache.get(device.serial)
                if cached is None or (
                    (cached.model == "unknown" and device.model != "unknown")
                    or (cached.os_version == "unknown" and device.os_version != "unknown")
                    or (not cached.display_name and bool(device.display_name))
                    or (not cached.model_number and bool(device.model_number))
                    or (not cached.model_identifier and bool(device.model_identifier))
                ):
                    self._device_cache[device.serial] = device
        return devices

    def list_packages(
        self, serial: str, user_only: bool = True
    ) -> List[PackageInfo]:
        if self._use_pymobiledevice():
            packages = pymobiledevice.list_packages(serial, user_only=user_only)
        else:
            self._require_macos()
            data = xcrun.run_json(
                ["devicectl", "device", "info", "apps", "--device", serial],
                timeout=90.0,
            )
            packages = parsers.parse_devicectl_apps(data, user_only=user_only)
        with self._cache_lock:
            for package in packages:
                self._package_cache[(serial, package.bundle_id)] = package
        for package in packages:
            if package.label:
                _label_cache_write(package.bundle_id, package.label)
        return packages

    def fetch_app_icons(
        self, serial: str, bundle_ids: List[str]
    ) -> Dict[str, Optional[bytes]]:
        """Return home-screen PNG icons (bytes, or ``None``) for the given iOS
        bundle ids, keyed by bundle id.

        Disk-cache first, then one batched SpringBoardServices fetch over
        usbmux for the misses (see :func:`mpi.ios.pmd3.fetch_app_icons`).
        Always graceful: any failure leaves the relevant ids mapped to ``None``
        so the dropdown falls back to a placeholder rather than blocking.
        """
        result: Dict[str, Optional[bytes]] = {}
        misses: List[str] = []
        for bundle_id in bundle_ids:
            if not bundle_id:
                continue
            cached = _icon_cache_read(bundle_id)
            if cached is not None:
                result[bundle_id] = cached
            else:
                result[bundle_id] = None
                misses.append(bundle_id)

        if not misses:
            return result

        try:
            fetched = pymobiledevice.fetch_app_icons(serial, misses)
        except Pymobiledevice3Error as exc:
            logger.debug("ios icon batch failed for %s: %s", serial, exc)
            return result

        for bundle_id, data in fetched.items():
            if data:
                result[bundle_id] = data
                _icon_cache_write(bundle_id, data)
        return result

    def prepare_wireless(self, serial: str) -> dict:
        if not serial:
            raise ValueError("serial required")
        if self._use_pymobiledevice():
            return pymobiledevice.prepare_wireless(serial)
        raise XcrunError(
            "iOS wireless setup from mpi is only implemented through "
            "pymobiledevice3 on Windows/Linux. On macOS, enable network pairing "
            "in Finder/Xcode and refresh iOS devices."
        )

    def capture(
        self,
        serial: str,
        package: PackageInfo,
        duration_sec: float = 10.0,
    ) -> MetricsSnapshot:
        device, enriched = self.prepare_capture(serial, package)
        return self.capture_sample(serial, device, enriched, duration_sec)

    def prepare_capture(
        self, serial: str, package: PackageInfo
    ) -> tuple[DeviceInfo, PackageInfo]:
        return self._resolve_device(serial), self._enrich_package(serial, package)

    def capture_sample(
        self,
        serial: str,
        device: DeviceInfo,
        package: PackageInfo,
        duration_sec: float,
    ) -> MetricsSnapshot:
        if self._use_pymobiledevice():
            pid = pymobiledevice.resolve_or_launch_pid(serial, package)
            return _capture_pymobiledevice_sample(
                serial,
                device,
                package,
                pid,
                duration_sec,
            )

        self._require_macos()
        result = record_trace(serial, package.bundle_id, duration_sec)
        cpu, memory = parsers.parse_activity_monitor(
            result["activity_monitor_xml"],
            pid=result["pid"],
        )
        frames = parsers.parse_core_animation(result["core_animation_xml"] or "")
        trace_id = hashlib.sha256(str(result["trace_path"]).encode("utf-8")).hexdigest()[:16]
        for event in frames.raw_events:
            event["stream_id"] = trace_id

        return MetricsSnapshot(
            device=device,
            package=package,
            captured_at=datetime.now(timezone.utc).isoformat(),
            duration_sec=duration_sec,
            frames=frames,
            memory=memory,
            cpu=cpu,
            raw={
                "trace_path": result["trace_path"],
                "activity_monitor_xml": result["activity_monitor_xml"],
                "core_animation_xml": result["core_animation_xml"] or "",
            },
        )

    def open_streaming(
        self, serial: str, package: PackageInfo
    ) -> "IOSStreamingCaptureSession":
        device, enriched = self.prepare_capture(serial, package)
        if self._use_pymobiledevice():
            pid = pymobiledevice.resolve_or_launch_pid(serial, enriched)
            return IOSPymobiledeviceStreamingCaptureSession(
                serial,
                device,
                enriched,
                pid,
            )
        self._require_macos()
        return IOSStreamingCaptureSession(self, serial, device, enriched)

    def terminate_app(
        self,
        serial: str,
        package: PackageInfo,
        *,
        session: object | None = None,
    ) -> None:
        """Terminate the measured app for API-owned automatic capture stop.

        A Windows/Linux streaming session already owns the DVT pid.  On macOS
        the trace session has no durable pid, so inspect the device process list
        and terminate only an existing matching process -- never launch one.
        """
        captured_pid = getattr(session, "pid", None)
        if self._use_pymobiledevice():
            if (
                not isinstance(captured_pid, int)
                or isinstance(captured_pid, bool)
                or captured_pid <= 0
            ):
                return
            current_pid = pymobiledevice.lookup_running_pid(serial, package.bundle_id)
            if current_pid <= 0:
                return
            if current_pid != captured_pid:
                logger.warning(
                    "refusing to terminate reused iOS pid %s for %s; capture owned %s",
                    current_pid,
                    package.bundle_id,
                    captured_pid,
                )
                return
            pymobiledevice.terminate_pid(serial, captured_pid)
            return

        self._require_macos()
        data = xcrun.run_json(
            [
                "devicectl",
                "device",
                "process",
                "list",
                "--device",
                serial,
                "--bundle-identifier",
                package.bundle_id,
            ],
            timeout=45.0,
        )
        pid = _find_pid_for_bundle(data, package.bundle_id)
        if not pid:
            # The app has already gone away.  This is a successful teardown,
            # and importantly does not cold-launch it merely to obtain a pid.
            return
        xcrun.run(
            [
                "devicectl",
                "device",
                "process",
                "terminate",
                "--device",
                serial,
                "--pid",
                str(pid),
                "--kill",
            ],
            timeout=60.0,
        )

    def create_temperature_monitor(
        self,
        serial: str,
        package: PackageInfo,
        threshold_c: float = 25.0,
        required_hits: int = 2,
        *,
        stop_event: Optional[threading.Event] = None,
        preview_only: bool = False,
    ) -> "IOSBatteryTemperatureMonitor":
        """Prepare the guard sidecar without launching ``package``.

        Temperature guard deliberately uses pymobiledevice3 on every host,
        including macOS; ordinary macOS capture continues through xcrun.
        """
        if not serial:
            raise ValueError("serial required")
        if not package or not package.bundle_id:
            raise ValueError("package bundle id required")
        if (
            isinstance(threshold_c, bool)
            or not math.isfinite(float(threshold_c))
            or not 5.0 <= float(threshold_c) <= 60.0
        ):
            raise ValueError("threshold_c must be between 5.0 and 60.0")
        if isinstance(required_hits, bool) or int(required_hits) < 1:
            raise ValueError("required_hits must be at least 1")

        with self._cache_lock:
            if serial in self._temperature_monitors:
                raise Pymobiledevice3Error(
                    f"temperature guard already active for iOS device {serial}"
                )

        device = self._resolve_device(serial)
        enriched = self._enrich_package(serial, package)
        reader = pymobiledevice.TemperatureGuardStreamReader(
            serial,
            enriched.bundle_id,
            float(threshold_c),
            required_hits=int(required_hits),
            interval_ms=1000,
            first_reading_timeout_sec=45.0,
            preview_only=preview_only,
        )
        try:
            if stop_event is not None and stop_event.is_set():
                raise Pymobiledevice3Error("iOS temperature guard preparation cancelled")
            reader.start()
            deadline = (
                time.monotonic()
                + IOSPymobiledeviceStreamingCaptureSession._CONNECT_TIMEOUT_SEC
            )
            prepared = False
            while not prepared:
                if stop_event is not None and stop_event.is_set():
                    raise Pymobiledevice3Error(
                        "iOS temperature guard preparation cancelled"
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                prepared = reader.wait_prepared(min(0.1, remaining))
                if reader.error or reader.failed():
                    break
            if not prepared or reader.error:
                reason = reader.error or "guard preparation timed out"
                raise Pymobiledevice3Error(
                    f"iOS temperature guard preparation failed: {reason}"
                )
        except Exception:
            reader.close()
            raise

        monitor = IOSBatteryTemperatureMonitor(
            serial,
            device,
            enriched,
            reader,
            on_release=self._release_temperature_monitor,
        )
        with self._cache_lock:
            # Reservation should make this impossible, but do not leak the
            # prepared subprocess if a concurrent caller won between checks.
            if serial in self._temperature_monitors:
                monitor.close()
                raise Pymobiledevice3Error(
                    f"temperature guard already active for iOS device {serial}"
                )
            self._temperature_monitors[serial] = monitor
        return monitor

    def create_temperature_preview_monitor(
        self,
        serial: str,
        package: PackageInfo,
        *,
        stop_event: Optional[threading.Event] = None,
    ) -> "IOSBatteryTemperatureMonitor":
        """Open a read-only temperature stream that cannot launch ``package``."""
        return self.create_temperature_monitor(
            serial,
            package,
            stop_event=stop_event,
            preview_only=True,
        )

    def _release_temperature_monitor(
        self, serial: str, monitor: "IOSBatteryTemperatureMonitor"
    ) -> None:
        with self._cache_lock:
            if self._temperature_monitors.get(serial) is monitor:
                self._temperature_monitors.pop(serial, None)

    def cold_start_and_open_streaming(
        self, serial: str, package: PackageInfo
    ) -> "IOSPymobiledeviceStreamingCaptureSession":
        """Promote an already-triggered guard reader into the capture session."""
        with self._cache_lock:
            monitor = self._temperature_monitors.get(serial)
        if monitor is None:
            raise Pymobiledevice3Error(
                f"no prepared iOS temperature guard for device {serial}"
            )
        reader, pid = monitor.promote(package)
        try:
            return IOSPymobiledeviceStreamingCaptureSession(
                serial,
                monitor.device,
                monitor.package,
                pid,
                reader=reader,
            )
        except Exception:
            reader.close()
            raise

    def _resolve_device(self, serial: str) -> DeviceInfo:
        with self._cache_lock:
            cached = self._device_cache.get(serial)
        if cached is not None:
            return cached
        for device in self.list_devices():
            if device.serial == serial:
                return device
        return DeviceInfo(serial=serial, platform=Platform.IOS)

    def _enrich_package(self, serial: str, package: PackageInfo) -> PackageInfo:
        key = (serial, package.bundle_id)
        with self._cache_lock:
            cached = self._package_cache.get(key)
        if cached is not None:
            return PackageInfo(
                bundle_id=package.bundle_id,
                label=package.label or cached.label,
                version_name=package.version_name or cached.version_name,
                version_code=package.version_code or cached.version_code,
                apk_path=package.apk_path or cached.apk_path,
                uid=package.uid or cached.uid,
            )
        try:
            packages = self.list_packages(serial, user_only=False)
        except (Pymobiledevice3Error, XcrunError):
            return package
        for candidate in packages:
            if candidate.bundle_id == package.bundle_id:
                return candidate
        return package


class IOSBatteryTemperatureMonitor:
    """Typed parent-side view of a prepared iOS guard sidecar.

    ``sample`` exposes only battery readings to the platform-neutral guard
    coordinator.  The sidecar independently counts the same threshold hits so
    it can launch on the already-open DVT connection; ``promote`` then transfers
    ownership of that exact reader to the streaming capture session.
    """

    _LAUNCH_WAIT_SEC = 50.0

    def __init__(
        self,
        serial: str,
        device: DeviceInfo,
        package: PackageInfo,
        reader: pymobiledevice.TemperatureGuardStreamReader,
        *,
        on_release: Optional[
            Callable[[str, "IOSBatteryTemperatureMonitor"], None]
        ] = None,
    ) -> None:
        self._serial = serial
        self.device = device
        self.package = package
        self._reader = reader
        self._on_release = on_release
        self._closed = False
        self._transferred = False
        self._release_lock = threading.Lock()

    @property
    def triggered(self) -> bool:
        return self._reader.wait_triggered(0.0) is not None

    @property
    def pid(self) -> Optional[int]:
        return self._reader.wait_triggered(0.0)

    def sample(self) -> Optional[BatteryTemperatureReading]:
        if self._closed:
            return None
        payload = self._reader.next_temperature(
            self._reader.first_reading_timeout_sec + 5.0
        )
        if payload is None:
            reason = self._reader.error
            if reason:
                raise Pymobiledevice3Error(reason)
            if self._reader.failed():
                raise Pymobiledevice3Error(
                    "iOS temperature guard stopped before producing a reading"
                )
            return None

        # The sidecar emits an internal reset marker for every missing/invalid
        # poll so the parent coordinator can reset its own consecutive-hit
        # counter. It intentionally becomes ``None`` here: only valid numeric
        # readings are exposed to UI/sample events.
        if payload.get("event") == "temperature_reset":
            return None

        value = payload.get("temperature_c")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 5.0 <= float(value) <= 60.0
        ):
            return None
        sampled_at = payload.get("sampled_at")
        if not isinstance(sampled_at, str) or not sampled_at:
            sampled_at = datetime.now(timezone.utc).isoformat()
        source = payload.get("source")
        if not isinstance(source, str) or not source:
            return None
        return BatteryTemperatureReading(
            temperature_c=float(value),
            sampled_at=sampled_at,
            source=source,
        )

    def promote(
        self, package: PackageInfo
    ) -> tuple[pymobiledevice.TemperatureGuardStreamReader, int]:
        if self._closed:
            raise Pymobiledevice3Error("iOS temperature guard is already closed")
        if package.bundle_id != self.package.bundle_id:
            raise Pymobiledevice3Error(
                "iOS temperature guard package changed while waiting"
            )
        pid = self._reader.wait_triggered(self._LAUNCH_WAIT_SEC)
        if pid is None:
            reason = self._reader.error or "target attach or launch did not complete"
            self.close()
            raise Pymobiledevice3Error(
                f"iOS temperature guard launch failed: {reason}"
            )
        with self._release_lock:
            self._transferred = True
            self._closed = True
        self._release()
        return self._reader, pid

    def _release(self) -> None:
        callback = self._on_release
        self._on_release = None
        if callback is not None:
            callback(self._serial, self)

    def close(self) -> None:
        with self._release_lock:
            if self._closed:
                return
            self._closed = True
            transferred = self._transferred
        if not transferred:
            self._reader.close()
        self._release()


def _battery_stats_from_frame(battery: dict) -> BatteryStats:
    """Map a streamer battery frame (level_percent / current_ma / voltage_mv /
    status, produced by mpi.ios.pmd3_stream._battery_fields) onto the
    unified BatteryStats schema."""
    level = battery.get("level_percent")
    return BatteryStats(
        level_percent=int(level) if isinstance(level, (int, float)) else None,
        current_ma=battery.get("current_ma"),
        voltage_mv=battery.get("voltage_mv"),
        status=battery.get("status"),
    )


def _capture_pymobiledevice_sample(
    serial: str,
    device: DeviceInfo,
    package: PackageInfo,
    pid: int,
    duration_sec: float,
) -> MetricsSnapshot:
    parsed = pymobiledevice.sample_process(
        serial,
        package,
        pid=pid,
        duration_sec=duration_sec,
    )
    return pymobiledevice.build_snapshot(device, package, duration_sec, parsed)


class IOSStreamingCaptureSession:
    """Streaming facade that exposes the Android session's sample contract."""

    def __init__(
        self,
        backend: IOSBackend,
        serial: str,
        device: DeviceInfo,
        package: PackageInfo,
    ):
        self._backend = backend
        self._serial = serial
        self.device = device
        self.package = package

    def sample(self, duration_sec: float) -> MetricsSnapshot:
        return self._backend.capture_sample(
            self._serial,
            self.device,
            self.package,
            duration_sec,
        )


class IOSPymobiledeviceStreamingCaptureSession:
    """Streaming session for Windows-compatible pymobiledevice3 captures.

    Drives a persistent metrics streamer (one DVT connection held open for the
    whole capture) and turns its per-second frames into snapshots. The previous
    implementation re-spawned ``sysmon process single`` per sample, which
    re-paid the multi-second tunnel/DVT handshake every time and yielded barely
    one sample per ~30 s with no FPS. If the streamer cannot start (or dies), we
    fall back to that single-shot path so capture still works, just slower.
    """

    # First sample blocks up to this long for the one-off tunnel/DVT handshake.
    _CONNECT_TIMEOUT_SEC = 90.0
    # If the streamer dies MID-capture (transient tunnel reset / brief device
    # lock), restart it this many times before giving up to single-shot, so a long
    # capture keeps its 1 Hz cadence + FPS instead of degrading for good. Each
    # restart re-resolves the RSD address from tunneld, so it also recovers from
    # a tunnel that dropped and re-formed on a ROTATED port (Wi-Fi blips).
    _MAX_STREAMER_RESTARTS = 3
    # Backgrounded-app detection: iOS SUSPENDS a backgrounded game (no background
    # execution), so its Sysmontap cpuUsage flatlines at exactly 0 while the
    # process stays in the table. A FOREGROUND app always burns some CPU every
    # second (render loop, vsync, runloop), so this many consecutive ~1 Hz
    # windows at effectively zero CPU means the app left the foreground.
    _SUSPENDED_CPU_MAX = 0.05
    _SUSPENDED_WINDOWS = 5

    def __init__(
        self,
        serial: str,
        device: DeviceInfo,
        package: PackageInfo,
        pid: int,
        *,
        reader: Optional[pymobiledevice.SysmonStreamReader] = None,
    ):
        self._serial = serial
        self.device = device
        self.package = package
        self.pid = pid
        self._first_sample = True
        self._legacy = False
        self._closing = False
        self._restarts = 0
        self._metric_cache: dict = {}
        self._raw_last_timestamp: dict = {}
        self._raw_dropped_seen = 0
        self._battery_field_times: dict = {}
        self._battery_metric_seen: dict = {}
        # Last good reading, held when a 1 s window drains no frames (see
        # _snapshot_from_frames) so the per-second series doesn't flicker to zero.
        self._last_cpu = 0.0
        self._last_mem_kb = 0
        self._last_fps: float | None = None
        self._last_gpu: float | None = None
        self._last_battery: dict | None = None
        # System-wide CPU (raw per-core-summed) and the device core count, held
        # like the rest so a window that drains no frames keeps the last value.
        self._last_total_raw: float | None = None
        self._last_cores: int | None = None
        # Consecutive sample windows in which the target pid was absent from
        # every drained sysmon frame — the app-exited detector (see
        # _snapshot_from_frames). Sysmontap streams the full process table, so
        # a running app is present in every frame; several all-missing windows
        # in a row mean the process is gone.
        self._missing_streak = 0
        # Consecutive windows where the process is present but its CPU is
        # flatlined at ~0 — the app-backgrounded (suspended) detector.
        self._suspended_streak = 0
        self._reader = reader if reader is not None else self._spawn_reader()
        if self._reader is None:
            self._legacy = True

    def _spawn_reader(self):
        """Create and start a fresh streamer subprocess, or return None (and log)
        when it cannot start so the caller degrades to single-shot."""
        reader = pymobiledevice.SysmonStreamReader(
            self._serial, self.pid, bundle_id=self.package.bundle_id, interval_ms=1000
        )
        try:
            reader.start()
        except Pymobiledevice3Error as exc:
            # No interpreter / streamer available — degrade to single-shot. This is
            # the usual cause of a capture that only samples every ~20 s with no
            # FPS (single-shot re-pays the tunnel/DVT handshake every sample and
            # carries no FPS), so log loudly: in a frozen build it typically means
            # no system Python with pymobiledevice3 is reachable to run the streamer.
            logger.warning(
                "iOS metrics streamer could not start; falling back to slow "
                "single-shot sampling (no FPS): %s",
                exc,
            )
            return None
        return reader

    def _restart_reader(self) -> bool:
        """Replace a streamer that died mid-capture with a fresh one, up to
        _MAX_STREAMER_RESTARTS times. Returns True when a new streamer is running;
        the caller resets _first_sample so the one-off handshake re-runs."""
        if self._closing or self._restarts >= self._MAX_STREAMER_RESTARTS:
            return False
        self._restarts += 1
        old = self._reader
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        reader = self._spawn_reader()
        if reader is None:
            self._reader = None
            return False
        self._reader = reader
        self._metric_cache.clear()
        self._raw_dropped_seen = 0
        self._first_sample = True  # re-run the connect handshake on the next sample
        logger.warning(
            "restarted iOS metrics streamer after mid-capture failure (%d/%d)",
            self._restarts,
            self._MAX_STREAMER_RESTARTS,
        )
        return True

    def sample(self, duration_sec: float) -> MetricsSnapshot:
        if self._closing:
            # Capture is being torn down; return immediately so the pump can
            # notice its stop flag (it drops this snapshot anyway).
            return self._snapshot_from_frames([], duration_sec)
        # A reader that dies MID-capture (after the first sample) can never produce
        # more frames. Try to restart it so the capture keeps its 1 Hz cadence + FPS
        # across a transient tunnel hiccup; only once the restart budget is spent do
        # we degrade to single-shot. On the FIRST sample we don't restart here — we
        # always let _sample_stream block on the one-off handshake and surface the
        # streamer's real failure reason instead of swallowing it.
        if (
            not self._legacy
            and not self._first_sample
            and (self._reader is None or self._reader.failed())
        ):
            if not self._restart_reader():
                self._legacy = True
        if self._legacy:
            try:
                snap = _capture_pymobiledevice_sample(
                    self._serial, self.device, self.package, self.pid, duration_sec
                )
            except Pymobiledevice3Error as exc:
                # The single-shot sampler raises this exact message when the pid
                # is absent from the sysmon snapshot — the same "app died"
                # signal the streamer path detects via ``missing`` frames.
                if "could not find sysmon process sample" in str(exc):
                    self._missing_streak += 1
                    if self._missing_streak >= 3:
                        raise AppExitedError(
                            f"app-exited: {self.package.bundle_id} (pid {self.pid}) "
                            "disappeared from the device process list; measurement stopped."
                        ) from exc
                raise
            self._missing_streak = 0
            return snap
        return self._sample_stream(duration_sec)

    def _sample_stream(self, duration_sec: float) -> MetricsSnapshot:
        reader = self._reader
        assert reader is not None
        if self._first_sample:
            self._first_sample = False
            # Block once for the connection handshake; if it never connects,
            # drop to the single-shot path for this and subsequent samples.
            connected = reader.wait_ready(max(self._CONNECT_TIMEOUT_SEC, duration_sec))
            if self._closing:
                return self._snapshot_from_frames([], duration_sec)
            if not connected or reader.failed():
                self._legacy = True
                # The streamer didn't deliver a first frame. If it reported a
                # concrete reason (a fatal/error event or its stderr tail), surface
                # THAT instead of silently re-running the identical single-shot,
                # which over a wireless tunnel floods and times out the same way —
                # the old behaviour cost ~90 s (wait_ready) + ~90 s (single-shot)
                # and only ever showed the generic CLI timeout, hiding the cause.
                reason = getattr(reader, "error", None)
                if reason:
                    raise Pymobiledevice3Error(f"iOS metrics streamer failed: {reason}")
                # No diagnostic reason (pure silent timeout): fall back to the
                # single-shot path exactly as before so USB captures still degrade
                # gracefully.
                logger.warning(
                    "iOS metrics streamer produced no frame within %.0fs; falling "
                    "back to slow single-shot sampling (no FPS).",
                    max(self._CONNECT_TIMEOUT_SEC, duration_sec),
                )
                return _capture_pymobiledevice_sample(
                    self._serial, self.device, self.package, self.pid, duration_sec
                )

        if duration_sec > 0:
            time.sleep(duration_sec)
        frames = reader.drain()
        return self._snapshot_from_frames(frames, duration_sec)

    def _snapshot_from_frames(
        self, frames: List[dict], duration_sec: float
    ) -> MetricsSnapshot:
        # Focused-app measurement: end the capture (with a notice) when the
        # measured process has vanished. The streamer stamps ``missing`` on any
        # frame whose sysmon snapshot lacks the pid, so 3 consecutive all-missing
        # windows (~3 s) mean the app exited — not a transient tap gap. Windows
        # that drained no frames are ignored (they carry no evidence either way).
        if frames and not self._closing:
            if all(f.get("missing") for f in frames):
                self._missing_streak += 1
                if self._missing_streak >= 3:
                    raise AppExitedError(
                        f"app-exited: {self.package.bundle_id} (pid {self.pid}) "
                        "disappeared from the device process list; measurement stopped."
                    )
            else:
                self._missing_streak = 0
        cpus = [f["cpu"] for f in frames if isinstance(f.get("cpu"), (int, float))]
        # Focused-app measurement: end the capture when the app has left the
        # foreground. iOS suspends backgrounded apps outright, so a process
        # that is still listed but reports (effectively) zero cpuUsage for
        # several consecutive windows is no longer running in front of the
        # user — keeping the capture alive would just record the home screen.
        if cpus and not self._closing:
            if max(cpus) <= self._SUSPENDED_CPU_MAX:
                self._suspended_streak += 1
                if self._suspended_streak >= self._SUSPENDED_WINDOWS:
                    raise AppBackgroundedError(
                        f"app-backgrounded: {self.package.bundle_id} (pid {self.pid}) "
                        "has been suspended (moved to the background); measurement stopped."
                    )
            else:
                self._suspended_streak = 0
        cores_vals = [
            f["cores"] for f in frames if isinstance(f.get("cores"), int) and f["cores"] > 0
        ]

        # Permit a short cadence mismatch, then expose a real data gap. Source
        # timestamps survive repeated emissions, so a dead instrument cannot
        # refresh itself merely because Sysmontap is still running.
        now = time.time()
        metric_status: dict = {}

        def reading(key, source, scope, ttl=3.0):
            changed = False
            new_values = []
            for f in frames:
                value = f.get(key)
                if value is None or isinstance(value, bool):
                    continue
                if isinstance(value, (int, float)) and not math.isfinite(value):
                    continue
                at = f.get(key + "_at", f.get("t", now))
                if not isinstance(at, (int, float)) or not math.isfinite(at):
                    continue
                if at > now + 1.0:
                    continue
                previous = self._metric_cache.get(key)
                if previous is None or at > previous[1]:
                    actual_source = f.get(key + "_source") or source
                    if previous is not None and previous[2] != actual_source:
                        new_values.clear()
                    self._metric_cache[key] = (value, at, actual_source)
                    changed = True
                    if key in ("cpu", "cpu_total", "fps", "gpu") and now - at <= ttl:
                        new_values.append(value)
            if new_values:
                latest = self._metric_cache[key]
                self._metric_cache[key] = (sum(new_values) / len(new_values), latest[1], latest[2])
            cached = self._metric_cache.get(key)
            age = max(0.0, now - cached[1]) if cached else None
            available = cached is not None and -1.0 <= now - cached[1] <= ttl
            metric_status[key] = {
                "source": cached[2] if cached else source, "scope": scope,
                "status": ("fresh" if changed and age <= (6.0 if ttl > 3 else 1.5) else "held") if available else "unavailable",
                "age_sec": round(age, 3) if age is not None else None,
            }
            return cached[0] if available else None

        cpu_pct = reading("cpu", "ios_sysmontap", "selected_process")
        mem_bytes = reading("mem_bytes", "ios_sysmontap", "selected_process")
        mem_kb = int(round(mem_bytes / 1024)) if mem_bytes is not None else None
        self._last_fps = reading("fps", "ios_graphics_core_animation", "system_display")
        self._last_gpu = reading("gpu", "ios_graphics", "system_device")
        self._last_battery = reading("battery", "ios_diagnostics", "system_device", 15.0)
        for frame in frames:
            if isinstance(frame.get("battery_field_times"), dict):
                self._battery_field_times = frame["battery_field_times"]
        if self._last_battery and self._battery_field_times:
            self._last_battery = {key: value for key, value in self._last_battery.items()
                                 if isinstance(self._battery_field_times.get(key), (int, float))
                                 and 0 <= now - self._battery_field_times[key] <= 15.0}
        for name, field in (("battery_level", "level_percent"), ("battery_current", "current_ma"),
                            ("battery_voltage", "voltage_mv"), ("temperature", "temp_c")):
            at = self._battery_field_times.get(field)
            age = max(0.0, now - at) if isinstance(at, (int, float)) else metric_status["battery"]["age_sec"]
            value = (self._last_battery or {}).get(field)
            changed = at is not None and at > self._battery_metric_seen.get(name, -1)
            if at is not None:
                self._battery_metric_seen[name] = at
            metric_status[name] = {
                "source": "ios_battery", "scope": "system_device",
                "status": "unavailable" if value is None else ("fresh" if changed and age <= 6 else "held"),
                "age_sec": round(age, 3) if age is not None else None,
            }
        self._last_total_raw = reading("cpu_total", "ios_sysmontap_process_sum", "system_device")
        if cores_vals:
            self._last_cores = cores_vals[-1]

        # kdebug frame accounting (see pmd3_stream._kdebug_fps_loop): each frame
        # may carry the display-commit frame/jank counts of the kdebug windows
        # that completed since the previous sysmon frame. Sum them over this
        # sample window — they are per-window event counts, not gauges, so no
        # hold-last is applied (an empty window genuinely has 0 new frames).
        k_frames = sum(
            f["k_frames"] for f in frames
            if isinstance(f.get("k_frames"), int) and not isinstance(f.get("k_frames"), bool)
        )
        k_jank = sum(
            f["k_jank"] for f in frames
            if isinstance(f.get("k_jank"), int) and not isinstance(f.get("k_jank"), bool)
        )
        k_big = sum(
            f["k_big"] for f in frames
            if isinstance(f.get("k_big"), int) and not isinstance(f.get("k_big"), bool)
        )

        def _k_ms(key: str) -> List[float]:
            return [
                float(f[key]) for f in frames
                if isinstance(f.get(key), (int, float)) and not isinstance(f.get(key), bool)
            ]

        k_p50s, k_p90s, k_p95s, k_p99s = (
            _k_ms("k_p50"), _k_ms("k_p90"), _k_ms("k_p95"), _k_ms("k_p99")
        )

        cpu = CpuStats(pid=self.pid, cpu_percent=cpu_pct)
        # App CPU% (cpu_percent) is the raw per-core-summed value (can exceed 100
        # across cores). Normalize to a 0-100 basis by the core count, and surface
        # the system-wide total the same way. iOS has no per-core frequency over
        # pymobiledevice3, so this is a core/thread-count normalization with no
        # frequency scaling (on Apple silicon logical CPUs == cores == threads).
        cores = self._last_cores
        cpu.logical_core_count = cores
        cpu.total_source = "ios_sysmontap_process_sum"
        cpu.total_status = metric_status["cpu_total"]["status"]
        if cores:
            if cpu_pct is not None:
                cpu.cpu_norm = max(0.0, min(100.0, cpu_pct / cores))
            if self._last_total_raw is not None:
                cpu.total_percent = max(0.0, min(100.0, self._last_total_raw / cores))
        metric_status["cpu_norm"] = dict(metric_status["cpu"])
        if cpu.cpu_norm is None:
            metric_status["cpu_norm"]["status"] = "unavailable"
        if cpu.total_percent is None:
            metric_status["cpu_total"]["status"] = "unavailable"
            cpu.total_status = "unavailable"
        memory = MemStats(pss_total_kb=mem_kb, rss_total_kb=mem_kb)
        frame_stats = (
            FrameStats(fps_estimate=self._last_fps)
            if self._last_fps is not None
            else FrameStats()
        )
        frame_stats.source = metric_status["fps"]["source"]
        frame_stats.scope = "system_display"
        frame_stats.experimental = frame_stats.source == "ios_kdebug_display_experimental"
        for frame in frames:
            for event in frame.get("raw_frame_events", []):
                if not isinstance(event, dict):
                    continue
                timestamp, duration = event.get("timestamp_ns"), event.get("duration_ns")
                identity = (event.get("stream_id"), event.get("source"))
                if (not isinstance(timestamp, (int, float)) or not isinstance(duration, (int, float))
                        or isinstance(timestamp, bool) or isinstance(duration, bool)
                        or not math.isfinite(timestamp) or not math.isfinite(duration) or duration <= 0):
                    continue
                if timestamp <= self._raw_last_timestamp.get(identity, -1):
                    continue
                self._raw_last_timestamp[identity] = timestamp
                frame_stats.raw_events.append(event)
            dropped = frame.get("raw_frame_events_dropped", 0)
            if isinstance(dropped, int) and dropped > self._raw_dropped_seen:
                frame_stats.raw_events_dropped += dropped - self._raw_dropped_seen
                self._raw_dropped_seen = dropped
            frame_stats.raw_events_dropped += max(0, frame.get("raw_buffer_events_dropped", 0))
        if k_frames > 0:
            # Jank derived from kdebug frame-commit deltas:
            # janky_frames is the total, split into small/big by severity.
            frame_stats.total_frames = k_frames
            frame_stats.janky_frames = k_jank
            frame_stats.small_jank = max(0, k_jank - k_big)
            frame_stats.big_jank = k_big
            frame_stats.jank_percent = k_jank / k_frames * 100.0
        if k_p50s:
            # Frame-time percentiles (ms) from the kdebug windows: the median
            # averages across drained windows; the tail percentiles keep the
            # WORST window so stutter spikes survive aggregation into the
            # ~1 Hz snapshot (and the frame_p??_ms CSV columns).
            frame_stats.p50_ms = round(sum(k_p50s) / len(k_p50s), 2)
            frame_stats.p90_ms = max(k_p90s) if k_p90s else 0.0
            frame_stats.p95_ms = max(k_p95s) if k_p95s else 0.0
            frame_stats.p99_ms = max(k_p99s) if k_p99s else 0.0
        gpu = (
            GpuStats(busy_percent=self._last_gpu)
            if self._last_gpu is not None
            else GpuStats()
        )
        battery = (
            _battery_stats_from_frame(self._last_battery)
            if self._last_battery
            else BatteryStats()
        )
        # Battery temperature rides along on the same diagnostics frame; surface
        # it as the "battery" thermal zone so it lights the Temp card (whose
        # primary series is thermal:battery) and flows through the CSV/report
        # exactly like Android's battery thermal zone. It is held with the rest
        # of the battery reading, so empty windows keep the last good value.
        temp_c = self._last_battery.get("temp_c") if self._last_battery else None
        temp_at = self._last_battery.get("temp_at") if self._last_battery else None
        if temp_at is not None and (not isinstance(temp_at, (int, float)) or now - temp_at > 15.0):
            temp_c = None
        thermal = (
            ThermalStats(zones_c={"battery": temp_c})
            if isinstance(temp_c, (int, float)) and not isinstance(temp_c, bool)
            else ThermalStats()
        )

        return MetricsSnapshot(
            device=self.device,
            package=self.package,
            captured_at=datetime.now(timezone.utc).isoformat(),
            duration_sec=duration_sec,
            frames=frame_stats,
            memory=memory,
            cpu=cpu,
            gpu=gpu,
            thermal=thermal,
            battery=battery,
            metric_status=metric_status,
            raw={
                "pymobiledevice3_stream_frames": len(frames),
                "pymobiledevice3_stream_last": {k: v for k, v in frames[-1].items() if k != "raw_frame_events"} if frames else {},
            },
        )

    def close(self) -> None:
        self._closing = True
        if self._reader is not None:
            self._reader.close()
