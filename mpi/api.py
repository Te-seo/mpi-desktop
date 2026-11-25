"""JSON-IPC backend for the Electron frontend.

Reads newline-delimited JSON requests from stdin and writes newline-delimited
JSON responses to stdout. All diagnostics go to stderr through the ``logging``
module so they never corrupt the protocol.

Request:
    {"id": "<correlation id>", "command": "<name>", "payload": {...}}

Response (success):
    {"id": "<id>", "ok": true, "result": <value>}

Response (failure):
    {"id": "<id>", "ok": false, "error": "<message>"}

Streaming events (currently reserved for future live-capture support):
    {"event": "<name>", "data": {...}}

Handlers:
    - ping           : health check
    - load_file      : parse an MPI raw CSV export (A1="MPI" header) and return
                       its summary + snapshots, used by the live dashboard's
                       replay/load
    - list_devices   : enumerate connected Android/iOS devices
    - list_packages  : enumerate user-installed packages/apps on a device
    - start_capture  : open a StreamingCaptureSession and stream snapshots as
                       unsolicited "snapshot" events
    - stop_capture   : signal a running capture to wind down
    - create_temperature_guard / activate_temperature_guard /
      stop_temperature_guard: reserve a physical device, wait for two valid
      threshold readings, then attach or launch and promote into live capture
    - shutdown       : exit cleanly
"""

from __future__ import annotations

import base64
import csv
import dataclasses
import json
import logging
import math
import os
import re
import shutil
import stat
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import __name__ as _pkg_name  # noqa: F401  (just to ensure package import)
from .analysis import (
    analyze_session,
    list_saved_sessions,
    read_list_item,
    session_dedup_key,
)
from .android.backend import AndroidBackend, cache_app_icon, cache_app_label
from .android.chart_memory import parse_replay_memory
from .backend import AppBackgroundedError, AppExitedError
from .ios.backend import IOSBackend
from .migration import migrate_session_storage, session_roots
from .reports import export_data_to_excel
from .reports.adapter import summary_to_report
from .report import (
    LEGACY_MPI_MARKER,
    MPI_MARKER,
    build_endpoint_payload,
    captured_span_seconds,
    gen_info_hash,
    generate_csv_report,
)
from .schema import (
    BatteryTemperatureReading,
    BatteryStats,
    CpuStats,
    DeviceInfo,
    FrameStats,
    GpuStats,
    MemStats,
    MetricsSnapshot,
    NetworkStats,
    PackageInfo,
    Platform,
    ThermalStats,
    TemperatureGuardConfig,
)

logger = logging.getLogger(__name__)


# Live capture state, keyed by capture_id.
class _CaptureContext:
    __slots__ = (
        "thread",
        "stop",
        "session",
        "duration_sec",
        "serial",
        "device_key",
        "guard_id",
        "platform",
        "backend",
        "auto_stop_after_sec",
        "measurement_started_at",
        "auto_stop_deadline",
        "auto_stop_timer",
        "stop_reason",
        "auto_stop_termination_done",
        "target_app_termination_error",
        "cleanup_lock",
        "session_closed",
        "overlay",
        "overlay_lock",
        "mobile_session_id",
        "pause_started_at", "paused_total_sec", "pause_generation",
    )

    def __init__(
        self,
        thread: threading.Thread,
        stop: threading.Event,
        session: object,
        duration_sec: float,
        serial: str,
        device_key: Optional[str] = None,
        guard_id: Optional[str] = None,
        platform: str = "android",
        backend: Optional[object] = None,
        auto_stop_after_sec: Optional[float] = None,
        overlay: Optional[object] = None,
        mobile_session_id: Optional[str] = None,
    ):
        self.thread = thread
        self.stop = stop
        self.session = session
        self.duration_sec = duration_sec
        self.pause_started_at: Optional[float] = None
        self.paused_total_sec = 0.0
        self.pause_generation = 0
        self.serial = serial
        self.device_key = device_key or serial
        self.guard_id = guard_id
        self.platform = platform
        self.backend = backend
        self.auto_stop_after_sec = auto_stop_after_sec
        # Monotonic anchor established by the first successfully sampled
        # snapshot. Guard/preflight time therefore never reaches the HUD timer.
        self.measurement_started_at: Optional[float] = None
        # Optional companion.OverlaySession driving the measured device's HUD.
        self.overlay = overlay
        self.mobile_session_id = mobile_session_id or str(uuid.uuid4())
        # Serializes opening/reconfiguring that session. Opening one is slow (adb
        # forward + connect + configure), so it cannot be done under
        # _captures_lock; without this, two toggles would each open a session and
        # the loser would be dropped with its socket, pusher thread and forward
        # rule still live. Lock order is always overlay_lock -> _captures_lock,
        # and the capture pump never takes it, so a wedged phone cannot stall a
        # measurement.
        self.overlay_lock = threading.Lock()
        self.auto_stop_deadline: Optional[float] = None
        self.auto_stop_timer: Optional[threading.Timer] = None
        # Set under _captures_lock.  The winner owns all stop-side effects.
        self.stop_reason: Optional[str] = None
        self.auto_stop_termination_done = threading.Event()
        self.target_app_termination_error: Optional[str] = None
        self.cleanup_lock = threading.Lock()
        self.session_closed = False


class _TemperatureGuardContext:
    __slots__ = (
        "guard_id",
        "config",
        "serial",
        "device_key",
        "backend",
        "stop",
        "thread",
        "monitor",
        "state",
        "capture_id",
        "ended",
        "cleanup_lock",
    )

    def __init__(
        self,
        guard_id: str,
        config: TemperatureGuardConfig,
        serial: str,
        device_key: str,
        backend: object,
    ) -> None:
        self.guard_id = guard_id
        self.config = config
        self.serial = serial
        self.device_key = device_key
        self.backend = backend
        self.stop = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.monitor: Optional[object] = None
        self.state = "preparing"
        self.capture_id: Optional[str] = None
        self.ended = False
        self.cleanup_lock = threading.Lock()


class _TemperaturePreviewContext:
    __slots__ = (
        "preview_id",
        "platform",
        "serial",
        "device_key",
        "backend",
        "device",
        "package",
        "stop",
        "thread",
        "monitor",
        "state",
        "ended",
        "cleanup_lock",
        "closed_resource_ids",
    )

    def __init__(
        self,
        preview_id: str,
        platform: str,
        serial: str,
        device_key: str,
        backend: object,
        device: DeviceInfo,
        package: PackageInfo,
    ) -> None:
        self.preview_id = preview_id
        self.platform = platform
        self.serial = serial
        self.device_key = device_key
        self.backend = backend
        self.device = device
        self.package = package
        self.stop = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.monitor: Optional[object] = None
        self.state = "preparing"
        self.ended = False
        self.cleanup_lock = threading.Lock()
        self.closed_resource_ids: set[int] = set()


_active_captures: Dict[str, _CaptureContext] = {}
# Serials with a capture being opened but not yet in _active_captures, so a
# concurrent start on the same device is rejected during the (slow) open handshake
# too — not just once the capture is fully registered.
_reserved_serials: set = set()
_active_temperature_guards: Dict[str, _TemperatureGuardContext] = {}
_active_temperature_previews: Dict[str, _TemperaturePreviewContext] = {}
_captures_lock = threading.Lock()
_android_backend = AndroidBackend()
_ios_backend = IOSBackend()

TEMPERATURE_GUARD_SAMPLE_INTERVAL_SEC = 1.0
TEMPERATURE_GUARD_FIRST_READING_TIMEOUT_SEC = 45.0
SHUTDOWN_JOIN_TIMEOUT_SEC = 5.0
DEFAULT_TEMPERATURE_GUARD_THRESHOLD_C = 25.0
_DEFAULT_CAPTURE_INTERVAL_SEC = {"android": 0.5, "ios": 1.0}
_MIN_BATTERY_TEMPERATURE_C = 5.0
_MAX_BATTERY_TEMPERATURE_C = 60.0
AUTO_STOP_MIN_SEC = 1.0
AUTO_STOP_MAX_SEC = 3600.0


# --- JSON helpers ---------------------------------------------------------


def _json_default(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    if isinstance(obj, Enum):
        return obj.value
    raise TypeError(f"not json serializable: {type(obj).__name__}")


def _strip_internal(value: Any) -> Any:
    """Drop ``_raw_cache_data`` and other private keys before serialization."""
    if isinstance(value, dict):
        return {
            k: _strip_internal(v)
            for k, v in value.items()
            if not str(k).startswith("_")
        }
    if isinstance(value, list):
        return [_strip_internal(v) for v in value]
    return value


_stdout_lock = threading.Lock()
_stdout_alive = True


def _emit(payload: Dict[str, Any]) -> None:
    """Write one JSON line to stdout. Treats broken-pipe errors as a graceful
    parent-exit signal — Electron killing this sidecar mid-write produces
    OSError on Windows (Errno 22 "Invalid argument") or BrokenPipeError on
    *nix. Either way, there's nobody to receive the message; we should not
    surface this as a user-visible PyInstaller dialog."""
    global _stdout_alive
    if not _stdout_alive:
        return
    line = json.dumps(payload, ensure_ascii=False, default=_json_default)
    with _stdout_lock:
        try:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        except (OSError, ValueError):
            # OSError covers BrokenPipeError, EPIPE, EINVAL on closed pipes;
            # ValueError covers writes to an already-closed file object.
            _stdout_alive = False


# --- Handlers -------------------------------------------------------------


def _handle_ping(_: Dict[str, Any]) -> Dict[str, Any]:
    return {"pong": True}


def _looks_like_live_csv(path: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.reader(fh)
            seen_metadata = False
            seen_timeseries = False
            for i, row in enumerate(reader):
                if len(row) >= 2 and row[0] == "# Section":
                    seen_metadata = seen_metadata or row[1] == "Metadata"
                    seen_timeseries = seen_timeseries or row[1] == "Time-series"
                if seen_metadata and seen_timeseries:
                    return True
                if i > 200:
                    break
    except (OSError, UnicodeError, csv.Error):
        return False
    return False


def _validate_mpi_header(path: str) -> Optional[str]:
    """Inspect cells A1/A2 of a CSV for the MPI raw-export header.

    Returns the 16-char hash when the file carries a valid header (A1 == "MPI"
    and A2 is a 16-hex hash). Returns ``None`` when there is no MPI marker at all
    (a legacy/offline CSV — caller falls through to existing handling). Raises
    ``ValueError`` when the marker is present but the hash cell is missing or
    malformed, so a corrupt raw export fails loudly instead of silently.
    """
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            cells: List[str] = []
            for row in csv.reader(fh):
                if row and row[0].strip():
                    cells.append(row[0].strip())
                if len(cells) >= 2:
                    break
    except (OSError, UnicodeError, csv.Error):
        return None

    if not cells or cells[0] not in (MPI_MARKER, LEGACY_MPI_MARKER):
        return None
    a2 = cells[1] if len(cells) >= 2 else ""
    if not re.fullmatch(r"[0-9a-fA-F]{16}", a2):
        raise ValueError(
            "MPI raw CSV header is invalid: cell A2 must hold a 16-character hash"
        )
    return a2.lower()


def _float_or_none(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> Optional[int]:
    parsed = _float_or_none(value)
    return int(parsed) if parsed is not None else None


def _opt_str(value: Any) -> Optional[str]:
    """A trimmed non-empty string, or None."""
    return value.strip() if isinstance(value, str) and value.strip() else None


def _trusted_cpu_frequency_columns(header: Sequence[str]) -> List[str]:
    """Return exact contiguous ``cpu0_mhz..cpuN_mhz`` columns, or none.

    Validate the original CSV header before rows are converted to dictionaries:
    that conversion collapses duplicate names and would otherwise make a
    duplicate or sparse core layout look trustworthy. Column order is part of
    the report contract, so reordered, duplicate, sparse, or non-canonical
    numeric names cannot establish a logical-CPU divisor.
    """
    columns = [name for name in header if re.fullmatch(r"cpu[0-9]+_mhz", name)]
    expected = [f"cpu{index}_mhz" for index in range(len(columns))]
    return columns if columns == expected else []


def _json_cell(value: Any, expected_type: type):
    try:
        parsed = json.loads(value or "null")
    except (ValueError, TypeError):
        return expected_type()
    return parsed if isinstance(parsed, expected_type) else expected_type()


def _parse_live_csv(path: str) -> List[MetricsSnapshot]:
    csv.field_size_limit(max(csv.field_size_limit(), 16 * 1024 * 1024))
    metadata: Dict[str, str] = {}
    timeseries_header: List[str] = []
    timeseries_rows: List[Dict[str, str]] = []
    section: Optional[str] = None
    waiting_for_timeseries_header = False

    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.reader(fh):
            if not row:
                section = None
                continue
            if len(row) >= 2 and row[0] == "# Section":
                section = row[1]
                waiting_for_timeseries_header = section == "Time-series"
                continue
            if section == "Metadata":
                if len(row) >= 2 and row[0] != "key":
                    metadata[row[0]] = row[1]
                continue
            if section == "Time-series":
                if waiting_for_timeseries_header:
                    timeseries_header = row
                    waiting_for_timeseries_header = False
                    continue
                if timeseries_header:
                    padded = row + [""] * max(0, len(timeseries_header) - len(row))
                    timeseries_rows.append(dict(zip(timeseries_header, padded)))

    if not metadata or not timeseries_rows:
        raise RuntimeError("not a live tracing CSV export")

    # Restore the platform from metadata instead of forcing Android — an iOS
    # capture must load back as iOS.
    plat_raw = (metadata.get("device.platform") or "").strip().lower()
    platform = Platform.IOS if plat_raw == "ios" else Platform.ANDROID
    # Only the SHA-256 serial hash is ever stored (never the raw serial); surface
    # it as the identity so a loaded session keeps a stable device id.
    serial_hash = metadata.get("device.serial_hash", "")
    device = DeviceInfo(
        serial=serial_hash or "offline",
        platform=platform,
        model=metadata.get("device.model", "unknown"),
        model_identifier=metadata.get("device.model_identifier", ""),
        model_number=metadata.get("device.model_number", ""),
        display_name=metadata.get("device.display_name", ""),
        os_version=metadata.get("device.os_version", "unknown"),
        build_version=metadata.get("device.build_version", ""),
        build_name=metadata.get("device.build_name", ""),
        connection_type="usb",
        code_name=metadata.get("device.code_name", ""),
        hw_ver=metadata.get("device.hw_ver", ""),
    )
    package = PackageInfo(
        bundle_id=metadata.get("package.bundle_id", "unknown"),
        label=metadata.get("package.label") or None,
        version_name=metadata.get("package.version_name") or None,
        version_code=metadata.get("package.version_code") or None,
        uid=metadata.get("package.uid") or None,
    )

    cpu_cols = _trusted_cpu_frequency_columns(timeseries_header)
    snapshots: List[MetricsSnapshot] = []
    for row in timeseries_rows:
        thermal = {
            key[len("thermal_") : -len("_c")]: value
            for key, raw in row.items()
            if key.startswith("thermal_")
            and key.endswith("_c")
            and (value := _float_or_none(raw)) is not None
        }
        snapshots.append(
            MetricsSnapshot(
                device=device,
                package=package,
                captured_at=row.get("captured_at", ""),
                duration_sec=float(row.get("duration_sec") or 0.0),
                elapsed_sec=_float_or_none(row.get("elapsed_sec")),
                pause_used=metadata.get("capture.pause_used", "").strip().lower() == "true",
                frames=FrameStats(
                    fps_estimate=_float_or_none(row.get("fps")),
                    jank_percent=float(row.get("jank_percent") or 0.0),
                    total_frames=int(float(row.get("frames_total") or 0)),
                    janky_frames=int(float(row.get("frames_janky") or 0)),
                    small_jank=int(float(row.get("frames_small_jank") or 0)),
                    big_jank=int(float(row.get("frames_big_jank") or 0)),
                    p50_ms=float(row.get("frame_p50_ms") or 0.0),
                    p90_ms=float(row.get("frame_p90_ms") or 0.0),
                    p95_ms=float(row.get("frame_p95_ms") or 0.0),
                    p99_ms=float(row.get("frame_p99_ms") or 0.0),
                    missed_vsync=int(float(row.get("frames_missed_vsync") or 0)),
                    slow_ui_thread=int(float(row.get("frames_slow_ui_thread") or 0)),
                    frame_deadline_missed=int(float(row.get("frames_deadline_missed") or 0)),
                    source=row.get("fps_source") or None,
                    scope=row.get("fps_scope") or None,
                    experimental=str(row.get("fps_experimental")).lower() in ("true", "1"),
                    raw_events=_json_cell(row.get("raw_frame_events"), list),
                    raw_events_dropped=_int_or_none(row.get("raw_frame_events_dropped")) or 0,
                ),
                memory=MemStats(
                    pss_total_kb=_int_or_none(row.get("mem_pss_kb")),
                    rss_total_kb=_int_or_none(row.get("mem_rss_kb")),
                    **parse_replay_memory(row, plat_raw),
                    java_heap_kb=int(float(row.get("mem_java_heap_kb") or 0)),
                    native_heap_kb=int(float(row.get("mem_native_heap_kb") or 0)),
                    graphics_kb=int(float(row.get("mem_graphics_kb") or 0)),
                    code_kb=int(float(row.get("mem_code_kb") or 0)),
                    stack_kb=int(float(row.get("mem_stack_kb") or 0)),
                    sys_total_kb=int(float(row.get("mem_sys_total_kb") or 0)),
                    sys_used_kb=int(float(row.get("mem_sys_used_kb") or 0)),
                ),
                cpu=CpuStats(
                    cpu_percent=_float_or_none(row.get("cpu_percent")),
                    mem_percent=float(row.get("mem_percent") or 0.0),
                    total_percent=_float_or_none(row.get("cpu_total_percent")),
                    cpu_norm=_float_or_none(row.get("cpu_norm")),
                    per_core_percent=_json_cell(row.get("cpu_per_core_percent"), list),
                    logical_core_count=_int_or_none(row.get("cpu_logical_core_count")),
                    total_source=row.get("cpu_total_source") or None,
                    total_status=row.get("cpu_total_status") or None,
                    # Position-anchored: keep each core at its column index (0.0
                    # for an empty cell) so a mid-list gap can't shift later cores
                    # down, matching the index-anchored writer layout.
                    per_core_freq_mhz=[
                        _float_or_none(row.get(col)) or 0.0 for col in cpu_cols
                    ],
                ),
                network=NetworkStats(
                    rx_bytes=_int_or_none(row.get("network_rx_bytes")),
                    tx_bytes=_int_or_none(row.get("network_tx_bytes")),
                    rx_kbps=_float_or_none(row.get("network_rx_kbps")),
                    tx_kbps=_float_or_none(row.get("network_tx_kbps")),
                ),
                gpu=GpuStats(
                    busy_percent=_float_or_none(row.get("gpu_busy_percent")),
                    clock_mhz=_float_or_none(row.get("gpu_clock_mhz")),
                    temp_c=_float_or_none(row.get("gpu_temp_c")),
                ),
                thermal=ThermalStats(zones_c=thermal),
                battery=BatteryStats(
                    level_percent=_int_or_none(row.get("battery_level_percent")),
                    current_ma=_float_or_none(row.get("battery_current_ma")),
                    voltage_mv=_float_or_none(row.get("battery_voltage_mv")),
                    status=row.get("battery_status") or None,
                ),
                raw={"live_csv": row},
                metric_status=_json_cell(row.get("metric_status"), dict),
            )
        )
    return snapshots


def _handle_load_file(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Parse an MPI raw CSV export and return its report + snapshots.

    Only MPI raw CSVs (carrying an A1="MPI"/A2=<hash> header, or otherwise the
    live column layout) are accepted; the live dashboard uses this to replay a
    previously saved capture.
    """
    path = payload.get("path")
    if not path or not os.path.isfile(path):
        raise ValueError(f"file not found: {path!r}")

    if not (path.lower().endswith(".csv") and _looks_like_live_csv(path)):
        raise ValueError(f"unsupported file (expected an MPI raw .csv): {path}")

    # MPI raw exports carry an A1="MPI"/A2=<hash> header; validate it before
    # parsing. A present-but-malformed header raises; an absent one just
    # disables replay.
    mpi_hash = _validate_mpi_header(path)
    snapshots = _parse_live_csv(path)
    if not snapshots:
        raise RuntimeError("live CSV parser returned no snapshots")
    raw = _live_summary_from_snapshots(snapshots)
    _, report = summary_to_report(raw, path, "live_csv")
    return {
        "source": "live_csv",
        "report": report,
        "snapshots": snapshots,
        "raw": _strip_internal(raw),
        # When the MPI header is present, the frontend replays these snapshots
        # in the full live dashboard.
        "mpi_hash": mpi_hash,
        "replay": mpi_hash is not None,
    }


# --- Session library handlers --------------------------------------------


def _managed_raw_dir() -> str:
    """The managed session folder: ``Documents/MPI/Raw`` (home fallback).

    Auto-save writes here and the dashboard's session library reads from here."""
    migrate_session_storage()
    target, _legacy = session_roots()
    return str(target / "Raw")


def _legacy_managed_raw_dir() -> str:
    _target, legacy = session_roots()
    return str(legacy / "Raw")


def _managed_raw_dirs() -> List[str]:
    primary = _managed_raw_dir()
    legacy = _legacy_managed_raw_dir()
    return [primary] + ([legacy] if os.path.isdir(legacy) else [])


def _handle_list_saved_sessions(payload: Dict[str, Any]) -> Dict[str, Any]:
    """List every valid saved session, newest first, for the library sidebar."""
    requested = payload.get("directory")
    if requested:
        directory = str(requested)
        sessions = list_saved_sessions(directory)
        return {"directory": directory, "sessions": sessions}

    directories = _managed_raw_dirs()
    directory = directories[0]
    sessions = []
    seen: set[str] = set()
    for candidate in directories:
        for item in list_saved_sessions(candidate):
            identity = item.session_id or os.path.normcase(
                os.path.realpath(item.file_path)
            )
            if identity in seen:
                continue
            seen.add(identity)
            sessions.append(item)
    sessions.sort(key=lambda item: item.modified_at, reverse=True)
    return {"directory": directory, "sessions": sessions}


def _path_is_within(root: str, candidate: str) -> bool:
    """True when ``candidate`` resolves below ``root`` on this platform."""
    root_norm = os.path.normcase(os.path.realpath(os.path.abspath(root)))
    candidate_norm = os.path.normcase(os.path.realpath(os.path.abspath(candidate)))
    try:
        return os.path.commonpath([root_norm, candidate_norm]) == root_norm
    except ValueError:
        # Different Windows drives, or otherwise incomparable roots.
        return False


class _SessionDeleteRejected(Exception):
    """A safe, per-file rejection that should be returned to the renderer."""


def _validate_saved_session_stream(fh: Any) -> None:
    """Apply the same structural validity test as ``analysis._read_sections``.

    Deletion must validate the already-open file instead of reopening its path:
    a parent junction can be retargeted between those two operations.  Keeping
    the compact parser here avoids weakening that handle guarantee merely to
    obtain list-item metadata that deletion does not use.
    """
    metadata: Dict[str, str] = {}
    header: List[str] = []
    has_rows = False
    section: Optional[str] = None
    waiting_header = False

    for row in csv.reader(fh):
        if not row:
            section = None
            continue
        if len(row) >= 2 and row[0] == "# Section":
            section = row[1]
            waiting_header = section == "Time-series"
            continue
        if section == "Metadata":
            if len(row) >= 2 and row[0] != "key":
                metadata[row[0]] = row[1]
            continue
        if section == "Time-series":
            if waiting_header:
                header = row
                waiting_header = False
            elif header:
                has_rows = True

    if not metadata or not header or not has_rows:
        raise ValueError("not a 3-section live CSV export")


def _windows_extended_path(path: str) -> str:
    """Return an absolute Win32 extended path, including the UNC spelling."""
    absolute = os.path.abspath(path)
    if absolute.startswith("\\\\?\\"):
        return absolute
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def _windows_normalize_final_path(path: str) -> str:
    """Normalize a GetFinalPathNameByHandleW result for containment checks."""
    if path.startswith("\\\\?\\UNC\\"):
        path = "\\\\" + path[8:]
    elif path.startswith("\\\\?\\"):
        path = path[4:]
    return os.path.normcase(os.path.normpath(path))


def _delete_saved_session_windows(candidate_abs: str, root_abs: str) -> None:
    """Validate and delete one Windows file through the exact opened handle.

    The managed directory handle establishes the real root.  The candidate is
    opened with reparse-point semantics for its final component, its final path
    is checked against that root, and FILE_DISPOSITION_INFO is applied to the
    same handle after parsing the CSV from it.  A parent junction retarget can
    therefore neither redirect validation nor redirect deletion.
    """
    import ctypes
    import msvcrt
    from ctypes import wintypes

    delete_access = 0x00010000
    file_read_data = 0x00000001
    file_read_attributes = 0x00000080
    share_all = 0x00000001 | 0x00000002 | 0x00000004
    open_existing = 3
    file_attribute_directory = 0x00000010
    file_attribute_reparse_point = 0x00000400
    file_flag_backup_semantics = 0x02000000
    file_flag_open_reparse_point = 0x00200000
    file_flag_sequential_scan = 0x08000000
    file_attribute_tag_info_class = 9
    file_disposition_info_class = 4

    class _FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]

    class _FileDispositionInfo(ctypes.Structure):
        _fields_ = [("DeleteFile", wintypes.BOOL)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    get_final_path = kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    get_final_path.restype = wintypes.DWORD
    get_file_info = kernel32.GetFileInformationByHandleEx
    get_file_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    get_file_info.restype = wintypes.BOOL
    set_file_info = kernel32.SetFileInformationByHandle
    set_file_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    set_file_info.restype = wintypes.BOOL
    invalid_handle = wintypes.HANDLE(-1).value

    def open_handle(path: str, access: int, flags: int) -> int:
        handle = create_file(
            _windows_extended_path(path),
            access,
            share_all,
            None,
            open_existing,
            flags,
            None,
        )
        if handle == invalid_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        return int(handle)

    def final_path(handle: int) -> str:
        size = 260
        while True:
            buffer = ctypes.create_unicode_buffer(size)
            length = get_final_path(handle, buffer, size, 0)
            if length == 0:
                raise ctypes.WinError(ctypes.get_last_error())
            if length < size:
                return _windows_normalize_final_path(buffer.value)
            size = length + 1

    def close_native_handle(handle: int, label: str) -> None:
        if not close_handle(handle):
            error = ctypes.WinError(ctypes.get_last_error())
            # Cleanup must continue so one bad close cannot leak the other
            # handle or hide the operation's more useful primary failure.
            logger.warning("failed to close %s handle: %s", label, error)

    root_handle: Optional[int] = None
    candidate_handle: Optional[int] = None
    candidate_fd: Optional[int] = None
    try:
        root_handle = open_handle(
            root_abs,
            file_read_attributes,
            file_flag_backup_semantics,
        )
        root_final = final_path(root_handle)

        candidate_handle = open_handle(
            candidate_abs,
            file_read_data | file_read_attributes | delete_access,
            file_flag_backup_semantics
            | file_flag_open_reparse_point
            | file_flag_sequential_scan,
        )
        attributes = _FileAttributeTagInfo()
        if not get_file_info(
            candidate_handle,
            file_attribute_tag_info_class,
            ctypes.byref(attributes),
            ctypes.sizeof(attributes),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if attributes.FileAttributes & file_attribute_reparse_point:
            raise _SessionDeleteRejected("reparse-point sessions cannot be deleted")
        if attributes.FileAttributes & file_attribute_directory:
            raise _SessionDeleteRejected("path is not a file")

        candidate_final = final_path(candidate_handle)
        try:
            within_root = (
                candidate_final != root_final
                and os.path.commonpath([root_final, candidate_final]) == root_final
            )
        except ValueError:
            within_root = False
        if not within_root:
            raise _SessionDeleteRejected("path is outside the saved-session directory")

        # open_osfhandle transfers ownership to the CRT descriptor.  From this
        # point on, candidate_handle must only be closed through candidate_fd.
        candidate_fd = msvcrt.open_osfhandle(
            candidate_handle,
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
        candidate_handle = None
        with os.fdopen(candidate_fd, "r", encoding="utf-8-sig", newline="") as fh:
            candidate_fd = None
            _validate_saved_session_stream(fh)
            disposition = _FileDispositionInfo(True)
            native_handle = msvcrt.get_osfhandle(fh.fileno())
            if not set_file_info(
                native_handle,
                file_disposition_info_class,
                ctypes.byref(disposition),
                ctypes.sizeof(disposition),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
        # The fd close above completes deletion after disposition was set.
    finally:
        if candidate_fd is not None:
            try:
                os.close(candidate_fd)
            except OSError as exc:
                logger.warning("failed to close saved-session descriptor: %s", exc)
        if candidate_handle is not None:
            close_native_handle(candidate_handle, "saved-session")
        if root_handle is not None:
            close_native_handle(root_handle, "managed-root")


def _delete_saved_session_posix(candidate_abs: str, root_abs: str) -> None:
    """Best-effort portable equivalent using pinned dir_fds and O_NOFOLLOW."""
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise OSError("secure saved-session deletion is unavailable on this platform")
    if os.open not in os.supports_dir_fd or os.unlink not in os.supports_dir_fd:
        raise OSError("secure saved-session deletion is unavailable on this platform")

    relative = os.path.relpath(candidate_abs, root_abs)
    parts = [part for part in relative.split(os.sep) if part not in ("", ".")]
    if not parts or any(part == os.pardir for part in parts):
        raise _SessionDeleteRejected("path is outside the saved-session directory")

    root_fd = os.open(root_abs, os.O_RDONLY | os.O_DIRECTORY)
    parent_fd = os.dup(root_fd)
    file_fd: Optional[int] = None
    try:
        for component in parts[:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            os.close(parent_fd)
            parent_fd = next_fd

        name = parts[-1]
        file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        opened = os.fstat(file_fd)
        if not stat.S_ISREG(opened.st_mode):
            raise _SessionDeleteRejected("path is not a file")
        with os.fdopen(os.dup(file_fd), "r", encoding="utf-8-sig", newline="") as fh:
            _validate_saved_session_stream(fh)

        # Pinning every parent and refusing symlinks prevents an unlink from
        # escaping the managed tree.  The identity check also detects a final
        # component swap before unlinkat on platforms without handle unlink.
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise _SessionDeleteRejected("saved session changed during deletion")
        os.unlink(name, dir_fd=parent_fd)
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(parent_fd)
        os.close(root_fd)


def _delete_one_saved_session(path: Any, managed_dir: str) -> Dict[str, Any]:
    """Delete one regular CSV below the managed session root, without raising.

    Both the lexical path and its resolved target must remain inside the managed
    root.  Final-component symlinks are rejected instead of followed, preventing
    a library entry from becoming a deletion primitive for an arbitrary target.
    Per-file errors let a multi-delete finish safely when another process already
    removed one of the selected files.
    """
    value = path.strip() if isinstance(path, str) else ""
    result: Dict[str, Any] = {
        "path": path if isinstance(path, str) else "",
        "file_name": os.path.basename(value) if value else "",
    }
    if not value:
        return {**result, "status": "failed", "reason": "path is required"}

    root_abs = os.path.abspath(managed_dir)
    candidate_abs = os.path.abspath(value)
    if not _path_is_within(root_abs, candidate_abs):
        return {**result, "status": "failed", "reason": "path is outside the saved-session directory"}
    if not candidate_abs.lower().endswith(".csv"):
        return {**result, "status": "failed", "reason": "saved session must be a .csv file"}
    if os.path.islink(candidate_abs):
        return {**result, "status": "failed", "reason": "symbolic-link sessions cannot be deleted"}
    if not os.path.exists(candidate_abs):
        return {**result, "status": "failed", "reason": "file not found"}
    if not os.path.isfile(candidate_abs):
        return {**result, "status": "failed", "reason": "path is not a file"}
    try:
        if sys.platform == "win32":
            _delete_saved_session_windows(candidate_abs, root_abs)
        else:
            _delete_saved_session_posix(candidate_abs, root_abs)
    except _SessionDeleteRejected as exc:
        return {**result, "status": "failed", "reason": str(exc)}
    except (ValueError, UnicodeError, csv.Error) as exc:
        return {**result, "status": "failed", "reason": f"not a saved session CSV: {exc}"}
    except OSError as exc:
        return {**result, "status": "failed", "reason": str(exc)}
    return {**result, "path": candidate_abs, "status": "deleted"}


def _handle_delete_saved_sessions(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Delete selected managed sessions with per-file success/failure results.

    The root is intentionally not caller-controlled.  Tests can monkeypatch
    ``_managed_raw_dir``; production renderer input can never widen deletion
    authority by supplying another directory in the IPC payload.
    """
    paths = payload.get("paths")
    if not isinstance(paths, list) or not paths:
        raise ValueError("paths must be a non-empty list")
    managed_dirs = _managed_raw_dirs()
    managed_dir = managed_dirs[0]
    results: List[Dict[str, Any]] = []
    for path in paths:
        try:
            value = path if isinstance(path, str) else ""
            matching_root = next(
                (
                    root
                    for root in managed_dirs
                    if value and _path_is_within(root, value)
                ),
                managed_dir,
            )
            results.append(_delete_one_saved_session(path, matching_root))
        except Exception as exc:  # defensive: one malformed path must not abort the batch
            value = path if isinstance(path, str) else ""
            results.append(
                {
                    "path": value,
                    "file_name": os.path.basename(value.replace("\x00", "")) if value else "",
                    "status": "failed",
                    "reason": str(exc) or type(exc).__name__,
                }
            )
    deleted = sum(1 for result in results if result["status"] == "deleted")
    if deleted:
        _emit({"event": "session_library_changed"})
    return {
        "directory": managed_dir,
        "deleted": deleted,
        "failed": len(results) - deleted,
        "results": results,
    }


def _reserve_dest_path(dest_dir: str, filename: str) -> str:
    """Atomically reserve a unique destination by creating the file with
    O_CREAT|O_EXCL, so two concurrent imports can never resolve to the same name
    and silently overwrite each other (closes the exists()-then-copy TOCTOU). The
    caller then overwrites the reserved (empty) file with the real content."""
    base, ext = os.path.splitext(filename or "session.csv")
    i = 0
    while True:
        name = f"{base}{ext}" if i == 0 else f"{base}_{i}{ext}"
        candidate = os.path.join(dest_dir, name)
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(fd)
            return candidate
        except FileExistsError:
            i += 1


def _import_one_session(src: Any, dest_dir: str, known: Dict[str, str]) -> Dict[str, Any]:
    """Validate + de-dupe + copy one CSV into the managed folder. Never raises —
    a per-file failure is reported so a batch import degrades to partial success."""
    name = os.path.basename(str(src)) if src else ""
    try:
        if not src or not os.path.isfile(str(src)):
            return {"source": src, "file_name": name, "status": "failed", "reason": "file not found"}
        if not str(src).lower().endswith(".csv"):
            return {"source": src, "file_name": name, "status": "failed", "reason": "not a .csv file"}
        try:
            key = session_dedup_key(str(src))
        except (TypeError, ValueError, OSError) as exc:
            return {"source": src, "file_name": name, "status": "failed", "reason": f"invalid session CSV: {exc}"}
        if key in known:
            return {
                "source": src,
                "file_name": name,
                "status": "duplicate",
                "reason": "already imported",
                "duplicate_of": known[key],
            }
        if os.path.abspath(os.path.dirname(str(src))) == os.path.abspath(dest_dir):
            dest = str(src)  # already in the managed folder — register in place
        else:
            dest = _reserve_dest_path(dest_dir, name)
            shutil.copy2(str(src), dest)
        known[key] = os.path.basename(dest)
        item = read_list_item(dest)
        return {
            "source": src,
            "file_name": os.path.basename(dest),
            "status": "imported",
            "session_id": item.session_id,
            "path": dest,
        }
    except Exception as exc:  # defensive: never fail the whole batch
        return {"source": src, "file_name": name, "status": "failed", "reason": str(exc)}


def _handle_import_session_files(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Import external CSVs into the managed folder with per-file results.

    New v2 files de-dupe by ``session.id``; legacy/header-less files by a
    header-excluded content fingerprint. Returns a per-file status list so the UI
    can show partial success (imported / duplicate / failed)."""
    paths = payload.get("paths")
    if not isinstance(paths, list) or not paths:
        raise ValueError("paths must be a non-empty list")
    requested_directory = payload.get("directory")
    dest_dir = requested_directory or _managed_raw_dir()
    os.makedirs(dest_dir, exist_ok=True)

    known: Dict[str, str] = {}
    known_directories = (
        [str(dest_dir)] if requested_directory else _managed_raw_dirs()
    )
    for known_directory in known_directories:
        for it in list_saved_sessions(known_directory):
            try:
                known[session_dedup_key(it.file_path)] = it.file_name
            except (ValueError, OSError):
                continue

    results = [_import_one_session(src, dest_dir, known) for src in paths]
    imported = sum(1 for r in results if r["status"] == "imported")
    duplicates = sum(1 for r in results if r["status"] == "duplicate")
    failed = sum(1 for r in results if r["status"] == "failed")
    if imported:
        # Notify other views only after an imported file is available to list.
        _emit({"event": "session_library_changed"})
    return {
        "directory": dest_dir,
        "imported": imported,
        "duplicates": duplicates,
        "failed": failed,
        "results": results,
    }


def _handle_load_session_analyses(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Full canonical analysis for up to 4 selected sessions.

    Extra paths beyond 4 are ignored (reported via ``dropped``). A per-file parse
    error is collected in ``errors`` rather than failing the whole request."""
    paths = payload.get("paths")
    if not isinstance(paths, list) or not paths:
        raise ValueError("paths must be a non-empty list")
    selected = paths[:4]
    ranges_raw = payload.get("ranges")
    ranges_by_path: Dict[str, Dict[str, Any]] = {}
    if isinstance(ranges_raw, list):
        for value in ranges_raw:
            if isinstance(value, dict) and value.get("path"):
                ranges_by_path[str(value["path"])] = value
    analyses: List[Any] = []
    errors: List[Dict[str, str]] = []
    for item in selected:
        if isinstance(item, dict):
            p = item.get("path")
            start_sec = item.get("start_sec")
            end_sec = item.get("end_sec")
        else:
            p = item
            range_value = ranges_by_path.get(str(p), {})
            start_sec = range_value.get("start_sec")
            end_sec = range_value.get("end_sec")
        if not p:
            errors.append({"path": "", "error": "path is required"})
            continue
        try:
            analyses.append(analyze_session(str(p), start_sec=start_sec, end_sec=end_sec))
        except (ValueError, OSError) as exc:
            errors.append({"path": str(p), "error": str(exc)})
    return {"analyses": analyses, "errors": errors, "dropped": max(0, len(paths) - 4)}


# --- Live capture handlers -----------------------------------------------


def _payload_platform(payload: Dict[str, Any], allow_all: bool = False) -> str:
    platform = str(payload.get("platform") or "android").lower()
    allowed = {"android", "ios"}
    if allow_all:
        allowed.add("all")
    if platform not in allowed:
        raise ValueError(f"unsupported platform: {platform}")
    return platform


def _backend_for_platform(platform: str) -> object:
    if platform == "android":
        return _android_backend
    if platform == "ios":
        return _ios_backend
    raise ValueError(f"unsupported platform: {platform}")


def _physical_device_key(
    platform: str,
    serial: str,
    backend: object,
    device: Optional[DeviceInfo] = None,
) -> str:
    """Canonical reservation key for a physical phone.

    iOS transport identifiers are UDIDs already.  Android Wi-Fi transports
    use ``ip:port``, so resolve the cached ``ro.serialno``/``hw_serial`` value
    to collide with the same phone's USB transport.
    """
    physical = serial
    if platform == "android":
        if device is not None:
            physical = device.hw_serial or device.serial
        else:
            resolver = getattr(backend, "physical_device_id", None)
            if callable(resolver):
                physical = str(resolver(serial) or serial)
    return f"{platform}:{str(physical).strip().casefold()}"


def _device_busy_locked(device_key: str) -> bool:
    return (
        device_key in _reserved_serials
        or any(
            context.device_key == device_key
            for context in _active_captures.values()
        )
        or any(
            not context.ended and context.device_key == device_key
            for context in _active_temperature_guards.values()
        )
        or any(
            not context.ended and context.device_key == device_key
            for context in _active_temperature_previews.values()
        )
    )


def _device_busy_error(serial: str) -> ValueError:
    return ValueError(
        f"이 기기에서 이미 측정 또는 온도 대기가 진행 중입니다 (serial={serial}). "
        "다른 창이나 USB/Wi-Fi 연결에서 같은 물리 기기를 동시에 사용할 수 없습니다. "
        "진행 중인 작업을 멈춘 뒤 다시 시도하세요."
    )


def _handle_list_devices(payload: Dict[str, Any]) -> Dict[str, Any]:
    platform = _payload_platform(payload, allow_all=True)
    if platform != "all":
        backend = _backend_for_platform(platform)
        return {"platform": platform, "devices": backend.list_devices()}  # type: ignore[attr-defined]

    # Android (adb) and iOS (xcrun/pymobiledevice3) discovery are independent.
    # Running them serially made the unified selector pay the sum of both cold
    # starts. Resolve both at once, but consume the futures in the stable
    # Android-then-iOS order so the UI's default ordering does not change.
    platforms = ("android", "ios")
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="device-list") as pool:
        futures = {
            plat: pool.submit(
                _backend_for_platform(plat).list_devices  # type: ignore[attr-defined]
            )
            for plat in platforms
        }
        devices: List[DeviceInfo] = []
        errors: Dict[str, str] = {}
        for plat in platforms:
            try:
                devices.extend(futures[plat].result())
            except Exception as exc:
                errors[plat] = f"{type(exc).__name__}: {exc}"
    result: Dict[str, Any] = {"platform": "all", "devices": devices}
    if errors:
        result["errors"] = errors
    return result


def _handle_play_memory_qa_sample(payload: Dict[str, Any]) -> Dict[str, Any]:
    """One bounded QA sample. Reserve the selected phone against other capture.

    This command does not start an app, change its state or upload QA data.
    """
    from . import memory_qa

    serial = payload.get("serial")
    package = payload.get("package")
    category = payload.get("category")
    state = payload.get("state")
    form_factor = payload.get("form_factor", "unknown")
    index = payload.get("sample_index")
    bitmap = payload.get("bitmap", False)
    if not memory_qa.valid_serial(serial):
        raise ValueError("select one authorized ADB device")
    if not isinstance(package, str) or len(package) > 255 or not memory_qa.PACKAGE.fullmatch(package):
        raise ValueError("select one exact Android package")
    if category not in ("app", "game") or state not in (
            "auto", "unknown", "foreground", "user_perceived_services", "background", "cached"):
        raise ValueError("invalid Play memory QA classification")
    if not isinstance(form_factor, str) or form_factor not in memory_qa.FORM_FACTORS:
        raise ValueError("invalid Play memory QA form factor")
    if type(index) is not int or not 0 <= index < 200 or type(bitmap) is not bool:
        raise ValueError("invalid Play memory QA sample index")
    device_key = _physical_device_key("android", serial, _android_backend)
    with _captures_lock:
        if _device_busy_locked(device_key):
            raise ValueError("device already sampling or tracing")
        _reserved_serials.add(device_key)
    try:
        adb = memory_qa.Adb("adb", serial)
        if adb.get_state() != "device":
            raise ValueError("select one authorized ADB device")
        try:
            total = adb.mem_total()
        except memory_qa.AdbError:
            total = None
        try:
            sdk = adb.sdk_int()
        except memory_qa.AdbError:
            sdk = None
        base = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "sample_index": index, "device": serial, "package": package, "category": category,
            "state": state, "state_source": "unknown" if state in ("auto", "unknown") else "operator_label",
            "mem_total_kib": total if total and total > 0 else None, "sdk_int": sdk, "form_factor": form_factor,
        }
        rows = memory_qa.read_sample(adb, package, base)
        # This is separately timestamped diagnostics, not the RSS sample's composition.
        bitmap_rows = memory_qa.read_bitmap_sample(adb, package, base, limit=1) if bitmap else []
        return {"rows": rows, "bitmap_rows": bitmap_rows}
    finally:
        with _captures_lock:
            _reserved_serials.discard(device_key)


def _handle_play_memory_qa_summary(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Group an in-memory QA window; does not use ADB, cloud or stored sessions."""
    from . import memory_qa

    rows = payload.get("rows")
    if (not isinstance(rows, list) or len(rows) > 200 * (memory_qa.MAX_PROCESSES + 1)
            or any(not isinstance(row, dict) or set(row) != set(memory_qa.COLUMNS) for row in rows)):
        raise ValueError("invalid Play memory QA window")
    return {"groups": memory_qa.summarize(rows)}


def _handle_ios_preflight(_: Dict[str, Any]) -> Dict[str, Any]:
    """Readiness check for iOS capture on this host. On macOS the Xcode tools
    are assumed; elsewhere reports whether the pymobiledevice3 CLI resolves
    and whether an in-app install could fix a missing one."""
    if sys.platform == "darwin":
        return {"platform": sys.platform, "tool": "xcrun", "available": True}
    from .ios import pmd3

    return {"platform": sys.platform, "tool": "pymobiledevice3", **pmd3.preflight()}


def _handle_ios_install_tool(_: Dict[str, Any]) -> Dict[str, Any]:
    """One-click pip install of pymobiledevice3 (Windows/Linux hosts)."""
    if sys.platform == "darwin":
        raise RuntimeError("macOS uses Xcode tools; nothing to install")
    from .ios import pmd3

    return {"platform": sys.platform, "tool": "pymobiledevice3", **pmd3.install_cli()}


def _handle_ios_temperature_guard_preflight(_: Dict[str, Any]) -> Dict[str, Any]:
    """Guard-specific readiness: pymobiledevice3 9.x on every host.

    Ordinary macOS capture intentionally keeps reporting/using xcrun.  The
    temperature guard is different because its prepared Diagnostics+DVT path
    is implemented by the pymobiledevice3 sidecar even on macOS.
    """
    from .ios import pmd3

    return {
        "platform": sys.platform,
        "tool": "pymobiledevice3",
        **pmd3.guard_preflight(),
    }


def _handle_ios_temperature_guard_install_tool(
    _: Dict[str, Any]
) -> Dict[str, Any]:
    """Install/repair the guard's supported pymobiledevice3 9.x dependency."""
    from .ios import pmd3

    pmd3.install_cli()
    return {
        "platform": sys.platform,
        "tool": "pymobiledevice3",
        **pmd3.guard_preflight(),
    }


def _handle_prepare_wireless(payload: Dict[str, Any]) -> Dict[str, Any]:
    platform = _payload_platform(payload)
    serial = payload.get("serial")
    if not serial:
        raise ValueError("serial required")
    port = int(payload.get("port") or 5555)
    if platform == "ios":
        return _ios_backend.prepare_wireless(str(serial))
    return _android_backend.prepare_wireless(str(serial), port=port)


def _handle_list_packages(payload: Dict[str, Any]) -> Dict[str, Any]:
    platform = _payload_platform(payload)
    serial = payload.get("serial")
    if not serial:
        raise ValueError("serial required")
    user_only = bool(payload.get("user_only", True))
    backend = _backend_for_platform(platform)
    packages = backend.list_packages(serial, user_only=user_only)  # type: ignore[attr-defined]
    return {"platform": platform, "serial": serial, "packages": packages}


def _handle_fetch_app_icon(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return the launcher PNG of an installed APK as base64.

    Frontend should request this lazily for each package in the dropdown.
    Backed by a disk cache, so repeat sessions skip adb entirely.
    """
    platform = _payload_platform(payload)
    serial = payload.get("serial")
    bundle_id = payload.get("package")
    apk_path = payload.get("apk_path")
    if not serial or not bundle_id:
        raise ValueError("serial and package required")

    if platform == "ios":
        data = _ios_backend.fetch_app_icons(serial, [str(bundle_id)]).get(str(bundle_id))
    else:
        data = _android_backend.fetch_app_icon_bytes(
            serial, apk_path, bundle_id=str(bundle_id)
        )
    if not data:
        return {"serial": serial, "package": bundle_id, "icon_b64": None}
    return {
        "serial": serial,
        "package": bundle_id,
        "icon_b64": base64.b64encode(data).decode("ascii"),
        "mime": "image/png",
    }


def _handle_fetch_app_icons(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Bulk variant of ``fetch_app_icon``. Takes a list of {package, apk_path}
    entries and returns ``{bundle_id: icon_b64 | null}``. Saves the per-call
    IPC overhead the frontend would otherwise pay 50× when opening a long
    package dropdown, and runs the adb extracts in parallel internally.
    """
    platform = _payload_platform(payload)
    serial = payload.get("serial")
    requests = payload.get("requests")
    if not serial:
        raise ValueError("serial required")
    if not isinstance(requests, list):
        raise ValueError("requests must be a list")

    icons: Dict[str, Optional[str]] = {}
    if not requests:
        return {"serial": serial, "icons": icons}

    if platform == "ios":
        bundle_ids: List[str] = []
        for r in requests:
            if isinstance(r, dict):
                bid = str(r.get("package") or "")
                if bid and bid not in icons:
                    icons[bid] = None
                    bundle_ids.append(bid)
        fetched = _ios_backend.fetch_app_icons(serial, bundle_ids)
        for bid, data in fetched.items():
            icons[bid] = base64.b64encode(data).decode("ascii") if data else None
        return {"serial": serial, "icons": icons}

    # Inner pool runs in parallel with the outer dispatcher pool.
    # Keep metadata extraction deliberately below the API worker count. These
    # calls share one adb transport with capture startup; a 12-icon + 12-label
    # burst could otherwise starve the latency-sensitive start_capture probes.
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="icon-bulk") as pool:
        futures = {}
        for r in requests:
            if not isinstance(r, dict):
                continue
            bid = str(r.get("package") or "")
            if not bid or bid in futures.values():
                continue
            apk = r.get("apk_path")
            fut = pool.submit(
                _android_backend.fetch_app_icon_bytes, serial, apk, bid
            )
            futures[fut] = bid
        for fut in as_completed(futures):
            bid = futures[fut]
            try:
                data = fut.result()
            except Exception:
                icons[bid] = None
                continue
            icons[bid] = base64.b64encode(data).decode("ascii") if data else None

    return {"serial": serial, "icons": icons}


def _handle_fetch_app_label(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return the friendly label of one installed app, or ``null``.

    Android resolves it from the APK (binary manifest + resources.arsc); other
    platforms already carry the label on ``list_packages`` output, so this
    returns ``null`` for them.
    """
    platform = _payload_platform(payload)
    serial = payload.get("serial")
    bundle_id = payload.get("package")
    apk_path = payload.get("apk_path")
    if not serial or not bundle_id:
        raise ValueError("serial and package required")
    if platform != "android":
        return {"serial": serial, "package": bundle_id, "label": None}
    label = _android_backend.fetch_app_label(serial, apk_path, bundle_id=str(bundle_id))
    return {"serial": serial, "package": bundle_id, "label": label or None}


def _handle_fetch_app_labels(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Bulk variant of ``fetch_app_label``. Takes a list of {package, apk_path}
    entries and returns ``{bundle_id: label | null}``. The package dropdown
    coalesces visibility-triggered requests into one of these, exactly as it does
    for ``fetch_app_icons``; the sidecar resolves the APK labels in parallel.

    Only Android needs this — iOS/devicectl already return display names from
    ``list_packages`` — so non-Android platforms get an all-``null`` map.
    """
    platform = _payload_platform(payload)
    serial = payload.get("serial")
    requests = payload.get("requests")
    if not serial:
        raise ValueError("serial required")
    if not isinstance(requests, list):
        raise ValueError("requests must be a list")

    labels: Dict[str, Optional[str]] = {}
    if not requests or platform != "android":
        for r in requests if isinstance(requests, list) else []:
            if isinstance(r, dict):
                bid = str(r.get("package") or "")
                if bid:
                    labels[bid] = None
        return {"serial": serial, "labels": labels}

    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="label-bulk") as pool:
        futures = {}
        for r in requests:
            if not isinstance(r, dict):
                continue
            bid = str(r.get("package") or "")
            if not bid or bid in futures.values():
                continue
            apk = r.get("apk_path")
            fut = pool.submit(_android_backend.fetch_app_label, serial, apk, bid)
            futures[fut] = bid
        for fut in as_completed(futures):
            bid = futures[fut]
            try:
                labels[bid] = fut.result() or None
            except Exception:
                labels[bid] = None

    return {"serial": serial, "labels": labels}


def _parse_auto_stop_after_sec(payload: Dict[str, Any]) -> Optional[float]:
    """Validate the optional API-owned live-capture deadline."""
    if "auto_stop_after_sec" not in payload:
        return None
    raw = payload.get("auto_stop_after_sec")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError("auto_stop_after_sec must be a finite number between 1 and 3600")
    value = float(raw)
    if (
        not math.isfinite(value)
        or value < AUTO_STOP_MIN_SEC
        or value > AUTO_STOP_MAX_SEC
    ):
        raise ValueError("auto_stop_after_sec must be between 1 and 3600")
    return value


# --- On-device HUD overlay (Android companion) ---------------------------
#
# Everything here is best-effort by construction: the overlay is a convenience on
# the phone's screen, and no failure of it may cost the user a measurement.

# The mobile dashboard labels this reading as battery temperature.
_OVERLAY_TEMPERATURE_ZONES = ("battery", "thermal:battery")


def _parse_overlay_metrics(raw: Any) -> List[str]:
    """Validate a metric-id list, returning it in the companion's display order."""
    from .android.companion import OVERLAY_METRICS

    if raw is None:
        return list(OVERLAY_METRICS)
    if not isinstance(raw, list) or any(not isinstance(m, str) for m in raw):
        raise ValueError("overlay metrics must be an array of strings")
    wanted = {m.strip().lower() for m in raw}
    return [m for m in OVERLAY_METRICS if m in wanted]


def _parse_overlay_config(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Validate the optional ``overlay`` key. Absent or null means "no HUD"."""
    raw = payload.get("overlay")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("overlay must be an object")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("overlay.enabled must be a boolean")
    return {"enabled": enabled, "metrics": _parse_overlay_metrics(raw.get("metrics"))}


def _open_overlay_session(
    platform: str,
    serial: str,
    bundle_id: str,
    config: Optional[Dict[str, Any]],
    allow_cold_start: bool = False,
    session_id: Optional[str] = None,
    label: Optional[str] = None,
) -> Optional[object]:
    """Open the mobile dashboard stream and optional HUD, or return None.

    Android only — an iOS device hosts nothing of ours and has no draw-over-apps
    API. Every failure is swallowed: a missing, old or unreachable companion must
    leave the capture itself untouched."""
    if platform != "android":
        return None
    config = config or {"enabled": False}
    from .android import companion

    try:
        return companion.open_overlay_session(
            serial,
            package=bundle_id,
            metrics=config.get("metrics"),
            enabled=bool(config.get("enabled")),
            allow_cold_start=allow_cold_start,
            session_id=session_id,
            label=label,
        )
    except Exception as exc:
        logger.info("device overlay unavailable on %s: %s", serial, exc)
        return None


def _publish_overlay(
    capture_id: str, context: _CaptureContext, overlay: object
) -> bool:
    """Hand a freshly opened HUD session to its capture, or refuse it.

    Refuses when the capture ended while we were connecting — the pump epilogue
    has already read and cleared the slot, so nothing would ever tear this
    session down again — and when a session is somehow already parked there.
    Callers must close what is refused: dropping a live session leaks its socket,
    its daemon pusher thread and its ``adb forward`` rule for the life of the
    sidecar. Must be called under ``context.overlay_lock``."""
    with _captures_lock:
        if _active_captures.get(capture_id) is not context or context.overlay is not None:
            return False
        context.overlay = overlay
        return True


def _close_overlay(overlay: Optional[object]) -> None:
    if overlay is None:
        return
    try:
        overlay.close()  # type: ignore[attr-defined]
    except Exception:
        logger.warning("device overlay close failed", exc_info=True)


def _overlay_values(
    snap: Any, elapsed_sec: float = 0.0
) -> Dict[str, Optional[float]]:
    """Project a snapshot onto the mobile dashboard and HUD values.

    CPU and network totals are device-wide. App CPU preserves its summed-core
    scale, while app memory prefers PSS and falls back to RSS when unavailable.
    """
    cpu = getattr(snap, "cpu", None)
    # HUD CPU is deliberately the device-wide total, matching the desktop's
    # primary CPU card. App CPU (raw/normalized) is a different metric and can
    # otherwise make an idle device appear saturated on multi-core hardware.
    cpu_value = getattr(cpu, "total_percent", None) if cpu is not None else None
    thermal = getattr(snap, "thermal", None)
    zones = getattr(thermal, "zones_c", None) or {}
    frames = getattr(snap, "frames", None)
    gpu = getattr(snap, "gpu", None)
    memory = getattr(snap, "memory", None)
    battery = getattr(snap, "battery", None)
    network = getattr(snap, "network", None)

    def numeric(value: Any) -> Optional[float]:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value) if math.isfinite(value) else None

    def memory_mb(value: Any) -> Optional[float]:
        kb = numeric(value)
        return kb / 1024.0 if kb is not None and kb > 0 else None

    app_memory = memory_mb(getattr(memory, "pss_total_kb", None))
    if app_memory is None:
        app_memory = memory_mb(getattr(memory, "rss_total_kb", None))
    current = numeric(getattr(battery, "current_ma", None))
    voltage = numeric(getattr(battery, "voltage_mv", None))
    frame_count = numeric(getattr(frames, "total_frames", None))
    return {
        "elapsed": max(0.0, elapsed_sec),
        "fps": numeric(getattr(frames, "fps_estimate", None)),
        "cpu": numeric(cpu_value),
        "cpu_app": numeric(getattr(cpu, "cpu_percent", None)),
        "gpu": numeric(getattr(gpu, "busy_percent", None)),
        "temp": next(
            (numeric(zones[z]) for z in _OVERLAY_TEMPERATURE_ZONES if numeric(zones.get(z)) is not None),
            None,
        ),
        "memory": app_memory,
        "memory_system": memory_mb(getattr(memory, "sys_used_kb", None)),
        "battery": numeric(getattr(battery, "level_percent", None)),
        "power": abs(current * voltage) / 1000.0 if current is not None and voltage is not None else None,
        # FrameStats defaults to zero when no frame source is available. Without
        # a measured denominator that zero is missing data, not a smooth run.
        "jank": numeric(getattr(frames, "jank_percent", None)) if frame_count is not None and frame_count > 0 else None,
        "rx": numeric(getattr(network, "rx_kbps", None)),
        "tx": numeric(getattr(network, "tx_kbps", None)),
    }


def _push_overlay_sample(capture_id: str, snap: Any) -> None:
    """Mirror one sample onto the device HUD. Never blocks and never raises — the
    desktop stream sets the cadence and the phone must not be able to stretch it."""
    with _captures_lock:
        context = _active_captures.get(capture_id)
        now = time.monotonic()
        if context is not None and context.measurement_started_at is None:
            context.measurement_started_at = now
        elapsed_sec = (
            getattr(snap, "elapsed_sec", None)
            if getattr(snap, "elapsed_sec", None) is not None
            else max(0.0, now - context.measurement_started_at - context.paused_total_sec)
            if context is not None and context.measurement_started_at is not None
            else 0.0
        )
        overlay = context.overlay if context is not None else None
    if overlay is None:
        return
    try:
        overlay.update(  # type: ignore[attr-defined]
            _overlay_values(snap, elapsed_sec)
        )
    except Exception:
        logger.debug("device overlay push failed for %s", capture_id, exc_info=True)


def _terminate_capture_target(context: _CaptureContext) -> None:
    """Request target-app termination for an automatic stop only."""
    backend = context.backend
    package = getattr(context.session, "package", None)
    bundle_id = getattr(package, "bundle_id", None)
    terminator = getattr(backend, "terminate_app", None)
    if not callable(terminator):
        raise RuntimeError("capture backend does not support target app termination")
    if not bundle_id:
        raise RuntimeError("capture session has no target app bundle id")
    if context.platform == "ios":
        terminator(context.serial, package, session=context.session)
    else:
        terminator(context.serial, str(bundle_id))


def _auto_stop_capture(capture_id: str) -> None:
    """Claim an elapsed auto-stop exactly once, then terminate its target app."""
    context: Optional[_CaptureContext] = None
    rearm_delay: Optional[float] = None
    with _captures_lock:
        current = _active_captures.get(capture_id)
        if current is None or current.stop_reason is not None:
            return
        if current.pause_started_at is not None:
            return
        deadline = current.auto_stop_deadline
        if deadline is None:
            return
        remaining = deadline - time.monotonic()
        if remaining > 0:
            # Timers normally do not fire early, but use the monotonic deadline
            # as the source of truth if a clock/scheduler edge case does occur.
            rearm_delay = remaining
        else:
            current.stop_reason = "auto_stop"
            current.stop.set()
            context = current

    if rearm_delay is not None:
        timer = threading.Timer(rearm_delay, _auto_stop_capture, args=(capture_id,))
        timer.daemon = True
        with _captures_lock:
            current = _active_captures.get(capture_id)
            if current is None or current.stop_reason is not None:
                return
            current.auto_stop_timer = timer
        timer.start()
        return
    if context is None:
        return

    try:
        # Closing first unblocks a long-running sample.  The pump waits for this
        # attempt before publishing capture_ended, preserving the contract that
        # automatic stop ends the target app with the measurement.
        _close_capture_context(context)
        _terminate_capture_target(context)
    except Exception as exc:
        logger.warning("auto-stop target termination for %s failed: %s", capture_id, exc)
        context.target_app_termination_error = f"{type(exc).__name__}: {exc}"
    finally:
        context.auto_stop_termination_done.set()


def _arm_capture_auto_stop(capture_id: str) -> None:
    """Start the deadline after (not before) the first snapshot event."""
    timer: Optional[threading.Timer] = None
    with _captures_lock:
        context = _active_captures.get(capture_id)
        if (
            context is None
            or context.stop_reason is not None
            or context.pause_started_at is not None
            or context.auto_stop_after_sec is None
            or context.auto_stop_deadline is not None
        ):
            return
        started_at = context.measurement_started_at or time.monotonic()
        context.measurement_started_at = started_at
        context.auto_stop_deadline = started_at + context.paused_total_sec + context.auto_stop_after_sec
        delay = max(0.0, context.auto_stop_deadline - time.monotonic())
        timer = threading.Timer(
            delay, _auto_stop_capture, args=(capture_id,)
        )
        timer.daemon = True
        context.auto_stop_timer = timer
    timer.start()


def _capture_pump(
    capture_id: str,
    session: object,
    stop: threading.Event,
    duration_sec: float,
) -> None:
    """Worker thread: pull snapshots from session and emit them as events."""
    seq = 0
    try:
        while not stop.is_set():
            with _captures_lock:
                context = _active_captures.get(capture_id)
                generation = context.pause_generation if context else 0
                was_paused = context is not None and context.pause_started_at is not None
            attempt_started = time.monotonic()
            try:
                snap = session.sample(duration_sec)  # type: ignore[attr-defined]
            except (AppExitedError, AppBackgroundedError) as exc:
                with _captures_lock:
                    current = _active_captures.get(capture_id)
                    paused = current is not None and current.pause_started_at is not None
                if paused:
                    stop.wait(timeout=duration_sec)
                    continue
                # The measured app died — surface the notice and END the capture
                # (fall through to the finally/capture_ended path) instead of
                # sampling a dead process forever. Collected data is preserved:
                # the frontend auto-saves on capture_ended.
                target_end_won = False
                with _captures_lock:
                    current = _active_captures.get(capture_id)
                    if current is not None and current.stop_reason is None:
                        # Claim this natural target-app ending before a deadline
                        # Timer can reinterpret the same instant as auto-stop.
                        current.stop_reason = "target_ended"
                        if current.auto_stop_timer is not None:
                            current.auto_stop_timer.cancel()
                        target_end_won = True
                if target_end_won:
                    logger.warning("capture %s: %s", capture_id, exc)
                    _emit(
                        {
                            "event": "capture_error",
                            "capture_id": capture_id,
                            "seq": seq,
                            "error": str(exc),
                        }
                    )
                break
            except Exception as exc:
                if stop.is_set():
                    # Auto/manual teardown can make a blocked transport raise a
                    # generic I/O error; it is not a second capture failure.
                    break
                logger.exception("capture %s sample failed", capture_id)
                _emit(
                    {
                        "event": "capture_error",
                        "capture_id": capture_id,
                        "seq": seq,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                # A timed-out read can already have used the entire interval.
                # Throttle fast failures without skipping the next viable tick.
                stop.wait(timeout=max(0.0, duration_sec - (time.monotonic() - attempt_started)))
                continue
            if stop.is_set():
                break
            with _captures_lock:
                context = _active_captures.get(capture_id)
                if context is not None:
                    # Keep device counters fresh while paused, but discard those
                    # samples and any sample straddling a pause transition.
                    if was_paused or context.pause_started_at is not None or context.pause_generation != generation:
                        continue
                    if context.measurement_started_at is None:
                        context.measurement_started_at = time.monotonic()
                        snap.elapsed_sec = 0.0
                    else:
                        snap.elapsed_sec = round(_capture_elapsed(context), 6)
                    snap.pause_used = context.pause_generation > 0
                _emit({"event": "snapshot", "capture_id": capture_id, "seq": seq, "data": snap})
            # After the desktop stream, never before it: the renderer's charts
            # must not wait on a phone we only talk to for convenience.
            _push_overlay_sample(capture_id, snap)
            seq += 1
            if seq == 1:
                _arm_capture_auto_stop(capture_id)
    finally:
        # Release any session-held resources (e.g. the iOS metrics streamer
        # subprocess) so they never outlive the capture.
        with _captures_lock:
            capture_context = _active_captures.get(capture_id)
        if capture_context is not None:
            if capture_context.stop_reason == "auto_stop":
                capture_context.auto_stop_termination_done.wait()
            _close_capture_context(capture_context)
        else:
            _close_session(session)

    guard_id: Optional[str] = None
    target_app_termination_error: Optional[str] = None
    stop_reason: Optional[str] = None
    auto_stop_after_sec: Optional[float] = None
    elapsed_sec: Optional[float] = None
    pause_used = False
    overlay: Optional[object] = None
    with _captures_lock:
        context = _active_captures.pop(capture_id, None)
        if context is not None:
            elapsed_sec = _capture_elapsed(context)
            pause_used = context.pause_generation > 0
            guard_id = context.guard_id
            target_app_termination_error = context.target_app_termination_error
            stop_reason = context.stop_reason
            auto_stop_after_sec = context.auto_stop_after_sec
            overlay = context.overlay
            context.overlay = None
            if context.auto_stop_timer is not None:
                context.auto_stop_timer.cancel()
    # Every ending funnels through here — manual stop, auto-stop, target exit,
    # repeated sample errors, shutdown — so this is the one place that can
    # guarantee the HUD and its forward rule never outlive the capture. Outside
    # the lock: teardown shells out to adb.
    _close_overlay(overlay)
    if guard_id is not None:
        _finish_temperature_guard(guard_id, "capture_ended")
    # ``capture_ended`` is the renderer's signal to unlock selection/restart.
    # Publish it only after registry and guard cleanup make that restart real.
    ended_payload: Dict[str, Any] = {
        "event": "capture_ended",
        "capture_id": capture_id,
        "seq": seq,
        "elapsed_sec": elapsed_sec,
        "pause_used": pause_used,
    }
    if target_app_termination_error is not None:
        ended_payload["target_app_termination_error"] = target_app_termination_error
    if stop_reason == "auto_stop":
        ended_payload["reason"] = "auto_stop"
        ended_payload["elapsed_sec"] = auto_stop_after_sec
    _emit(ended_payload)


def _close_session(session: object) -> None:
    closer = getattr(session, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:
            logger.exception("session close failed")


def _close_capture_context(context: _CaptureContext) -> None:
    guard_context: Optional[_TemperatureGuardContext] = None
    with context.cleanup_lock:
        if context.session_closed:
            return
        context.session_closed = True
        if context.guard_id is not None:
            with _captures_lock:
                guard_context = _active_temperature_guards.get(context.guard_id)
        if guard_context is None:
            _close_session(context.session)
            return
        with guard_context.cleanup_lock:
            _close_session(context.session)


def _handle_start_capture(payload: Dict[str, Any]) -> Dict[str, Any]:
    platform = _payload_platform(payload)
    serial = payload.get("serial")
    bundle_id = payload.get("package")
    if not serial or not bundle_id:
        raise ValueError("serial and package required")
    serial = str(serial)

    raw_duration = payload.get("duration_sec")
    duration_sec = float(raw_duration) if raw_duration is not None else _DEFAULT_CAPTURE_INTERVAL_SEC[platform]
    if isinstance(raw_duration, bool) or not math.isfinite(duration_sec) or duration_sec <= 0:
        raise ValueError("duration_sec must be > 0")
    auto_stop_after_sec = _parse_auto_stop_after_sec(payload)
    overlay_config = _parse_overlay_config(payload)

    backend = _backend_for_platform(platform)
    device_key = _physical_device_key(platform, serial, backend)

    # Block a second concurrent capture on the SAME physical device. Android frame
    # stats are device-global (SurfaceFlinger --timestats / `gfxinfo reset` are
    # cleared every sample) so two captures sharing one device would corrupt each
    # other's FPS/jank data. Different devices run fully in parallel. Reserve the
    # serial under the lock BEFORE the slow open handshake so two near-simultaneous
    # starts can't both slip through. The desktop surfaces this in the new window.
    with _captures_lock:
        if _device_busy_locked(device_key):
            raise _device_busy_error(serial)
        _reserved_serials.add(device_key)

    capture_id: str | None = None
    session: object | None = None
    try:
        package = PackageInfo(bundle_id=str(bundle_id))
        session = backend.open_streaming(serial, package)  # type: ignore[attr-defined]

        # A cold cache may only reveal Android's hw_serial during open. Rekey
        # before registration and reject a USB/Wi-Fi collision discovered at
        # that point rather than allowing two device-global FPS sessions.
        resolved_key = _physical_device_key(
            platform,
            serial,
            backend,
            getattr(session, "device", None),
        )
        with _captures_lock:
            if resolved_key != device_key:
                _reserved_serials.discard(device_key)
                if _device_busy_locked(resolved_key):
                    raise _device_busy_error(serial)
                _reserved_serials.add(resolved_key)
                device_key = resolved_key

        capture_id = uuid.uuid4().hex[:12]
        stop = threading.Event()
        thread = threading.Thread(
            target=_capture_pump,
            args=(capture_id, session, stop, duration_sec),
            name=f"capture-{capture_id}",
            daemon=True,
        )

        ctx = _CaptureContext(
            thread=thread,
            stop=stop,
            session=session,
            duration_sec=duration_sec,
            serial=serial,
            device_key=device_key,
            platform=platform,
            backend=backend,
            auto_stop_after_sec=auto_stop_after_sec,
        )
        with _captures_lock:
            _active_captures[capture_id] = ctx
            _reserved_serials.discard(device_key)
        # After registration (the pump epilogue owns teardown from here on) and
        # before the pump runs. Only use a bridge warmed by install/connect:
        # `am start` here would foreground the companion over the measured app.
        #
        # Under overlay_lock because the capture id is already public: a
        # set_capture_overlay arriving in this window would otherwise open a
        # second session and one of the two would be overwritten, leaking its
        # forward rule.
        with ctx.overlay_lock:
            opened = _open_overlay_session(
                platform,
                serial,
                str(bundle_id),
                overlay_config,
                session_id=ctx.mobile_session_id,
                label=getattr(getattr(session, "package", None), "label", None),
            )
            if opened is not None and not _publish_overlay(capture_id, ctx, opened):
                _close_overlay(opened)
        # Inside the try: if thread.start() raises (e.g. RuntimeError "can't start
        # new thread"), the except below pops the just-registered entry so the
        # pump's finally (which never runs) isn't relied on to free the serial.
        thread.start()
    except BaseException:
        # Open/registration/start failed — free the reservation, drop any registered
        # entry (its pump never ran, so it would otherwise linger and block the
        # device forever), and release the session so the device isn't held.
        orphan: Optional[_CaptureContext] = None
        with _captures_lock:
            _reserved_serials.discard(device_key)
            if capture_id is not None:
                orphan = _active_captures.pop(capture_id, None)
        if orphan is not None:
            _close_overlay(orphan.overlay)
        if session is not None:
            _close_session(session)
        raise

    return {
        "platform": platform,
        "capture_id": capture_id,
        "session_id": ctx.mobile_session_id,
        "device": session.device,  # type: ignore[attr-defined]
        "package": session.package,  # type: ignore[attr-defined]
        "duration_sec": duration_sec,
        "auto_stop_after_sec": auto_stop_after_sec,
    }


def _capture_elapsed(context: _CaptureContext) -> float:
    if context.measurement_started_at is None:
        return 0.0
    now = context.pause_started_at if context.pause_started_at is not None else time.monotonic()
    return max(0.0, now - context.measurement_started_at - context.paused_total_sec)


def _set_capture_paused(payload: Dict[str, Any], paused: bool) -> Dict[str, Any]:
    capture_id = payload.get("capture_id")
    with _captures_lock:
        context = _active_captures.get(capture_id)
        if context is None or context.stop_reason is not None:
            raise ValueError("capture is not running")
        now = time.monotonic()
        if paused and context.pause_started_at is None:
            context.pause_started_at = now
            context.pause_generation += 1
            if context.auto_stop_timer is not None:
                context.auto_stop_timer.cancel()
        elif not paused and context.pause_started_at is not None:
            delay = now - context.pause_started_at
            context.paused_total_sec += delay
            context.pause_started_at = None
            context.pause_generation += 1
            if context.auto_stop_deadline is not None:
                context.auto_stop_deadline += delay
                timer = threading.Timer(max(0.0, context.auto_stop_deadline - now), _auto_stop_capture, args=(capture_id,))
                timer.daemon = True
                context.auto_stop_timer = timer
                timer.start()
        result = {"capture_id": capture_id, "paused": paused, "elapsed_sec": _capture_elapsed(context),
                  "pause_used": context.pause_generation > 0}
        # Same lock as snapshot emission: paused always follows the last kept sample.
        _emit({"event": "capture_state", **result})
    if not paused and context.measurement_started_at is not None:
        _arm_capture_auto_stop(capture_id)
    return result


def _handle_pause_capture(payload: Dict[str, Any]) -> Dict[str, Any]:
    return _set_capture_paused(payload, True)


def _handle_resume_capture(payload: Dict[str, Any]) -> Dict[str, Any]:
    return _set_capture_paused(payload, False)


def _handle_save_session_cuts(payload: Dict[str, Any]) -> Dict[str, Any]:
    from .session_cuts import save_cuts

    raw = payload.get("path")
    if not isinstance(raw, str) or not raw:
        raise ValueError("path required")
    path = os.path.abspath(raw)
    root = os.path.realpath(_managed_raw_dir())
    if os.path.islink(path) or os.path.commonpath([os.path.realpath(path), root]) != root:
        raise ValueError("session is outside the managed session directory")
    if not path.lower().endswith(".csv"):
        raise ValueError("session must be a CSV file")
    cuts = save_cuts(path, payload.get("cuts"), payload.get("expected_cuts"))
    _emit({"event": "session_library_changed"})
    return {"path": path, "cuts": cuts}


def _handle_stop_capture(payload: Dict[str, Any]) -> Dict[str, Any]:
    capture_id = payload.get("capture_id")
    if not capture_id:
        raise ValueError("capture_id required")

    manual_stop_won = False
    with _captures_lock:
        ctx = _active_captures.get(capture_id)
        if ctx is not None and ctx.stop_reason is None:
            ctx.stop_reason = "manual"
            ctx.stop.set()
            manual_stop_won = True
        guard_context = (
            _active_temperature_guards.get(ctx.guard_id)
            if manual_stop_won and ctx is not None and ctx.guard_id is not None
            else None
        )
        if guard_context is not None and not guard_context.ended:
            guard_context.stop.set()
            guard_context.state = "stopping"

    if ctx is None:
        return {"capture_id": capture_id, "status": "not_running"}

    if guard_context is not None:
        _emit_temperature_guard_state(guard_context, "stopping")

    if not manual_stop_won:
        # An automatic stop already owns teardown (or another manual request
        # won).  Do not accidentally convert it into an app-preserving stop.
        return {"capture_id": capture_id, "status": "stopping"}
    # Close the session too: if the pump is blocked inside sample() waiting on a
    # slow one-off iOS connection handshake, closing unblocks it immediately so
    # stop is responsive and the streamer subprocess is torn down right away.
    _close_capture_context(ctx)
    # Don't join — the pump thread is daemon and will emit its own
    # "capture_ended" event. Joining would block stdin processing.
    return {"capture_id": capture_id, "status": "stopping"}


# --- Battery-temperature preview handlers -------------------------------


def _emit_temperature_preview_state(
    context: _TemperaturePreviewContext, state: str
) -> None:
    _emit(
        {
            "event": "temperature_preview_state",
            "preview_id": context.preview_id,
            "state": state,
        }
    )


def _close_temperature_preview_resource(
    context: _TemperaturePreviewContext, resource: object
) -> None:
    with context.cleanup_lock:
        resource_id = id(resource)
        if resource_id in context.closed_resource_ids:
            return
        context.closed_resource_ids.add(resource_id)
        _close_session(resource)


def _finish_temperature_preview(preview_id: str, reason: str) -> None:
    context: Optional[_TemperaturePreviewContext] = None
    monitor: Optional[object] = None
    with _captures_lock:
        current = _active_temperature_previews.get(preview_id)
        if current is None or current.ended:
            return
        current.ended = True
        current.state = "ended"
        monitor = current.monitor
        current.monitor = None
        _reserved_serials.add(current.device_key)
        _active_temperature_previews.pop(preview_id, None)
        context = current
    if monitor is not None:
        _close_temperature_preview_resource(context, monitor)
    with _captures_lock:
        _reserved_serials.discard(context.device_key)
    _emit(
        {
            "event": "temperature_preview_ended",
            "preview_id": context.preview_id,
            "reason": reason,
        }
    )


def _fail_temperature_preview(
    context: _TemperaturePreviewContext, code: str, error: object
) -> None:
    monitor: Optional[object] = None
    should_emit = False
    cancelled = False
    with _captures_lock:
        current = _active_temperature_previews.get(context.preview_id)
        if current is context and not context.ended and context.stop.is_set():
            cancelled = True
        elif current is context and not context.ended:
            context.ended = True
            context.state = "error"
            monitor = context.monitor
            context.monitor = None
            _reserved_serials.add(context.device_key)
            _active_temperature_previews.pop(context.preview_id, None)
            should_emit = True
    if cancelled:
        _finish_temperature_preview(context.preview_id, "cancelled")
        return
    if not should_emit:
        return
    if monitor is not None:
        _close_temperature_preview_resource(context, monitor)
    with _captures_lock:
        _reserved_serials.discard(context.device_key)
    _emit_temperature_preview_state(context, "error")
    _emit(
        {
            "event": "temperature_preview_error",
            "preview_id": context.preview_id,
            "code": code,
            "error": str(error),
        }
    )
    _emit(
        {
            "event": "temperature_preview_ended",
            "preview_id": context.preview_id,
            "reason": code,
        }
    )


def _temperature_preview_pump(context: _TemperaturePreviewContext) -> None:
    try:
        factory = getattr(context.backend, "create_temperature_preview_monitor")
        monitor = factory(
            context.serial,
            package=context.package,
            stop_event=context.stop,
        )
    except BaseException as exc:
        if context.stop.is_set():
            _finish_temperature_preview(context.preview_id, "cancelled")
        else:
            _fail_temperature_preview(context, "temperature_monitor_failed", exc)
        return

    with _captures_lock:
        current = _active_temperature_previews.get(context.preview_id)
        active = current is context and not context.ended and not context.stop.is_set()
        if active:
            context.monitor = monitor
            context.state = "running"
            _emit_temperature_preview_state(context, "running")
    if not active:
        _close_temperature_preview_resource(context, monitor)
        _finish_temperature_preview(context.preview_id, "cancelled")
        return

    first_valid_deadline = (
        time.monotonic() + TEMPERATURE_GUARD_FIRST_READING_TIMEOUT_SEC
    )
    saw_valid = False
    while not context.stop.is_set():
        sample_started = time.monotonic()
        try:
            reading = _coerce_temperature_reading(
                monitor.sample()  # type: ignore[attr-defined]
            )
        except BaseException as exc:
            if context.stop.is_set():
                _finish_temperature_preview(context.preview_id, "cancelled")
            else:
                message = str(exc)
                code = (
                    "temperature_unavailable"
                    if "temperature_unavailable" in message
                    else "device_disconnected"
                    if "device_disconnected" in message
                    else "temperature_monitor_failed"
                )
                _fail_temperature_preview(context, code, exc)
            return

        if reading is not None:
            saw_valid = True
            with _captures_lock:
                current = _active_temperature_previews.get(context.preview_id)
                sample_is_current = (
                    current is context
                    and not context.ended
                    and not context.stop.is_set()
                    and context.state == "running"
                )
                if sample_is_current:
                    _emit(
                        {
                            "event": "temperature_preview_sample",
                            "preview_id": context.preview_id,
                            "temperature_c": reading.temperature_c,
                            "sampled_at": reading.sampled_at,
                            "source": reading.source,
                        }
                    )
            if not sample_is_current:
                _finish_temperature_preview(context.preview_id, "cancelled")
                return
        elif not saw_valid and time.monotonic() >= first_valid_deadline:
            _fail_temperature_preview(
                context,
                "temperature_unavailable",
                "no valid battery temperature within 45 seconds",
            )
            return

        elapsed = time.monotonic() - sample_started
        remaining = max(0.0, TEMPERATURE_GUARD_SAMPLE_INTERVAL_SEC - elapsed)
        if context.stop.wait(remaining):
            break
    _finish_temperature_preview(context.preview_id, "cancelled")


def _handle_create_temperature_preview(payload: Dict[str, Any]) -> Dict[str, Any]:
    platform = _payload_platform(payload)
    serial_value = payload.get("serial")
    package_value = payload.get("package")
    if not serial_value or not package_value:
        raise ValueError("serial and package required")
    serial = str(serial_value)
    backend = _backend_for_platform(platform)
    device_key = _physical_device_key(platform, serial, backend)
    with _captures_lock:
        if _device_busy_locked(device_key):
            raise _device_busy_error(serial)
        _reserved_serials.add(device_key)

    try:
        device, package = backend.prepare_capture(  # type: ignore[attr-defined]
            serial, PackageInfo(bundle_id=str(package_value))
        )
        resolved_key = _physical_device_key(
            platform, serial, backend, device=device
        )
        with _captures_lock:
            if resolved_key != device_key:
                _reserved_serials.discard(device_key)
                if _device_busy_locked(resolved_key):
                    raise _device_busy_error(serial)
                _reserved_serials.add(resolved_key)
                device_key = resolved_key
        preview_id = uuid.uuid4().hex[:12]
        context = _TemperaturePreviewContext(
            preview_id,
            platform,
            serial,
            device_key,
            backend,
            device,
            package,
        )
        with _captures_lock:
            _active_temperature_previews[preview_id] = context
    except BaseException:
        with _captures_lock:
            _reserved_serials.discard(device_key)
        raise

    return {
        "preview_id": preview_id,
        "state": context.state,
        "device": device,
        "package": package,
    }


def _handle_activate_temperature_preview(payload: Dict[str, Any]) -> Dict[str, Any]:
    preview_id = payload.get("preview_id")
    if not preview_id:
        raise ValueError("preview_id required")
    preview_id = str(preview_id)
    with _captures_lock:
        context = _active_temperature_previews.get(preview_id)
        if context is None or context.ended:
            return {"preview_id": preview_id, "status": "not_running"}
        if context.thread is not None:
            return {"preview_id": preview_id, "status": context.state}
        if context.stop.is_set():
            return {"preview_id": preview_id, "status": "stopping"}
        thread = threading.Thread(
            target=_temperature_preview_pump,
            args=(context,),
            name=f"temperature-preview-{preview_id}",
            daemon=True,
        )
        context.thread = thread
        try:
            thread.start()
        except BaseException:
            context.thread = None
            context.ended = True
            context.state = "ended"
            _active_temperature_previews.pop(preview_id, None)
            _reserved_serials.discard(context.device_key)
            raise
        _emit_temperature_preview_state(context, "preparing")
    return {"preview_id": preview_id, "status": "preparing"}


def _handle_stop_temperature_preview(payload: Dict[str, Any]) -> Dict[str, Any]:
    preview_id = payload.get("preview_id")
    if not preview_id:
        raise ValueError("preview_id required")
    preview_id = str(preview_id)
    finish_now = False
    monitor: Optional[object] = None
    with _captures_lock:
        context = _active_temperature_previews.get(preview_id)
        if context is None or context.ended:
            return {"preview_id": preview_id, "status": "not_running"}
        context.stop.set()
        context.state = "stopping"
        # An activated preview remains registered/reserved until its worker has
        # actually escaped monitor.sample() and reached _finish.  close() is
        # only the unblock signal here; publishing ended early would let a new
        # guard overlap the still-running adb/Diagnostics worker.
        finish_now = context.thread is None
        monitor = context.monitor
        _emit_temperature_preview_state(context, "stopping")
    if monitor is not None:
        _close_temperature_preview_resource(context, monitor)
    if finish_now:
        _finish_temperature_preview(preview_id, "cancelled")
    return {"preview_id": preview_id, "status": "stopping"}


# --- Battery-temperature guard handlers ---------------------------------


def _coerce_temperature_reading(value: object) -> Optional[BatteryTemperatureReading]:
    if isinstance(value, BatteryTemperatureReading):
        reading = value
    elif isinstance(value, dict):
        try:
            reading = BatteryTemperatureReading(
                temperature_c=float(value.get("temperature_c")),
                sampled_at=str(value.get("sampled_at") or ""),
                source=str(value.get("source") or "unknown"),
            )
        except (TypeError, ValueError):
            return None
    else:
        return None

    temperature_c = reading.temperature_c
    if isinstance(temperature_c, bool):
        return None
    try:
        numeric = float(temperature_c)
    except (TypeError, ValueError):
        return None
    if (
        not math.isfinite(numeric)
        or numeric < _MIN_BATTERY_TEMPERATURE_C
        or numeric > _MAX_BATTERY_TEMPERATURE_C
    ):
        return None
    return BatteryTemperatureReading(
        temperature_c=numeric,
        sampled_at=reading.sampled_at,
        source=reading.source,
    )


def _emit_temperature_guard_state(
    context: _TemperatureGuardContext, state: str
) -> None:
    _emit(
        {
            "event": "temperature_guard_state",
            "guard_id": context.guard_id,
            "state": state,
        }
    )


def _close_temperature_guard_resource(
    context: _TemperatureGuardContext, resource: object
) -> None:
    # stop(), error cleanup, and the monitor thread can converge concurrently.
    # Serialize close so reservation release never races ahead of an in-flight
    # Diagnostics socket/subprocess teardown.
    with context.cleanup_lock:
        _close_session(resource)


def _finish_temperature_guard(guard_id: str, reason: str) -> None:
    monitor: Optional[object] = None
    context: Optional[_TemperatureGuardContext] = None
    with _captures_lock:
        current = _active_temperature_guards.get(guard_id)
        if current is None or current.ended:
            return
        current.ended = True
        current.state = "ended"
        monitor = current.monitor
        current.monitor = None
        # Keep the physical key blocked until monitor/socket teardown finishes.
        _reserved_serials.add(current.device_key)
        _active_temperature_guards.pop(guard_id, None)
        context = current
    if monitor is not None:
        _close_temperature_guard_resource(context, monitor)
    if context is not None:
        with _captures_lock:
            _reserved_serials.discard(context.device_key)
        _emit(
            {
                "event": "temperature_guard_ended",
                "guard_id": context.guard_id,
                "reason": reason,
            }
        )


def _fail_temperature_guard(
    context: _TemperatureGuardContext, code: str, error: object
) -> None:
    monitor: Optional[object] = None
    should_emit = False
    cancelled = False
    with _captures_lock:
        current = _active_temperature_guards.get(context.guard_id)
        if (
            current is context
            and not context.ended
            and context.stop.is_set()
        ):
            cancelled = True
        elif current is context and not context.ended:
            context.state = "error"
            context.ended = True
            monitor = context.monitor
            context.monitor = None
            _reserved_serials.add(context.device_key)
            _active_temperature_guards.pop(context.guard_id, None)
            should_emit = True
    if cancelled:
        _finish_temperature_guard(context.guard_id, "cancelled")
        return
    if not should_emit:
        return
    if monitor is not None:
        _close_temperature_guard_resource(context, monitor)
    with _captures_lock:
        _reserved_serials.discard(context.device_key)
    # The renderer unlocks the immutable selection when it sees a terminal
    # error. Emit only after the monitor/socket is closed and the reservation is
    # actually free, otherwise an immediate retry can spuriously hit device_busy.
    _emit_temperature_guard_state(context, "error")
    _emit(
        {
            "event": "temperature_guard_error",
            "guard_id": context.guard_id,
            "code": code,
            "error": str(error),
        }
    )
    _emit(
        {
            "event": "temperature_guard_ended",
            "guard_id": context.guard_id,
            "reason": code,
        }
    )


def _promote_temperature_guard(
    context: _TemperatureGuardContext,
    session: object,
    trigger_temperature_c: float,
) -> bool:
    """Atomically replace a guard reservation with a live capture."""
    capture_id = uuid.uuid4().hex[:12]
    gate = threading.Event()
    mobile_session_id = str(uuid.uuid4())

    # Warm connect only, even though this is still before the first sample: the
    # guard has just attached to or launched the measured app, and `am start`-ing
    # the companion on top of it would foreground the companion instead — the
    # backend would then end the capture as backgrounded a few samples in.
    overlay = _open_overlay_session(
        context.config.platform.value,
        context.serial,
        context.config.package.bundle_id,
        context.config.overlay,
        allow_cold_start=False,
        session_id=mobile_session_id,
        label=context.config.package.label,
    )

    def run_capture() -> None:
        gate.wait()
        _capture_pump(
            capture_id,
            session,
            context.stop,
            context.config.duration_sec,
        )

    thread = threading.Thread(
        target=run_capture,
        name=f"capture-{capture_id}",
        daemon=True,
    )
    capture_context = _CaptureContext(
        thread=thread,
        stop=context.stop,
        session=session,
        duration_sec=context.config.duration_sec,
        serial=context.serial,
        device_key=context.device_key,
        guard_id=context.guard_id,
        platform=context.config.platform.value,
        backend=context.backend,
        auto_stop_after_sec=context.config.auto_stop_after_sec,
        overlay=overlay,
        mobile_session_id=mobile_session_id,
    )

    promoted = False
    try:
        with _captures_lock:
            current = _active_temperature_guards.get(context.guard_id)
            if (
                current is not context
                or context.ended
                or context.stop.is_set()
                or context.state != "triggering"
            ):
                return False

            _active_captures[capture_id] = capture_context
            _reserved_serials.discard(context.device_key)
            context.capture_id = capture_id
            context.monitor = None  # successful platform hand-off owns the resource
            context.state = "running"
            try:
                thread.start()
            except BaseException:
                _active_captures.pop(capture_id, None)
                _reserved_serials.add(context.device_key)
                context.capture_id = None
                context.state = "triggering"
                raise

            # Keep these events ordered before the pump can emit its first snapshot.
            _emit_temperature_guard_state(context, "running")
            _emit(
                {
                    "event": "temperature_guard_started",
                    "guard_id": context.guard_id,
                    "capture_id": capture_id,
                    "session_id": mobile_session_id,
                    "trigger_temperature_c": trigger_temperature_c,
                    "device": getattr(session, "device", context.config.device),
                    "package": getattr(session, "package", context.config.package),
                    "duration_sec": context.config.duration_sec,
                    "auto_stop_after_sec": context.config.auto_stop_after_sec,
                }
            )
            gate.set()
        promoted = True
    finally:
        # The pump epilogue only tears the HUD down for a capture it actually
        # ran; an abandoned promotion has to release the forward rule itself.
        # Outside the lock: teardown shells out to adb.
        if not promoted:
            _close_overlay(overlay)
    return True


def _open_guard_capture(
    context: _TemperatureGuardContext, trigger_temperature_c: float
) -> None:
    config = context.config
    try:
        if config.cold_start:
            opener = getattr(context.backend, "cold_start_and_open_streaming")
            if config.platform == Platform.ANDROID:
                session = opener(
                    context.serial,
                    config.package,
                    stop_event=context.stop,
                )
            else:
                session = opener(context.serial, config.package)
        else:
            session = context.backend.open_streaming(  # type: ignore[attr-defined]
                context.serial, config.package
            )
    except BaseException as exc:
        if context.stop.is_set():
            _finish_temperature_guard(context.guard_id, "cancelled")
            return
        message = str(exc)
        code = (
            "app_launch_failed"
            if "app_launch_failed" in message
            or "launcher_activity_unavailable" in message
            else "capture_start_failed"
        )
        _fail_temperature_guard(context, code, exc)
        return

    if context.stop.is_set():
        _close_temperature_guard_resource(context, session)
        _finish_temperature_guard(context.guard_id, "cancelled")
        return
    try:
        promoted = _promote_temperature_guard(
            context, session, trigger_temperature_c
        )
    except BaseException as exc:
        _close_temperature_guard_resource(context, session)
        if context.stop.is_set():
            _finish_temperature_guard(context.guard_id, "cancelled")
        else:
            _fail_temperature_guard(context, "capture_start_failed", exc)
        return
    if not promoted:
        _close_temperature_guard_resource(context, session)
        _finish_temperature_guard(context.guard_id, "cancelled")


def _temperature_guard_pump(context: _TemperatureGuardContext) -> None:
    try:
        monitor = context.backend.create_temperature_monitor(  # type: ignore[attr-defined]
            context.serial,
            package=context.config.package,
            threshold_c=context.config.threshold_c,
            required_hits=context.config.required_hits,
            stop_event=context.stop,
        )
    except BaseException as exc:
        if context.stop.is_set():
            _finish_temperature_guard(context.guard_id, "cancelled")
        else:
            _fail_temperature_guard(
                context, "temperature_monitor_failed", exc
            )
        return

    with _captures_lock:
        current = _active_temperature_guards.get(context.guard_id)
        if current is context and not context.ended:
            context.monitor = monitor
        cancelled = (
            current is not context or context.ended or context.stop.is_set()
        )
        if not cancelled:
            context.state = "waiting"
            _emit_temperature_guard_state(context, "waiting")
    if cancelled:
        _close_temperature_guard_resource(context, monitor)
        _finish_temperature_guard(context.guard_id, "cancelled")
        return

    first_valid_deadline = (
        time.monotonic() + TEMPERATURE_GUARD_FIRST_READING_TIMEOUT_SEC
    )
    saw_valid = False
    consecutive_hits = 0

    while not context.stop.is_set():
        sample_started = time.monotonic()
        try:
            raw_reading = monitor.sample()  # type: ignore[attr-defined]
        except BaseException as exc:
            if context.stop.is_set():
                _finish_temperature_guard(context.guard_id, "cancelled")
            else:
                message = str(exc)
                code = (
                    "temperature_unavailable"
                    if "temperature_unavailable" in message
                    else "device_disconnected"
                    if "device_disconnected" in message
                    else "temperature_monitor_failed"
                )
                _fail_temperature_guard(context, code, exc)
            return

        reading = _coerce_temperature_reading(raw_reading)
        if reading is None:
            consecutive_hits = 0
            if not saw_valid and time.monotonic() >= first_valid_deadline:
                _fail_temperature_guard(
                    context,
                    "temperature_unavailable",
                    "no valid battery temperature within 45 seconds",
                )
                return
        else:
            saw_valid = True
            if reading.temperature_c >= context.config.threshold_c:
                consecutive_hits += 1
            else:
                consecutive_hits = 0
            with _captures_lock:
                current = _active_temperature_guards.get(context.guard_id)
                sample_is_current = (
                    current is context
                    and not context.ended
                    and not context.stop.is_set()
                    and context.state == "waiting"
                )
                won_trigger = (
                    sample_is_current
                    and consecutive_hits >= context.config.required_hits
                )
                if sample_is_current:
                    _emit(
                        {
                            "event": "temperature_guard_sample",
                            "guard_id": context.guard_id,
                            "temperature_c": reading.temperature_c,
                            "sampled_at": reading.sampled_at,
                            "source": reading.source,
                            "threshold_c": context.config.threshold_c,
                            "consecutive_hits": consecutive_hits,
                            "required_hits": context.config.required_hits,
                        }
                    )
                if won_trigger:
                    context.state = "triggering"
                    _emit_temperature_guard_state(context, "triggering")
            if not sample_is_current:
                if context.stop.is_set():
                    _finish_temperature_guard(context.guard_id, "cancelled")
                return
            if won_trigger:
                _open_guard_capture(context, reading.temperature_c)
                return

        elapsed = time.monotonic() - sample_started
        remaining = max(0.0, TEMPERATURE_GUARD_SAMPLE_INTERVAL_SEC - elapsed)
        if context.stop.wait(remaining):
            break

    _finish_temperature_guard(context.guard_id, "cancelled")


def _handle_create_temperature_guard(payload: Dict[str, Any]) -> Dict[str, Any]:
    platform = _payload_platform(payload)
    serial_value = payload.get("serial")
    package_value = payload.get("package")
    if not serial_value or not package_value:
        raise ValueError("serial and package required")
    serial = str(serial_value)
    bundle_id = str(package_value)

    threshold_raw = payload.get(
        "threshold_c", DEFAULT_TEMPERATURE_GUARD_THRESHOLD_C
    )
    if isinstance(threshold_raw, bool):
        raise ValueError("threshold_c must be a number")
    try:
        threshold_c = float(threshold_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("threshold_c must be a number") from exc
    if (
        not math.isfinite(threshold_c)
        or threshold_c < _MIN_BATTERY_TEMPERATURE_C
        or threshold_c > _MAX_BATTERY_TEMPERATURE_C
    ):
        raise ValueError("threshold_c must be between 5.0 and 60.0")

    required_raw = payload.get("required_hits", 2)
    if isinstance(required_raw, bool):
        raise ValueError("required_hits must be an integer")
    try:
        required_hits = int(required_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("required_hits must be an integer") from exc
    if isinstance(required_raw, float) and not required_raw.is_integer():
        raise ValueError("required_hits must be an integer")
    if required_hits <= 0 or required_hits > 100:
        raise ValueError("required_hits must be between 1 and 100")

    cold_start = payload.get("cold_start", True)
    if not isinstance(cold_start, bool):
        raise ValueError("cold_start must be a boolean")
    duration_raw = payload.get("duration_sec", _DEFAULT_CAPTURE_INTERVAL_SEC[platform])
    if isinstance(duration_raw, bool):
        raise ValueError("duration_sec must be > 0")
    try:
        duration_sec = float(duration_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("duration_sec must be > 0") from exc
    if not math.isfinite(duration_sec) or duration_sec <= 0:
        raise ValueError("duration_sec must be > 0")
    auto_stop_after_sec = _parse_auto_stop_after_sec(payload)
    overlay_config = _parse_overlay_config(payload)

    backend = _backend_for_platform(platform)
    device_key = _physical_device_key(platform, serial, backend)
    with _captures_lock:
        if _device_busy_locked(device_key):
            raise _device_busy_error(serial)
        _reserved_serials.add(device_key)

    try:
        prepare = getattr(backend, "prepare_capture")
        device, package = prepare(serial, PackageInfo(bundle_id=bundle_id))
        resolved_key = _physical_device_key(
            platform, serial, backend, device=device
        )
        with _captures_lock:
            if resolved_key != device_key:
                _reserved_serials.discard(device_key)
                if _device_busy_locked(resolved_key):
                    raise _device_busy_error(serial)
                _reserved_serials.add(resolved_key)
                device_key = resolved_key

        config = TemperatureGuardConfig(
            platform=Platform(platform),
            device=device,
            package=package,
            threshold_c=threshold_c,
            required_hits=required_hits,
            cold_start=cold_start,
            duration_sec=duration_sec,
            auto_stop_after_sec=auto_stop_after_sec,
            overlay=overlay_config,
        )
        guard_id = uuid.uuid4().hex[:12]
        context = _TemperatureGuardContext(
            guard_id, config, serial, device_key, backend
        )
        with _captures_lock:
            _active_temperature_guards[guard_id] = context
    except BaseException:
        with _captures_lock:
            _reserved_serials.discard(device_key)
        raise

    return {
        "guard_id": guard_id,
        "state": context.state,
        "device": device,
        "package": package,
        "duration_sec": duration_sec,
        "auto_stop_after_sec": auto_stop_after_sec,
    }


def _handle_activate_temperature_guard(payload: Dict[str, Any]) -> Dict[str, Any]:
    guard_id = payload.get("guard_id")
    if not guard_id:
        raise ValueError("guard_id required")
    guard_id = str(guard_id)

    with _captures_lock:
        context = _active_temperature_guards.get(guard_id)
        if context is None or context.ended:
            return {"guard_id": guard_id, "status": "not_running"}
        if context.thread is not None:
            return {"guard_id": guard_id, "status": context.state}
        if context.stop.is_set():
            return {"guard_id": guard_id, "status": "stopping"}
        thread = threading.Thread(
            target=_temperature_guard_pump,
            args=(context,),
            name=f"temperature-guard-{guard_id}",
            daemon=True,
        )
        context.thread = thread
        try:
            thread.start()
        except BaseException:
            context.thread = None
            context.ended = True
            context.state = "ended"
            _active_temperature_guards.pop(guard_id, None)
            _reserved_serials.discard(context.device_key)
            raise
        _emit_temperature_guard_state(context, "preparing")
    return {"guard_id": guard_id, "status": "preparing"}


def _handle_stop_temperature_guard(payload: Dict[str, Any]) -> Dict[str, Any]:
    guard_id = payload.get("guard_id")
    if not guard_id:
        raise ValueError("guard_id required")
    guard_id = str(guard_id)

    monitor: Optional[object] = None
    capture_context: Optional[_CaptureContext] = None
    finish_now = False
    with _captures_lock:
        context = _active_temperature_guards.get(guard_id)
        if context is None or context.ended:
            return {"guard_id": guard_id, "status": "not_running"}
        context.stop.set()
        context.state = "stopping"
        monitor = context.monitor
        if context.capture_id is not None:
            capture_context = _active_captures.get(context.capture_id)
        finish_now = context.thread is None and capture_context is None
        _emit_temperature_guard_state(context, "stopping")

    if monitor is not None:
        _close_temperature_guard_resource(context, monitor)
    if capture_context is not None:
        capture_context.stop.set()
        _close_capture_context(capture_context)
    elif finish_now:
        _finish_temperature_guard(guard_id, "cancelled")
    return {"guard_id": guard_id, "status": "stopping"}


# --- CSV export of live snapshots ----------------------------------------


def _snapshot_from_dict(data: Dict[str, Any]) -> MetricsSnapshot:
    """Reconstruct a MetricsSnapshot dataclass from its dict serialization.

    The frontend collects ``snapshot`` events (each with a ``data`` field that
    is ``dataclasses.asdict(snapshot)``) and sends them back here for CSV
    export. Re-hydrating into the original dataclasses lets us reuse
    ``mpi.report.generate_csv_report`` unchanged.
    """
    device_d = data.get("device") or {}
    package_d = data.get("package") or {}
    frames_d = data.get("frames") or {}
    memory_d = data.get("memory") or {}
    cpu_d = data.get("cpu") or {}
    network_d = data.get("network") or {}
    gpu_d = data.get("gpu") or {}
    thermal_d = data.get("thermal") or {}
    battery_d = data.get("battery") or {}

    platform_value = device_d.get("platform") or "android"
    try:
        platform = Platform(platform_value)
    except ValueError:
        platform = Platform.ANDROID

    return MetricsSnapshot(
        device=DeviceInfo(
            serial=str(device_d.get("serial") or "unknown"),
            platform=platform,
            model=str(device_d.get("model") or "unknown"),
            model_identifier=str(device_d.get("model_identifier") or ""),
            model_number=str(device_d.get("model_number") or ""),
            display_name=str(device_d.get("display_name") or ""),
            os_version=str(device_d.get("os_version") or "unknown"),
            build_version=str(device_d.get("build_version") or ""),
            build_name=str(device_d.get("build_name") or ""),
            connection_type=str(device_d.get("connection_type") or "usb"),
            code_name=str(device_d.get("code_name") or ""),
            hw_ver=str(device_d.get("hw_ver") or ""),
            sw_ver=str(device_d.get("sw_ver") or ""),
            hw_serial=str(device_d.get("hw_serial") or ""),
        ),
        package=PackageInfo(
            bundle_id=str(package_d.get("bundle_id") or "unknown"),
            label=package_d.get("label"),
            version_name=package_d.get("version_name"),
            version_code=package_d.get("version_code"),
            apk_path=package_d.get("apk_path"),
            uid=package_d.get("uid"),
        ),
        captured_at=str(data.get("captured_at") or ""),
        duration_sec=float(data.get("duration_sec") or 0.0),
        elapsed_sec=_float_or_none(data.get("elapsed_sec")),
        pause_used=data.get("pause_used") is True,
        frames=FrameStats(**{k: frames_d[k] for k in FrameStats.__dataclass_fields__ if k in frames_d}),
        memory=MemStats(**{k: memory_d[k] for k in MemStats.__dataclass_fields__ if k in memory_d}),
        cpu=CpuStats(**{k: cpu_d[k] for k in CpuStats.__dataclass_fields__ if k in cpu_d}),
        network=NetworkStats(
            **{k: network_d[k] for k in NetworkStats.__dataclass_fields__ if k in network_d}
        ),
        gpu=GpuStats(**{k: gpu_d[k] for k in GpuStats.__dataclass_fields__ if k in gpu_d}),
        thermal=ThermalStats(zones_c=dict(thermal_d.get("zones_c") or {})),
        battery=BatteryStats(**{k: battery_d[k] for k in BatteryStats.__dataclass_fields__ if k in battery_d}),
        raw=dict(data.get("raw") or {}),
        metric_status=dict(data.get("metric_status") or {}),
    )


def _raw_save_path(save_path: str) -> str:
    """Route a chosen path into a sibling ``Raw/`` subfolder (auto-created).

    ``…/dir/file.csv`` -> ``…/dir/Raw/file.csv``. The spec requires raw exports
    to live under a ``Raw`` folder; the user still picks the base directory.
    """
    directory, filename = os.path.split(save_path)
    raw_dir = os.path.join(directory or ".", "Raw")
    os.makedirs(raw_dir, exist_ok=True)
    return os.path.join(raw_dir, filename)


def _write_live_csv(
    snapshots: Sequence[MetricsSnapshot],
    save_path: str,
    session_id: Optional[str] = None,
    session_name: Optional[str] = None,
    cuts: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Write snapshots to ``Raw/<file>`` with an MPI/hash header. Returns the path."""
    first = snapshots[0]
    info_hash = gen_info_hash(
        first.device.serial,
        first.package.bundle_id,
        datetime.now().isoformat(timespec="seconds"),
    )
    target = _raw_save_path(save_path)
    icon = _fetch_app_icon_data_uri(
        first.device.platform.value,
        first.device.serial,
        first.package.bundle_id,
        first.package.apk_path,
    )
    generate_csv_report(
        snapshots,
        target,
        info_hash=info_hash,
        session_id=session_id,
        session_name=session_name,
        app_icon=icon,
        cuts=cuts,
    )
    return target


def _handle_export_live_csv(payload: Dict[str, Any]) -> Dict[str, Any]:
    save_path = payload.get("save_path")
    snapshots_raw = payload.get("snapshots")
    if not save_path:
        raise ValueError("save_path required")
    if not isinstance(snapshots_raw, list) or not snapshots_raw:
        raise ValueError("snapshots must be a non-empty list")

    snapshots = [_snapshot_from_dict(s) for s in snapshots_raw]
    target = _write_live_csv(
        snapshots,
        save_path,
        _opt_str(payload.get("session_id")),
        _opt_str(payload.get("session_name")),
        payload.get("cuts"),
    )
    return {"save_path": target, "count": len(snapshots)}


def _handle_auto_save_raw_csv(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Auto-save the raw CSV on measurement end, no dialog.

    Writes ``Documents/MPI/Raw/mpi_<model>_<ts>.csv`` (falling
    back to the home dir when Documents is absent). Returns the written path. An
    empty session is a no-op (returns ``saved=False``) rather than an error.
    A close-time partial backup uses ``Documents/MPI/Recovery/Raw`` so a delayed
    write cannot replace a completed CSV or appear in the managed session library.
    """
    snapshots_raw = payload.get("snapshots")
    if not isinstance(snapshots_raw, list) or not snapshots_raw:
        return {"saved": False, "reason": "no snapshots"}

    snapshots = [_snapshot_from_dict(s) for s in snapshots_raw]
    base = os.path.dirname(_managed_raw_dir())
    if payload.get("close_backup") is True:
        base = os.path.join(base, "Recovery")
    model = (
        snapshots[0].device.display_name
        or snapshots[0].device.model
        or "live"
    ).replace(os.sep, "_").replace(" ", "_")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # _write_live_csv appends the Raw/ subfolder, so point at base/<file>.
    save_path = os.path.join(base, f"mpi_{model}_{stamp}.csv")
    target = _write_live_csv(
        snapshots,
        save_path,
        _opt_str(payload.get("session_id")),
        _opt_str(payload.get("session_name")),
        payload.get("cuts"),
    )
    if payload.get("close_backup") is not True:
        # Recovery backups do not belong to the managed session library.
        _emit({"event": "session_library_changed"})
    return {"saved": True, "save_path": target, "count": len(snapshots)}


def _handle_build_endpoint_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Build the JSON payload for the configured data-transfer endpoint.

    Returns the same 3-section data as the raw CSV (metadata/summary/time-series)
    as a JSON-serializable dict, with an MPI identity ``info_hash``. The Electron
    main process POSTs this to the user-configured endpoint. Raises on empty
    sessions so the caller can skip the POST.
    """
    snapshots_raw = payload.get("snapshots")
    if not isinstance(snapshots_raw, list) or not snapshots_raw:
        raise ValueError("snapshots must be a non-empty list")
    snapshots = [_snapshot_from_dict(s) for s in snapshots_raw]
    first = snapshots[0]
    info_hash = gen_info_hash(
        first.device.serial,
        first.package.bundle_id,
        datetime.now().isoformat(timespec="seconds"),
    )
    # Optional user-set session name, set by the desktop naming modal after a
    # capture ends; forwarded to metadata.capture.name for the receiver.
    name_raw = payload.get("name")
    name = name_raw if isinstance(name_raw, str) and name_raw.strip() else None
    # Best-effort app launcher icon for a receiver's session list. The backend
    # icon fetch is disk-cached (populated when the package was selected), so this
    # is normally instant and never blocks the save; failures degrade to no icon.
    icon = _fetch_app_icon_data_uri(
        first.device.platform.value,
        first.device.serial,
        first.package.bundle_id,
        first.package.apk_path,
    )
    return build_endpoint_payload(
        snapshots,
        info_hash=info_hash,
        name=name,
        icon=icon,
        session_id=_opt_str(payload.get("session_id")),
        cuts=payload.get("cuts"),
    )


def _fetch_app_icon_data_uri(
    platform: str, serial: str, bundle_id: str, apk_path: Optional[str]
) -> Optional[str]:
    """The launcher icon as a validated PNG/WebP data URI, or ``None``.

    Reuses the same disk-cached icon backends as ``_handle_fetch_app_icon``.
    Swallows all errors (device disconnected, no icon) so a failed icon fetch
    never blocks the cloud save."""
    if not serial or not bundle_id:
        return None
    try:
        if platform == "ios":
            data = _ios_backend.fetch_app_icons(serial, [str(bundle_id)]).get(str(bundle_id))
        else:
            data = _android_backend.fetch_app_icon_bytes(serial, apk_path, bundle_id=str(bundle_id))
    except Exception:
        return None
    if not data:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        return None
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def _whole_device_app_cpu_values(
    snapshots: Sequence[MetricsSnapshot],
) -> List[float]:
    """Return app CPU on a whole-device 0-100 scale for live summaries.

    ``cpu_percent`` is a process-core sum where 100% means one fully busy
    logical CPU, so it is only comparable with whole-device system CPU after
    division by one stable logical-CPU width. Empty per-core samples are
    ignored, but contradictory non-empty widths invalidate the divisor. iOS
    may fall back to its core-normalized ``cpu_norm``; Android may not because
    that field is also frequency adjusted there.
    """
    widths = {
        len(snapshot.cpu.per_core_freq_mhz)
        for snapshot in snapshots
        if snapshot.cpu.per_core_freq_mhz
    }
    logical_cpu_count = next(iter(widths)) if len(widths) == 1 else None
    if logical_cpu_count is not None and not 0 < logical_cpu_count <= 1024:
        logical_cpu_count = None

    values: List[float] = []
    for snapshot in snapshots:
        value: Optional[float] = None
        raw = snapshot.cpu.cpu_percent
        count = snapshot.cpu.logical_core_count or logical_cpu_count
        if count is not None and count > 0 and raw is not None:
            raw_value = float(raw)
            if math.isfinite(raw_value):
                value = raw_value / count
        elif snapshot.device.platform == Platform.IOS and snapshot.cpu.cpu_norm is not None:
            normalized = float(snapshot.cpu.cpu_norm)
            if math.isfinite(normalized):
                value = normalized

        if value is not None:
            values.append(max(0.0, min(100.0, value)))
    return values


def _live_summary_from_snapshots(snapshots: List[MetricsSnapshot]) -> Dict[str, Any]:
    """Synthesize a summary dict from a live capture session so
    the existing ``excel_export`` writer can render it as a Summary Report.
    """
    fps_series: List[float] = [
        float(s.frames.fps_estimate) for s in snapshots if s.frames.fps_estimate is not None
    ]
    n = len(fps_series)
    sorted_fps = sorted(fps_series)
    median = sorted_fps[n // 2] if n else 0.0
    average = sum(fps_series) / n if n else 0.0
    max_fps = max(fps_series) if fps_series else 0.0

    # Stability ≈ % of samples within ±20% of the median (matches PD's _stability)
    if median:
        within = sum(1 for v in fps_series if median * 0.8 <= v <= median * 1.2)
        stability = round(100 * within / n, 1)
    else:
        stability = 0.0

    half = n // 2 if n else 0
    first_half = sum(fps_series[:half]) / half if half else 0.0
    second_half = sum(fps_series[half:]) / (n - half) if (n - half) else 0.0

    fps_std = 0.0
    if n > 1:
        mean = average
        var = sum((v - mean) ** 2 for v in fps_series) / n
        fps_std = round(var**0.5, 2)

    low_ratio = 0.0
    if max_fps:
        low = sum(1 for v in fps_series if v < max_fps * 0.9)
        low_ratio = round(100 * low / n, 2)

    # Temperature aggregates (start / end / max per zone seen at all)
    zone_first: Dict[str, float] = {}
    zone_last: Dict[str, float] = {}
    zone_max: Dict[str, float] = {}
    for snap in snapshots:
        for zone, val in (snap.thermal.zones_c or {}).items():
            try:
                v = float(val)
            except (TypeError, ValueError):
                continue
            zone_first.setdefault(zone, v)
            zone_last[zone] = v
            zone_max[zone] = max(zone_max.get(zone, v), v)
    temp_analysis = {
        zone: {"start": round(zone_first[zone], 1), "end": round(zone_last[zone], 1), "max": round(zone_max[zone], 1)}
        for zone in zone_first
    }

    cpu_values = _whole_device_app_cpu_values(snapshots)
    gpu_values = [
        float(s.gpu.busy_percent)
        for s in snapshots
        if s.gpu.busy_percent is not None
    ]
    # Power (mW) = |V(mV) × I(mA)| / 1000, averaged over the samples that report
    # both voltage and current.
    power_values = [
        abs(float(s.battery.voltage_mv) * float(s.battery.current_ma)) / 1000.0
        for s in snapshots
        if s.battery.voltage_mv is not None and s.battery.current_ma is not None
    ]
    power_avg = round(sum(power_values) / len(power_values), 1) if power_values else ""

    # Report the real elapsed wall-clock span of the collected samples (first →
    # last captured_at), not the sum of nominal per-sample intervals. The latter
    # equals sample_count × interval and drifts from the actual measurement time
    # whenever samples arrive irregularly or are dropped (the reported "Duration
    # differs from the real measurement time" bug). Falls back to the interval
    # sum for a single sample where there is no span.
    span_sec = captured_span_seconds(snapshots)
    duration_total_sec = span_sec if span_sec and span_sec > 0 else sum(s.duration_sec for s in snapshots)
    # Round the total to whole seconds BEFORE splitting, so a fractional
    # remainder that rounds up carries into minutes (119.6s -> 2m 0s, not 1m 60s).
    duration_total_sec = int(round(duration_total_sec))
    minutes = duration_total_sec // 60
    seconds = duration_total_sec % 60

    head = snapshots[0]
    abs_start = head.captured_at
    abs_end = snapshots[-1].captured_at if snapshots else abs_start

    from .analysis import _frame_summary
    frame_summary = _frame_summary([
        {"raw_frame_events": json.dumps(s.frames.raw_events),
         "raw_frame_events_dropped": str(s.frames.raw_events_dropped),
         "frame_p95_ms": str(s.frames.p95_ms), "frame_p99_ms": str(s.frames.p99_ms)}
        for s in snapshots
    ])
    return {
        "median": round(median, 1) if n else None,
        "median_stability": stability,
        "average": round(average, 1) if n else None,
        "average_stability": stability,
        "first_half": round(first_half, 1) if n else None,
        "first_half_stability": stability,
        "second_half": round(second_half, 1) if n else None,
        "second_half_stability": stability,
        "max_fps": round(max_fps, 1) if n else None,
        "fps_std": fps_std if n else None,
        "fps_below_90pct_ratio": low_ratio,
        "actual_measurement_time": (minutes, seconds),
        "cpu_usage": round(sum(cpu_values) / len(cpu_values), 1) if cpu_values else None,
        "gpu_usage": round(sum(gpu_values) / len(gpu_values), 1) if gpu_values else None,
        "power": power_avg,
        "abs_time_start": abs_start,
        "abs_time_end": abs_end,
        "fps_series": fps_series,
        "measurement": {
            "FPS source": ", ".join(sorted({s.frames.source for s in snapshots if s.frames.source})),
            "FPS scope": ", ".join(sorted({s.frames.scope for s in snapshots if s.frames.scope})),
            "CPU total source": ", ".join(sorted({s.cpu.total_source for s in snapshots if s.cpu.total_source})),
            "CPU total status": ", ".join(sorted({s.cpu.total_status for s in snapshots if s.cpu.total_status})),
            "Raw frames observed": frame_summary.raw_frame_count,
            "Raw frames dropped": frame_summary.raw_frames_dropped,
            "Raw frame sources": ", ".join(frame_summary.raw_frame_sources),
            "Raw frame scope": ", ".join(frame_summary.raw_frame_scope),
            "Session frame P50 (ms)": frame_summary.session_p50_ms,
            "Session frame P90 (ms)": frame_summary.session_p90_ms,
            "Session frame P95 (ms)": frame_summary.session_p95_ms,
            "Session frame P99 (ms)": frame_summary.session_p99_ms,
            "Worst window P95 (ms)": frame_summary.worst_window_p95_ms,
            "Worst window P99 (ms)": frame_summary.worst_window_p99_ms,
        },
        "temp_analysis": temp_analysis,
        "norm_fps_analysis": {"norm_fps_std": "", "norm_fps_avg": "", "norm_fps_max": ""},
        "metadata": {
            "model": head.device.model,
            "model_identifier": head.device.model_identifier or "",
            "model_number": head.device.model_number or "",
            "display_name": head.device.display_name or "",
            "os_ver": head.device.os_version,
            "build_version": head.device.build_version or "",
            "build_name": head.device.build_name or "",
            "package_name": head.package.bundle_id,
            "game_name": head.package.label or head.package.bundle_id,
            "game_ver": head.package.version_name or "",
            "date": abs_start,
        },
    }


def _handle_export_live_xlsx(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Render a Summary Report .xlsx for a live capture.

    Reuses ``mpi.reports.excel_export`` so the output matches what
    the offline pipeline produces. Uses the live device + package label as
    the file key (the section header in the workbook).
    """
    save_path = payload.get("save_path")
    snapshots_raw = payload.get("snapshots")
    if not save_path:
        raise ValueError("save_path required")
    if not isinstance(snapshots_raw, list) or not snapshots_raw:
        raise ValueError("snapshots must be a non-empty list")

    snapshots = [_snapshot_from_dict(s) for s in snapshots_raw]
    summary = _live_summary_from_snapshots(snapshots)
    from .session_cuts import validate_cuts
    from .report import _duration_total_sec
    cuts = validate_cuts(payload.get("cuts") or [], _duration_total_sec(snapshots))
    head = snapshots[0]
    file_key = f"{head.package.bundle_id} ({head.device.model}).xlsx"

    # Surface the battery temperature in the report's Temperature(°C) section.
    # The Temperature columns are fed from the thermal map keyed to match the
    # summary's file key (dirname("") == dirname("")), so build a one-entry map
    # from the per-zone start/end/max of the "battery" thermal zone.
    battery_temp = (summary.get("temp_analysis") or {}).get("battery")
    results_thermal: Dict[str, Any] = {}
    if battery_temp:
        results_thermal[file_key] = {
            "start_temp": battery_temp.get("start", ""),
            "end_temp": battery_temp.get("end", ""),
            "max_temp": battery_temp.get("max", ""),
        }

    export_data_to_excel(save_path, {file_key: summary}, results_thermal)
    if cuts:
        from openpyxl import load_workbook
        workbook = load_workbook(save_path)
        sheet = workbook.create_sheet("Session Cuts")
        sheet.append(["Name", "Start (sec)", "End (sec)", "Duration (sec)"])
        for cut in cuts:
            sheet.append([cut["name"], cut["start_sec"], cut["end_sec"], cut["end_sec"] - cut["start_sec"]])
            # A user-provided cut name is text, including a leading '='.
            sheet.cell(sheet.max_row, 1).data_type = "s"
        sheet.column_dimensions["A"].width = 30
        for column in ("B", "C", "D"):
            sheet.column_dimensions[column].width = 18
        workbook.save(save_path)
        workbook.close()
    return {"save_path": save_path, "count": len(snapshots)}


# --- Google Play companion handlers --------------------------------------


def _handle_companion_status(payload: Dict[str, Any]) -> Dict[str, Any]:
    serial = payload.get("serial")
    if not serial:
        raise ValueError("serial required")
    from .android import companion

    return companion.status(str(serial))


def _handle_companion_app_list(payload: Dict[str, Any]) -> Dict[str, Any]:
    serial = payload.get("serial")
    if not serial:
        raise ValueError("serial required")
    from .android import companion

    apps = companion.fetch_app_list(str(serial), user_only=bool(payload.get("user_only", True)))
    # Companion labels are authoritative PackageManager display names.  Persist
    # them in the same cache used by capture startup and legacy CSV recovery;
    # previously they only lived in React state, so saved snapshots lost them.
    for app in apps:
        if isinstance(app, dict):
            bundle_id = str(app.get("package") or "")
            label = app.get("label")
            if bundle_id and isinstance(label, str):
                cache_app_label(bundle_id, label)
    return {"serial": serial, "apps": apps}


def _handle_companion_icons(payload: Dict[str, Any]) -> Dict[str, Any]:
    serial = payload.get("serial")
    packages = payload.get("packages")
    if not serial or not isinstance(packages, list):
        raise ValueError("serial and packages[] required")
    from .android import companion

    icons = companion.fetch_icons(
        str(serial), [str(p) for p in packages], size=int(payload.get("size") or 96)
    )
    # Companion rasterizes adaptive icons that the APK unzip fallback cannot.
    # Cache those bytes so auto-save and old session rows can reuse the exact
    # icon even after the device disconnects.
    for bundle_id, encoded in icons.items():
        if not isinstance(encoded, str) or not encoded:
            continue
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error):
            continue
        cache_app_icon(str(bundle_id), data)
    return {"serial": serial, "icons": icons}


def _handle_companion_metrics(payload: Dict[str, Any]) -> Dict[str, Any]:
    serial = payload.get("serial")
    if not serial:
        raise ValueError("serial required")
    from .android import companion

    return {"serial": serial, "metrics": companion.fetch_metrics(str(serial), package=payload.get("package"))}


def _handle_companion_capabilities(payload: Dict[str, Any]) -> Dict[str, Any]:
    serial = payload.get("serial")
    if not serial:
        raise ValueError("serial required")
    from .android import companion

    return {"serial": serial, "capabilities": companion.fetch_capabilities(str(serial))}


def _capture_running_on(serial: str) -> bool:
    with _captures_lock:
        return any(ctx.serial == serial for ctx in _active_captures.values())


def _handle_companion_overlay_status(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Can this device show the on-device HUD? Answers instead of raising for a
    missing or pre-overlay companion — the desktop uses this to decide whether to
    offer the toggle at all, so "no" is data, not a failure."""
    serial = payload.get("serial")
    if not serial:
        raise ValueError("serial required")
    serial = str(serial)
    from .android import companion

    # Never cold-start the companion while this device is being measured. A cold
    # connect runs `am start` on the companion activity, which takes the measured
    # app out of the foreground; the Android backend then ends the capture after
    # six samples where it is not the resumed activity. The desktop probes this
    # exactly when the user reaches for the toggle mid-capture, so the guard
    # belongs here rather than in the caller.
    allow_cold_start = payload.get("connect") is True and not _capture_running_on(serial)
    return companion.overlay_support(serial, allow_cold_start=allow_cold_start)


def _handle_set_capture_overlay(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Toggle the HUD (and its metric selection) while a capture runs.

    Deliberately takes no device reservation: _device_busy_locked is true by
    definition here, because the capture this targets is the thing holding the
    device."""
    capture_id = payload.get("capture_id")
    if not capture_id:
        raise ValueError("capture_id required")
    capture_id = str(capture_id)
    enabled = payload.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean")
    metrics = _parse_overlay_metrics(payload.get("metrics"))

    with _captures_lock:
        context = _active_captures.get(capture_id)
    if context is None:
        raise ValueError("unknown capture_id")

    reason: Optional[str] = None
    # The renderer fires this fire-and-forget on every toggle with no debounce,
    # onto a multi-worker pool, so two calls for one capture overlap routinely
    # (switch on, then untick a metric). Everything that reads or replaces the
    # overlay slot happens under this per-capture lock, which makes the slow open
    # below the only one in flight; the read of the slot itself still has to be
    # taken under _captures_lock, because the pump epilogue clears it from the
    # capture thread without ever taking overlay_lock.
    with context.overlay_lock:
        with _captures_lock:
            live = _active_captures.get(capture_id) is context
            overlay = context.overlay if live else None
        if not live:
            raise ValueError("unknown capture_id")

        if overlay is not None and not getattr(overlay, "alive", True):
            # Already torn down under us — by the epilogue racing this toggle, or
            # by a run of failed pushes. Closing disables the HUD on the way out,
            # so there is nothing left to reconfigure, and reconnecting to a
            # capture that is ending would only strand another forward rule.
            overlay = None
            if enabled:
                reason = "companion_unavailable"

        if overlay is None:
            if enabled and reason is None:
                # Warm connect only: cold-starting the companion now would
                # foreground it over the measured app and end the capture as
                # backgrounded.
                package = getattr(context.session, "package", None)
                overlay = _open_overlay_session(
                    context.platform,
                    context.serial,
                    str(getattr(package, "bundle_id", "") or ""),
                    {"enabled": True, "metrics": metrics},
                    allow_cold_start=False,
                    session_id=context.mobile_session_id,
                    label=getattr(package, "label", None),
                )
                if overlay is None:
                    reason = "companion_unavailable"
                elif not _publish_overlay(capture_id, context, overlay):
                    # The capture ended during the connect; this session is ours
                    # to close, or its forward rule outlives the sidecar.
                    _close_overlay(overlay)
                    raise ValueError("unknown capture_id")
        else:
            try:
                overlay.configure(enabled=enabled, metrics=metrics)  # type: ignore[attr-defined]
            except Exception as exc:
                logger.info("device overlay toggle failed for %s: %s", capture_id, exc)
                reason = "companion_unavailable"

    effective = False
    if overlay is not None and reason is None:
        effective = bool(getattr(overlay, "hud_enabled", False))
        if enabled and not effective:
            # The companion accepted the request but cannot draw: the user
            # revoked "Display over other apps" after we granted the appop.
            reason = "overlay_permission_denied"
    return {
        "capture_id": capture_id,
        "enabled": effective,
        "metrics": metrics,
        "reason": reason,
    }


def _handle_sync_companion_cloud(payload: Dict[str, Any], generation: int | None = None) -> Dict[str, int]:
    from .android.companion_cloud import bridge
    try:
        return bridge.sync(payload.get("session"), _backend_for_platform("android").list_devices, generation)
    except Exception:
        # Internal auth failures return only counts, never token-bearing payloads.
        bridge.clear()
        return {"connected": 0, "signed_in": 0, "unavailable": 1}


_HANDLERS: Dict[str, Callable[[Dict[str, Any]], Any]] = {
    "sync_companion_cloud": _handle_sync_companion_cloud,
    "ping": _handle_ping,
    "companion_status": _handle_companion_status,
    "companion_app_list": _handle_companion_app_list,
    "companion_icons": _handle_companion_icons,
    "companion_metrics": _handle_companion_metrics,
    "companion_capabilities": _handle_companion_capabilities,
    "companion_overlay_status": _handle_companion_overlay_status,
    "load_file": _handle_load_file,
    "list_saved_sessions": _handle_list_saved_sessions,
    "delete_saved_sessions": _handle_delete_saved_sessions,
    "import_session_files": _handle_import_session_files,
    "load_session_analyses": _handle_load_session_analyses,
    "list_devices": _handle_list_devices,
    "play_memory_qa_sample": _handle_play_memory_qa_sample,
    "play_memory_qa_summary": _handle_play_memory_qa_summary,
    "ios_preflight": _handle_ios_preflight,
    "ios_install_tool": _handle_ios_install_tool,
    "ios_temperature_guard_preflight": _handle_ios_temperature_guard_preflight,
    "ios_temperature_guard_install_tool": _handle_ios_temperature_guard_install_tool,
    "prepare_wireless": _handle_prepare_wireless,
    "list_packages": _handle_list_packages,
    "fetch_app_icon": _handle_fetch_app_icon,
    "fetch_app_icons": _handle_fetch_app_icons,
    "fetch_app_label": _handle_fetch_app_label,
    "fetch_app_labels": _handle_fetch_app_labels,
    "start_capture": _handle_start_capture,
    "stop_capture": _handle_stop_capture,
    "pause_capture": _handle_pause_capture,
    "resume_capture": _handle_resume_capture,
    "save_session_cuts": _handle_save_session_cuts,
    "set_capture_overlay": _handle_set_capture_overlay,
    "create_temperature_preview": _handle_create_temperature_preview,
    "activate_temperature_preview": _handle_activate_temperature_preview,
    "stop_temperature_preview": _handle_stop_temperature_preview,
    "create_temperature_guard": _handle_create_temperature_guard,
    "activate_temperature_guard": _handle_activate_temperature_guard,
    "stop_temperature_guard": _handle_stop_temperature_guard,
    "export_live_csv": _handle_export_live_csv,
    "export_live_xlsx": _handle_export_live_xlsx,
    "auto_save_raw_csv": _handle_auto_save_raw_csv,
    "build_endpoint_payload": _handle_build_endpoint_payload,
}


# --- Main loop ------------------------------------------------------------


def _dispatch(request: Dict[str, Any], companion_generation: int | None = None) -> Dict[str, Any]:
    cmd = request.get("command")
    rid = request.get("id")
    payload = request.get("payload") or {}

    if cmd == "shutdown":
        _emit({"id": rid, "ok": True, "result": {"bye": True}})
        raise SystemExit(0)

    handler = _HANDLERS.get(cmd or "")
    if handler is None:
        return {"id": rid, "ok": False, "error": f"unknown command: {cmd!r}"}

    try:
        result = (_handle_sync_companion_cloud(payload, companion_generation)
                  if cmd == "sync_companion_cloud" else handler(payload))
        return {"id": rid, "ok": True, "result": result}
    except Exception as exc:
        logger.exception("handler %s failed", cmd)
        return {
            "id": rid,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(limit=5),
        }


def main() -> int:
    # Force UTF-8 on stdin/stdout. The Electron main process writes JSON in
    # UTF-8, but Python on Windows defaults stdin to the active code page
    # (e.g. cp949 in Korean locales), which corrupts Korean / Chinese / etc.
    # paths into surrogateescape sequences like ``\udcec``.
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="strict")  # type: ignore[union-attr]
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")  # type: ignore[union-attr]
    except (AttributeError, OSError):
        pass

    logging.basicConfig(
        level=os.environ.get("MPI_LOG", "INFO"),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger.info("mpi.api ready")

    # Each command runs on a worker thread so independent requests (e.g.
    # parallel `fetch_app_icon` calls for the package dropdown) don't queue
    # behind a slow handler. _emit and _active_captures already serialize via
    # their own locks, so the only constraint is that `shutdown` must not be
    # racing with itself — handled by raising SystemExit synchronously below.
    pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="api-worker")
    shutdown_signal: Dict[str, bool] = {"value": False}
    pending_lock = threading.Lock()
    pending: List = []  # active futures

    def _on_done(fut) -> None:  # type: ignore[no-untyped-def]
        with pending_lock:
            try:
                pending.remove(fut)
            except ValueError:
                pass

    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError as exc:
                _emit({"id": None, "ok": False, "error": f"invalid JSON: {exc}"})
                continue
            if not isinstance(request, dict):
                _emit({"id": None, "ok": False, "error": "request must be a JSON object"})
                continue

            # Shutdown is synchronous so we don't race with in-flight workers.
            if request.get("command") == "shutdown":
                try:
                    _dispatch(request)  # emits its own response via SystemExit path
                except SystemExit:
                    pass
                shutdown_signal["value"] = True
                break

            companion_generation = None
            if request.get("command") == "sync_companion_cloud":
                from .android.companion_cloud import bridge
                payload = request.get("payload")
                if not isinstance(payload, dict) or payload.get("session") is None:
                    # Revocation must overtake queued work and fence it at stdin arrival.
                    _emit(_dispatch(request))
                    continue
                companion_generation = bridge.reserve()

            def _run(req: Dict[str, Any], generation: int | None) -> None:
                try:
                    response = _dispatch(req, generation)
                except SystemExit:
                    shutdown_signal["value"] = True
                    return
                _emit(response)

            fut = pool.submit(_run, request, companion_generation)
            with pending_lock:
                pending.append(fut)
            fut.add_done_callback(_on_done)

            if not _stdout_alive:
                break
    except (OSError, KeyboardInterrupt):
        # Stdin pipe closed by Electron killing us, or user interrupt — both
        # are normal shutdown signals; never raise to the PyInstaller bootloader.
        pass

    from .android.companion_cloud import bridge
    bridge.clear()
    # Drain any in-flight workers before tearing down captures.
    pool.shutdown(wait=True, cancel_futures=False)
    _shutdown_captures()
    return 0


def _shutdown_captures() -> None:
    """Stop resources and give worker threads a bounded teardown window.

    In particular, an iOS guard may still be inside ``create_temperature_monitor``
    with its sidecar local to that worker.  Signalling and immediately returning
    from the data engine can orphan that child.  A bounded join lets the 100 ms
    stop checks close Diagnostics/DVT normally; Electron retains a process-tree
    kill fallback for a genuinely wedged transport.
    """
    with _captures_lock:
        preview_ids = list(_active_temperature_previews)
        preview_contexts = list(_active_temperature_previews.values())
        guard_ids = list(_active_temperature_guards)
        guard_contexts = list(_active_temperature_guards.values())
        ctxs = list(_active_captures.values())
        for ctx in ctxs:
            # Shutdown is an app-preserving stop and must win over a pending
            # Timer callback that has not yet claimed automatic teardown.
            if ctx.stop_reason is None:
                ctx.stop_reason = "shutdown"
            if ctx.auto_stop_timer is not None:
                ctx.auto_stop_timer.cancel()
            ctx.stop.set()
    for preview_id in preview_ids:
        _handle_stop_temperature_preview({"preview_id": preview_id})
    for guard_id in guard_ids:
        _handle_stop_temperature_guard({"guard_id": guard_id})
    for ctx in ctxs:
        _close_capture_context(ctx)

    current = threading.current_thread()
    threads: List[threading.Thread] = []
    seen: set[int] = set()
    for context in [*preview_contexts, *guard_contexts]:
        if context.thread is not None and id(context.thread) not in seen:
            seen.add(id(context.thread))
            threads.append(context.thread)
    for context in ctxs:
        if id(context.thread) not in seen:
            seen.add(id(context.thread))
            threads.append(context.thread)

    deadline = time.monotonic() + SHUTDOWN_JOIN_TIMEOUT_SEC
    for thread in threads:
        if thread is current or not thread.is_alive():
            continue
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    still_alive = [thread.name for thread in threads if thread.is_alive()]
    if still_alive:
        logger.warning(
            "capture teardown timed out; process owner must terminate the tree: %s",
            ", ".join(still_alive),
        )


if __name__ == "__main__":
    sys.exit(main())
