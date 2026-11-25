"""Canonical session-analysis calculation path.

This module is the single source of truth for every aggregate the desktop app
shows in the Analysis Dashboard. React, the CSV summary section, and the XLSX
summary all consume :class:`SessionAnalysis` produced here so no two surfaces
recompute an average or a median differently.

Design rules (from the dashboard spec):

* Aggregate **finite values only** — ``None``, empty cells, ``NaN`` and ``inf``
  are dropped. A measured ``0`` is a real value and is kept.
* ``coverage_percent = 100 * valid_count / total_count`` where ``total_count`` is
  the session sample count. Below 80% the UI shows a "partial data" warning.
* A single valid sample => percentiles equal that value and the population
  standard deviation is ``0``.
* No valid samples => every derived statistic is ``None`` (rendered as "—").
* Percentiles are inclusive (``statistics.quantiles(n=100, method="inclusive")``):
  p05 = cut point 5, p95 = 95, p99 = 99. Standard deviation is the *population*
  deviation (``statistics.pstdev``). Median is ``statistics.median`` (mean of the
  two middle values on an even count).

The parser preserves null-vs-measured-0 by reading each cell through
:func:`_float_or_none`; it never coerces an empty cell to ``0``. It supports the
MPI v2 CSV, the MPI v1 CSV, and the header-less (no A1/A2 identity) 3-section
live CSV.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import math
import os
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .android.chart_memory import SOURCE as ANDROID_MEMORY_SOURCE, parse_replay_memory
from .report import LEGACY_MPI_MARKER, MPI_MARKER
from .session_cuts import read_cuts


# --- Column / metadata name constants (must match report.py writer) ---------

COL_CAPTURED_AT = "captured_at"
COL_DURATION = "duration_sec"
COL_FPS = "fps"
COL_JANK_PERCENT = "jank_percent"
COL_FRAMES_TOTAL = "frames_total"
COL_FRAMES_JANKY = "frames_janky"
COL_FRAMES_SMALL = "frames_small_jank"
COL_FRAMES_BIG = "frames_big_jank"
COL_FRAME_P50 = "frame_p50_ms"
COL_FRAME_P90 = "frame_p90_ms"
COL_FRAME_P95 = "frame_p95_ms"
COL_FRAME_P99 = "frame_p99_ms"
# v2 additions (written by report.py once the CSV is bumped; read defensively).
COL_FRAMES_MISSED_VSYNC = "frames_missed_vsync"
COL_FRAMES_SLOW_UI = "frames_slow_ui_thread"
COL_FRAMES_DEADLINE_MISSED = "frames_deadline_missed"
COL_CPU_PERCENT = "cpu_percent"
COL_CPU_TOTAL_PERCENT = "cpu_total_percent"
COL_CPU_NORM = "cpu_norm"
COL_GPU_BUSY = "gpu_busy_percent"
COL_GPU_CLOCK = "gpu_clock_mhz"
COL_GPU_TEMP = "gpu_temp_c"
COL_BATT_LEVEL = "battery_level_percent"
COL_BATT_CURRENT = "battery_current_ma"
COL_BATT_VOLTAGE = "battery_voltage_mv"
COL_MEM_PSS = "mem_pss_kb"
COL_MEM_RSS = "mem_rss_kb"
COL_MEM_SYS_TOTAL = "mem_sys_total_kb"
COL_MEM_CODE = "mem_code_kb"
COL_MEM_STACK = "mem_stack_kb"

META_SCHEMA_VERSION = "schema.version"
META_SESSION_ID = "session.id"
META_SESSION_NAME = "session.name"
META_PLATFORM = "device.platform"
META_MODEL = "device.model"
META_MODEL_IDENTIFIER = "device.model_identifier"
META_MODEL_NUMBER = "device.model_number"
META_DISPLAY_NAME = "device.display_name"
META_OS_VERSION = "device.os_version"
META_BUILD_VERSION = "device.build_version"
META_BUILD_NAME = "device.build_name"
META_SERIAL_HASH = "device.serial_hash"
META_CODE_NAME = "device.code_name"
META_HW_VER = "device.hw_ver"
META_BUNDLE_ID = "package.bundle_id"
META_LABEL = "package.label"
META_ICON = "package.icon"
META_VERSION_NAME = "package.version_name"
META_STARTED_AT = "capture.started_at"
META_ENDED_AT = "capture.ended_at"

# Current CSV schema version. v1/markerless files report is_legacy=True.
SCHEMA_VERSION = 2

_THERMAL_ZONE_RE = re.compile(r"^thermal_(?P<zone>.+)_c$")
_BATTERY_ZONE_RE = re.compile(r"batt", re.IGNORECASE)
_MAX_CACHED_ICON_BYTES = 2 * 1024 * 1024
_MAX_ICON_DATA_URI_CHARS = 3 * 1024 * 1024
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _safe_bundle_cache_name(bundle_id: str) -> str:
    """Match the platform backends' bundle-id-to-cache-file mapping."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", bundle_id)


def _validated_metadata_icon(value: Optional[str]) -> Optional[str]:
    """Accept only the bounded PNG/WebP data URIs written by this application.

    Session CSVs can be imported from arbitrary locations.  Passing an arbitrary
    URI from a CSV straight into an ``img`` element would make the session list a
    surprising external-content loader; malformed or oversized values are
    therefore ignored and the normal cache/placeholder fallback is used.
    """
    icon = (value or "").strip()
    if not icon or len(icon) > _MAX_ICON_DATA_URI_CHARS:
        return None
    match = re.fullmatch(
        r"data:image/(?P<mime>png|webp);base64,(?P<data>[A-Za-z0-9+/]*={0,2})",
        icon,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    try:
        raw = base64.b64decode(match.group("data"), validate=True)
    except (ValueError, base64.binascii.Error):
        return None
    mime = match.group("mime").lower()
    if mime == "png" and not raw.startswith(_PNG_MAGIC):
        return None
    if mime == "webp" and not (len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"):
        return None
    return icon


def _cached_app_label(platform: str, bundle_id: str) -> Optional[str]:
    """Best-effort friendly-name recovery for older CSVs without package.label."""
    if not bundle_id:
        return None
    cache_dir = "ios-labels" if platform == "ios" else "labels"
    path = os.path.join(
        os.path.expanduser("~"),
        ".cache",
        "mpi",
        cache_dir,
        _safe_bundle_cache_name(bundle_id) + ".txt",
    )
    try:
        with open(path, "r", encoding="utf-8") as fh:
            label = fh.read(512).strip()
    except (OSError, UnicodeError):
        return None
    return re.sub(r"\s+", " ", label).strip() or None


def _cached_app_icon(platform: str, bundle_id: str) -> Optional[str]:
    """Return a bounded cached launcher icon as a self-contained data URI."""
    if not bundle_id:
        return None
    cache_dir = "ios-icons" if platform == "ios" else "icons"
    path = os.path.join(
        os.path.expanduser("~"),
        ".cache",
        "mpi",
        cache_dir,
        _safe_bundle_cache_name(bundle_id) + ".png",
    )
    try:
        size = os.path.getsize(path)
        if size <= 0 or size > _MAX_CACHED_ICON_BYTES:
            return None
        with open(path, "rb") as fh:
            raw = fh.read(_MAX_CACHED_ICON_BYTES + 1)
    except OSError:
        return None
    if raw.startswith(_PNG_MAGIC):
        mime = "image/png"
    elif len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        return None
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _resolved_app_identity(metadata: Dict[str, str]) -> Tuple[Optional[str], Optional[str]]:
    """Metadata-first app identity with an offline cache fallback.

    New CSVs are self-contained.  Legacy files predate package.label/icon, so
    the persistent caches populated by the device/app selector are the only
    accurate offline source available; callers still retain bundle_id separately
    when neither cache entry exists.
    """
    platform = (metadata.get(META_PLATFORM) or "android").strip().lower()
    bundle_id = (metadata.get(META_BUNDLE_ID) or "").strip()
    label = (metadata.get(META_LABEL) or "").strip() or _cached_app_label(platform, bundle_id)
    icon = _validated_metadata_icon(metadata.get(META_ICON)) or _cached_app_icon(platform, bundle_id)
    return label or None, icon


_CPU_CORE_FREQ_RE = re.compile(r"^cpu(?P<index>\d+)_mhz$")


# --- MetricStats ------------------------------------------------------------


@dataclass
class MetricStats:
    """Aggregate statistics for one numeric metric over a session.

    ``average``/``median``/``min``/``max``/``p05``/``p95``/``p99``/``stddev`` are
    ``None`` when no finite sample exists. ``coverage_percent`` reflects how many
    of the session's samples carried this metric.
    """

    valid_count: int
    total_count: int
    coverage_percent: float
    average: Optional[float]
    median: Optional[float]
    min: Optional[float]
    max: Optional[float]
    p05: Optional[float]
    p95: Optional[float]
    p99: Optional[float]
    stddev: Optional[float]


def _finite(v: Any) -> bool:
    """True only for a real finite number. Bools and NaN/inf are rejected."""
    return (
        isinstance(v, (int, float))
        and not isinstance(v, bool)
        and math.isfinite(float(v))
    )


def _percentiles(nums: Sequence[float]) -> Tuple[float, float, float]:
    """Inclusive p05/p95/p99 for a non-empty list. A single value maps to itself."""
    if len(nums) == 1:
        only = float(nums[0])
        return only, only, only
    qs = statistics.quantiles(nums, n=100, method="inclusive")
    # 99 cut points, 0-indexed: p05 -> qs[4], p95 -> qs[94], p99 -> qs[98].
    return qs[4], qs[94], qs[98]


def compute_metric_stats(
    values: Sequence[Optional[float]], total_count: Optional[int] = None
) -> MetricStats:
    """Compute a :class:`MetricStats` from a column of (possibly null) values.

    ``total_count`` is the denominator for coverage (the session sample count);
    it defaults to ``len(values)`` when omitted. Non-finite entries are dropped.
    """
    nums = [float(v) for v in values if _finite(v)]
    total = total_count if total_count is not None else len(values)
    valid = len(nums)
    coverage = (100.0 * valid / total) if total > 0 else 0.0
    if valid == 0:
        return MetricStats(
            valid_count=0,
            total_count=total,
            coverage_percent=coverage,
            average=None,
            median=None,
            min=None,
            max=None,
            p05=None,
            p95=None,
            p99=None,
            stddev=None,
        )
    p05, p95, p99 = _percentiles(nums)
    return MetricStats(
        valid_count=valid,
        total_count=total,
        coverage_percent=coverage,
        average=statistics.fmean(nums),
        median=statistics.median(nums),
        min=min(nums),
        max=max(nums),
        p05=p05,
        p95=p95,
        p99=p99,
        stddev=statistics.pstdev(nums),  # 0.0 for a single sample
    )


# --- Nested summaries -------------------------------------------------------


@dataclass
class ZoneTempStats:
    zone: str
    start: Optional[float]
    end: Optional[float]
    max: Optional[float]
    change: Optional[float]


@dataclass
class FrameSummary:
    """Frame/jank rollup. Weighted jank = Σjanky / Σtotal × 100 over samples with
    ``total_frames > 0``. Frame-time percentiles are *worst-window* maxima of the
    per-sample windowed percentiles, not a session-wide distribution."""

    total_frames: int
    janky_frames: int
    small_jank: int
    big_jank: int
    weighted_jank_percent: Optional[float]
    worst_window_p95_ms: Optional[float]
    worst_window_p99_ms: Optional[float]
    missed_vsync: Optional[int]
    slow_ui_thread: Optional[int]
    frame_deadline_missed: Optional[int]
    raw_frame_count: int = 0
    raw_frames_dropped: int = 0
    session_p50_ms: Optional[float] = None
    session_p90_ms: Optional[float] = None
    session_p95_ms: Optional[float] = None
    session_p99_ms: Optional[float] = None
    raw_frame_sources: List[str] = field(default_factory=list)
    raw_frame_scope: List[str] = field(default_factory=list)


@dataclass
class SessionListItem:
    session_id: str
    file_name: str
    file_path: str
    is_legacy: bool
    schema_version: Optional[int]
    session_name: Optional[str]
    app_label: Optional[str]
    app_icon: Optional[str]
    bundle_id: str
    app_version: Optional[str]
    device_model: str
    device_display_name: Optional[str]
    platform: str
    os_version: str
    serial_hash: str
    started_at: Optional[str]
    ended_at: Optional[str]
    duration_sec: float
    sample_count: int
    modified_at: float
    cuts: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class SessionAnalysis:
    # --- identity / list metadata ---
    session_id: str
    file_name: str
    file_path: str
    is_legacy: bool
    schema_version: Optional[int]
    session_name: Optional[str]
    app_label: Optional[str]
    app_icon: Optional[str]
    bundle_id: str
    app_version: Optional[str]
    device_model: str
    device_display_name: Optional[str]
    platform: str
    os_version: str
    code_name: str
    hw_ver: str
    serial_hash: str
    started_at: Optional[str]
    ended_at: Optional[str]
    duration_sec: float
    sample_count: int
    # --- aggregate metric stats (keyed by canonical metric name) ---
    metrics: Dict[str, MetricStats]
    fps_stability_percent: Optional[float]
    frames: FrameSummary
    # --- temperature ---
    temp_zones: List[ZoneTempStats]
    battery_zone: Optional[str]
    max_temp_battery: Optional[float]
    hottest_sensor_zone: Optional[str]
    hottest_sensor_max: Optional[float]
    # --- memory / battery context ---
    peak_app_memory_mb: Optional[float]
    memory_ratio_percent: Optional[float]
    battery_start_level: Optional[float]
    battery_end_level: Optional[float]
    battery_change_pp: Optional[float]
    # --- nullable time series for charts ---
    timeseries: Dict[str, List[Optional[float]]]
    cuts: List[Dict[str, Any]] = field(default_factory=list)
    metric_provenance: Dict[str, Any] = field(default_factory=dict)
    device_build_version: str = ""
    device_build_name: str = ""
    device_model_identifier: str = ""
    device_model_number: str = ""
    memory_source: Optional[str] = None
    memory_reference_context: Dict[str, Any] = field(default_factory=dict)


# --- CSV section reading ----------------------------------------------------


def _float_or_none(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _int_or_none(value: Any) -> Optional[int]:
    f = _float_or_none(value)
    return int(f) if f is not None else None


def _read_sections(path: str) -> Tuple[Dict[str, str], List[str], List[Dict[str, str]]]:
    """Parse the 3-section live CSV, preserving empty cells as empty strings.

    Returns ``(metadata, timeseries_header, timeseries_rows)``. Any MPI A1/A2
    identity rows preceding the first section are ignored. Raises
    :class:`ValueError` when the file is not a 3-section live CSV.
    """
    csv.field_size_limit(max(csv.field_size_limit(), 16 * 1024 * 1024))
    metadata: Dict[str, str] = {}
    header: List[str] = []
    rows: List[Dict[str, str]] = []
    section: Optional[str] = None
    waiting_header = False

    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
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
                        continue
                    if header:
                        padded = row + [""] * max(0, len(header) - len(row))
                        rows.append(dict(zip(header, padded)))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ValueError(f"could not read CSV: {exc}") from exc

    if not metadata or not header or not rows:
        raise ValueError("not a 3-section live CSV export")
    return metadata, header, rows


def _column(rows: Sequence[Dict[str, str]], name: str) -> List[Optional[float]]:
    return [_float_or_none(r.get(name)) for r in rows]


def _clamp_percent(value: Optional[float]) -> Optional[float]:
    """Return one finite percentage constrained to the dashboard's 0..100 scale."""
    if value is None or not math.isfinite(value):
        return None
    return max(0.0, min(100.0, value))


def _cpu_core_count(header: Sequence[str]) -> Optional[int]:
    """Infer the capture's stable logical-CPU count from dynamic CSV columns.

    The report writer emits one position-anchored ``cpuN_mhz`` column per
    logical CPU, using the maximum snapshot width for the whole session.  Use
    that stable header width rather than counting populated cells per row:
    offline cores and older captures can leave individual frequency cells
    empty, but that must not change the CPU normalization divisor mid-session.
    A sparse/malformed sequence is not trustworthy and deliberately falls back
    to the saved normalized metric instead.
    """
    indices = sorted(
        int(match.group("index"))
        for name in header
        if (match := _CPU_CORE_FREQ_RE.fullmatch(name)) is not None
    )
    if not indices or indices != list(range(len(indices))):
        return None
    return len(indices)


def _dashboard_cpu_series(
    rows: Sequence[Dict[str, str]], header: Sequence[str], *, platform: str
) -> Tuple[
    List[Optional[float]],
    List[Optional[float]],
    List[Optional[float]],
]:
    """Build bounded CPU series for dashboard statistics and charts.

    ``cpu_percent`` in the CSV intentionally remains the platform's raw app
    CPU sum and may exceed 100 when several cores/threads are busy.  The
    dashboard's canonical app metric is instead normalized to whole-device CPU
    capacity.  Dynamic ``cpuN_mhz`` columns provide the stable logical-CPU
    divisor on Android; iOS has no per-core frequency columns, so its already
    core-normalized ``cpu_norm`` is the canonical fallback, but only for iOS:
    Android stores a frequency-weighted estimate under the same legacy field,
    which is not interchangeable with whole-device app utilization.  Captures
    without a trustworthy platform-specific source remain unavailable rather
    than exposing a core/thread-count-dependent raw percentage as though it
    were a whole-device percentage.

    ``cpu_norm`` retains a valid saved frequency-normalized value when present.
    Only iOS may fall back to its canonical app series when that saved value is
    absent; Android's core-only app utilization is not the same metric as its
    frequency-weighted normalization. System CPU is specified by the CSV
    contract as a whole-device percentage and only needs validation/clamping.
    No source row is mutated.
    """
    core_count = _cpu_core_count(header)
    is_ios = platform.strip().lower() == "ios"
    raw_app = _column(rows, COL_CPU_PERCENT)
    saved_norm = _column(rows, COL_CPU_NORM)
    raw_system = _column(rows, COL_CPU_TOTAL_PERCENT)

    app: List[Optional[float]] = []
    norm: List[Optional[float]] = []
    system: List[Optional[float]] = []
    for row, raw_value, norm_value, system_value in zip(rows, raw_app, saved_norm, raw_system):
        recorded_cores = _int_or_none(row.get("cpu_logical_core_count"))
        row_core_count = recorded_cores if recorded_cores is not None and recorded_cores > 0 else core_count
        if raw_value is not None and row_core_count is not None:
            app_value = _clamp_percent(raw_value / row_core_count)
        elif core_count is None and is_ios and norm_value is not None:
            app_value = _clamp_percent(norm_value)
        else:
            app_value = None

        saved_norm_value = _clamp_percent(norm_value)
        app.append(app_value)
        norm.append(
            saved_norm_value
            if saved_norm_value is not None
            else app_value if is_ios else None
        )
        system.append(_clamp_percent(system_value))

    return app, system, norm


def _first_last_valid(values: Sequence[Optional[float]]) -> Tuple[Optional[float], Optional[float]]:
    first = next((v for v in values if _finite(v)), None)
    last = next((v for v in reversed(values) if _finite(v)), None)
    return first, last


def _span_seconds(rows: Sequence[Dict[str, str]]) -> Optional[float]:
    """Wall-clock seconds between the first and last parseable ``captured_at``.

    Uses the real timestamp span so irregular sampling / dropped samples do not
    distort the duration (unlike summing per-sample intervals)."""
    active = [_float_or_none(r.get("elapsed_sec")) for r in rows]
    if len(active) >= 2 and all(v is not None for v in active):
        return active[-1] - active[0]
    stamps = [r.get(COL_CAPTURED_AT, "") for r in rows]
    parsed: List[Tuple[int, datetime]] = []
    for i, s in enumerate(stamps):
        try:
            parsed.append((i, datetime.fromisoformat(s)))
        except (ValueError, TypeError):
            continue
    if len(parsed) < 2:
        return None
    return (parsed[-1][1] - parsed[0][1]).total_seconds()


def _time_axis(rows: Sequence[Dict[str, str]]) -> List[Optional[float]]:
    """Seconds elapsed from the first sample for each row.

    Prefers real timestamps (offset from the first parseable ``captured_at``);
    falls back to the cumulative sum of per-sample ``duration_sec`` when
    timestamps are unusable."""
    active = [_float_or_none(r.get("elapsed_sec")) for r in rows]
    if active and all(v is not None for v in active):
        return [v - active[0] for v in active]
    base: Optional[datetime] = None
    axis: List[Optional[float]] = []
    usable = 0
    for r in rows:
        dt: Optional[datetime] = None
        try:
            dt = datetime.fromisoformat(r.get(COL_CAPTURED_AT, ""))
        except (ValueError, TypeError):
            dt = None
        if dt is not None:
            if base is None:
                base = dt
            axis.append((dt - base).total_seconds())
            usable += 1
        else:
            axis.append(None)
    if usable >= 2:
        return axis
    # Fallback: cumulative nominal intervals.
    cumulative = 0.0
    fallback: List[Optional[float]] = []
    for r in rows:
        fallback.append(cumulative)
        step = _float_or_none(r.get(COL_DURATION))
        cumulative += step if step is not None else 0.0
    return fallback


def _duration_seconds(rows: Sequence[Dict[str, str]], metadata: Dict[str, str]) -> float:
    span = _span_seconds(rows)
    if span is not None and span > 0:
        return span
    total = 0.0
    any_step = False
    for r in rows:
        step = _float_or_none(r.get(COL_DURATION))
        if step is not None:
            total += step
            any_step = True
    if any_step:
        return total
    meta = _float_or_none(metadata.get("capture.duration_total_sec"))
    return meta if meta is not None else 0.0


# --- Identity helpers -------------------------------------------------------


def _read_mpi_hash(path: str) -> Optional[str]:
    """Return the A2 identity hash if the file carries an A1="MPI" header."""
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
    if (
        len(cells) >= 2
        and cells[0] in (MPI_MARKER, LEGACY_MPI_MARKER)
        and re.fullmatch(r"[0-9a-fA-F]{16}", cells[1])
    ):
        return cells[1].lower()
    return None


def _resolve_session_id(path: str, metadata: Dict[str, str]) -> str:
    sid = (metadata.get(META_SESSION_ID) or "").strip()
    if sid:
        return sid
    mpi = _read_mpi_hash(path)
    if mpi:
        return f"hash:{mpi}"
    return f"file:{os.path.basename(path)}"


def _schema_version(metadata: Dict[str, str]) -> Optional[int]:
    return _int_or_none(metadata.get(META_SCHEMA_VERSION))


# --- List items -------------------------------------------------------------


def read_list_item(path: str) -> SessionListItem:
    """Cheap metadata-only descriptor for the session library list."""
    metadata, _header, rows = _read_sections(path)
    full_duration = _duration_seconds(rows, metadata)
    schema = _schema_version(metadata)
    app_label, app_icon = _resolved_app_identity(metadata)
    started = rows[0].get(COL_CAPTURED_AT) if rows else metadata.get(META_STARTED_AT)
    ended = rows[-1].get(COL_CAPTURED_AT) if rows else metadata.get(META_ENDED_AT)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0
    return SessionListItem(
        session_id=_resolve_session_id(path, metadata),
        file_name=os.path.basename(path),
        file_path=path,
        is_legacy=schema != SCHEMA_VERSION,
        schema_version=schema,
        session_name=(metadata.get(META_SESSION_NAME) or None),
        cuts=read_cuts(metadata, full_duration),
        app_label=app_label,
        app_icon=app_icon,
        bundle_id=metadata.get(META_BUNDLE_ID, "unknown"),
        app_version=(metadata.get(META_VERSION_NAME) or None),
        device_model=metadata.get(META_MODEL, "unknown"),
        device_display_name=(metadata.get(META_DISPLAY_NAME) or None),
        platform=(metadata.get(META_PLATFORM) or "android"),
        os_version=metadata.get(META_OS_VERSION, "unknown"),
        serial_hash=metadata.get(META_SERIAL_HASH, ""),
        started_at=started or None,
        ended_at=ended or None,
        duration_sec=_duration_seconds(rows, metadata),
        sample_count=len(rows),
        modified_at=mtime,
    )


def list_saved_sessions(directory: str) -> List[SessionListItem]:
    """Every valid 3-section live CSV under ``directory`` (recursively), newest
    first. Unreadable / non-session CSVs are skipped silently."""
    items: List[SessionListItem] = []
    if not directory or not os.path.isdir(directory):
        return items
    for root, _dirs, files in os.walk(directory):
        for name in files:
            if not name.lower().endswith(".csv"):
                continue
            full = os.path.join(root, name)
            try:
                items.append(read_list_item(full))
            except (ValueError, OSError):
                continue
    items.sort(key=lambda it: (it.started_at or "", it.modified_at), reverse=True)
    return items


# --- De-duplication ---------------------------------------------------------


def content_fingerprint(path: str) -> str:
    """A 16-hex fingerprint of the *measurement content* of a session CSV.

    Hashes the time-series column header plus every data row, ignoring the MPI
    A1/A2 identity rows and the metadata section (which carry a save-time-derived
    ``info_hash`` that differs between re-exports of the same capture). Two
    exports of the same session therefore share a fingerprint.
    """
    _metadata, header, rows = _read_sections(path)
    h = hashlib.sha256()
    h.update("\x1e".join(header).encode("utf-8"))
    h.update(b"\n")
    for r in rows:
        h.update("\x1f".join(str(r.get(c, "")) for c in header).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()[:16]


def session_dedup_key(path: str) -> str:
    """Stable key for detecting that two files are the same session.

    Prefers the recorded ``session.id`` (v2 files); falls back to a content
    fingerprint for legacy / header-less files that carry no id."""
    metadata, _header, _rows = _read_sections(path)
    sid = (metadata.get(META_SESSION_ID) or "").strip()
    if sid:
        return f"id:{sid}"
    return f"fp:{content_fingerprint(path)}"


# --- Full analysis ----------------------------------------------------------


def _thermal_zone_columns(header: Sequence[str]) -> List[Tuple[str, str]]:
    """(column, zone_name) pairs for every ``thermal_<zone>_c`` column."""
    out: List[Tuple[str, str]] = []
    for col in header:
        m = _THERMAL_ZONE_RE.match(col)
        if m:
            out.append((col, m.group("zone")))
    return out


def _app_memory_mb_series(rows: Sequence[Dict[str, str]]) -> List[Optional[float]]:
    """Per-sample app memory in MB: positive PSS preferred, else positive RSS.

    A legacy CSV that records ``PSS=0`` with a positive RSS falls back to RSS."""
    series: List[Optional[float]] = []
    for r in rows:
        if r.get("mem_memory_source") == ANDROID_MEMORY_SOURCE:
            series.append(None)
            continue
        pss = _float_or_none(r.get(COL_MEM_PSS))
        rss = _float_or_none(r.get(COL_MEM_RSS))
        chosen: Optional[float]
        if pss is not None and pss > 0:
            chosen = pss
        elif rss is not None and rss > 0:
            chosen = rss
        else:
            chosen = None
        series.append(chosen / 1024.0 if chosen is not None else None)
    return series


def _power_mw_series(rows: Sequence[Dict[str, str]]) -> List[Optional[float]]:
    """abs(mV × mA) / 1000 for samples that report both voltage and current."""
    series: List[Optional[float]] = []
    for r in rows:
        v = _float_or_none(r.get(COL_BATT_VOLTAGE))
        i = _float_or_none(r.get(COL_BATT_CURRENT))
        if v is not None and i is not None:
            series.append(abs(v * i) / 1000.0)
        else:
            series.append(None)
    return series


def _fps_stability(fps: Sequence[Optional[float]]) -> Optional[float]:
    nums = [float(v) for v in fps if _finite(v)]
    if not nums:
        return None
    median = statistics.median(nums)
    if median == 0:
        return None
    within = sum(1 for v in nums if median * 0.8 <= v <= median * 1.2)
    return 100.0 * within / len(nums)


def _sum_optional_ints(rows: Sequence[Dict[str, str]], col: str) -> Optional[int]:
    total = 0
    seen = False
    for r in rows:
        v = _int_or_none(r.get(col))
        if v is not None:
            total += v
            seen = True
    return total if seen else None


def _frame_summary(rows: Sequence[Dict[str, str]]) -> FrameSummary:
    total = 0
    janky = 0
    small = 0
    big = 0
    weighted_denom = 0
    weighted_num = 0
    for r in rows:
        t = _int_or_none(r.get(COL_FRAMES_TOTAL))
        if t is None or t <= 0:
            continue  # frame stats valid only when total_frames > 0
        j = _int_or_none(r.get(COL_FRAMES_JANKY)) or 0
        total += t
        janky += j
        small += _int_or_none(r.get(COL_FRAMES_SMALL)) or 0
        big += _int_or_none(r.get(COL_FRAMES_BIG)) or 0
        weighted_denom += t
        weighted_num += j
    weighted = (100.0 * weighted_num / weighted_denom) if weighted_denom > 0 else None

    # Legacy FrameStats uses zero as its no-duration sentinel. A valid frame
    # interval is strictly positive, unlike a valid measured zero FPS.
    p95_vals = [v for v in _column(rows, COL_FRAME_P95) if _finite(v) and v > 0]
    p99_vals = [v for v in _column(rows, COL_FRAME_P99) if _finite(v) and v > 0]
    durations: list[float] = []
    seen: set = set()
    sources: set[str] = set()
    scopes: set[str] = set()
    dropped = 0
    for row in rows:
        dropped += max(0, _int_or_none(row.get("raw_frame_events_dropped")) or 0)
        try:
            events = json.loads(row.get("raw_frame_events") or "[]")
        except (ValueError, TypeError):
            continue
        if not isinstance(events, list):
            continue
        for event in events:
            if not isinstance(event, dict):
                continue
            if isinstance(event.get("timestamp_ns"), bool) or isinstance(event.get("duration_ns"), bool):
                continue
            timestamp = _float_or_none(event.get("timestamp_ns"))
            duration = _float_or_none(event.get("duration_ns"))
            if timestamp is None or timestamp < 0 or duration is None or duration <= 0:
                continue
            identity = (str(event.get("source")), str(event.get("stream_id")), timestamp)
            if identity in seen:
                continue
            seen.add(identity)
            durations.append(duration / 1_000_000)
            sources.add(str(event.get("source") or "unknown"))
            scopes.add(str(event.get("scope") or "unknown"))
    percentiles = statistics.quantiles(durations, n=100, method="inclusive") if len(durations) > 1 else None
    def percentile(q):
        return percentiles[q - 1] if percentiles else durations[0] if durations else None
    return FrameSummary(
        total_frames=total,
        janky_frames=janky,
        small_jank=small,
        big_jank=big,
        weighted_jank_percent=weighted,
        worst_window_p95_ms=max(p95_vals) if p95_vals else None,
        worst_window_p99_ms=max(p99_vals) if p99_vals else None,
        missed_vsync=_sum_optional_ints(rows, COL_FRAMES_MISSED_VSYNC),
        slow_ui_thread=_sum_optional_ints(rows, COL_FRAMES_SLOW_UI),
        frame_deadline_missed=_sum_optional_ints(rows, COL_FRAMES_DEADLINE_MISSED),
        raw_frame_count=len(durations), raw_frames_dropped=dropped,
        session_p50_ms=percentile(50), session_p90_ms=percentile(90),
        session_p95_ms=percentile(95), session_p99_ms=percentile(99),
        raw_frame_sources=sorted(sources), raw_frame_scope=sorted(scopes),
    )


def _temperature(
    rows: Sequence[Dict[str, str]], header: Sequence[str]
) -> Tuple[List[ZoneTempStats], Optional[str], Optional[float], Optional[str], Optional[float], Dict[str, List[Optional[float]]]]:
    zones: List[ZoneTempStats] = []
    series: Dict[str, List[Optional[float]]] = {}
    battery_zone: Optional[str] = None
    battery_max: Optional[float] = None
    hottest_zone: Optional[str] = None
    hottest_max: Optional[float] = None

    for col, zone in _thermal_zone_columns(header):
        values = _column(rows, col)
        series[zone] = values
        first, last = _first_last_valid(values)
        valid = [v for v in values if _finite(v)]
        zmax = max(valid) if valid else None
        change = (last - first) if (first is not None and last is not None) else None
        zones.append(ZoneTempStats(zone=zone, start=first, end=last, max=zmax, change=change))
        if zmax is not None:
            if hottest_max is None or zmax > hottest_max:
                hottest_max = zmax
                hottest_zone = zone
            if _BATTERY_ZONE_RE.search(zone) and (battery_max is None or zmax > battery_max):
                battery_max = zmax
                battery_zone = zone

    zones.sort(key=lambda z: (z.max is None, -(z.max or 0.0)))
    return zones, battery_zone, battery_max, hottest_zone, hottest_max, series


def analyze_session(
    path: str,
    start_sec: Optional[float] = None,
    end_sec: Optional[float] = None,
) -> SessionAnalysis:
    """Full canonical analysis of one saved session CSV.

    ``start_sec``/``end_sec`` crop by elapsed time (inclusive). All aggregates,
    frame totals, temperatures, duration and time-series are then derived from
    exactly the retained rows. The returned time axis is rebased to zero.
    """
    metadata, header, rows = _read_sections(path)
    full_duration = _duration_seconds(rows, metadata)
    if start_sec is not None or end_sec is not None:
        start = 0.0 if start_sec is None else float(start_sec)
        end = math.inf if end_sec is None else float(end_sec)
        if not math.isfinite(start) or start < 0:
            raise ValueError("start_sec must be a finite non-negative number")
        if math.isnan(end) or end < start:
            raise ValueError("end_sec must be greater than or equal to start_sec")
        elapsed = _time_axis(rows)
        rows = [
            row for row, t in zip(rows, elapsed)
            if t is not None and start <= t <= end
        ]
        if not rows:
            raise ValueError("analysis range contains no samples")
    n = len(rows)
    schema = _schema_version(metadata)
    app_label, app_icon = _resolved_app_identity(metadata)

    fps = _column(rows, COL_FPS)
    cpu_app, cpu_system, cpu_norm = _dashboard_cpu_series(
        rows,
        header,
        platform=(metadata.get(META_PLATFORM) or "android"),
    )
    gpu_busy = _column(rows, COL_GPU_BUSY)
    gpu_clock = _column(rows, COL_GPU_CLOCK)
    gpu_temp = _column(rows, COL_GPU_TEMP)
    app_mem = _app_memory_mb_series(rows)
    recorded_platform = (metadata.get(META_PLATFORM) or "").strip().lower()
    typed_memory = [parse_replay_memory(row, recorded_platform) for row in rows]
    has_typed_memory = any(typed_memory)
    latest_memory = typed_memory[-1] if has_typed_memory else {}
    anon_mem = [value / 1024 if (value := sample.get("anon_rss_plus_swap_kib")) is not None else None
                for sample in typed_memory]
    power = _power_mw_series(rows)
    batt_level = _column(rows, COL_BATT_LEVEL)

    metrics: Dict[str, MetricStats] = {
        "fps": compute_metric_stats(fps, n),
        "cpu_app": compute_metric_stats(cpu_app, n),
        "cpu_system": compute_metric_stats(cpu_system, n),
        "cpu_norm": compute_metric_stats(cpu_norm, n),
        "gpu_busy": compute_metric_stats(gpu_busy, n),
        "gpu_clock": compute_metric_stats(gpu_clock, n),
        "gpu_temp": compute_metric_stats(gpu_temp, n),
        "app_memory_mb": compute_metric_stats(app_mem, n),
        "anon_rss_plus_swap_mib": compute_metric_stats(anon_mem, n),
        "power_mw": compute_metric_stats(power, n),
        "battery_level": compute_metric_stats(batt_level, n),
    }

    zones, battery_zone, battery_max, hottest_zone, hottest_max, zone_series = _temperature(
        rows, header
    )

    # Memory ratio vs whole-device RAM, when the session recorded it.
    peak_mem = metrics["app_memory_mb"].max
    sys_total_vals = [v for v in _column(rows, COL_MEM_SYS_TOTAL) if _finite(v) and v > 0]
    sys_total_kb = max(sys_total_vals) if sys_total_vals else None
    mem_ratio: Optional[float] = None
    if peak_mem is not None and sys_total_kb:
        mem_ratio = 100.0 * (peak_mem * 1024.0) / sys_total_kb

    batt_first, batt_last = _first_last_valid(batt_level)
    batt_change = (batt_last - batt_first) if (batt_first is not None and batt_last is not None) else None

    timeseries: Dict[str, List[Optional[float]]] = {
        "t": _time_axis(rows),
        "fps": fps,
        "cpu_app": cpu_app,
        "cpu_system": cpu_system,
        "cpu_norm": cpu_norm,
        "gpu_busy": gpu_busy,
        "gpu_clock": gpu_clock,
        "gpu_temp": gpu_temp,
        "app_memory_mb": app_mem,
        "anon_rss_plus_swap_mib": anon_mem,
        "power_mw": power,
        "battery_level": batt_level,
    }
    for zone, values in zone_series.items():
        timeseries[f"thermal:{zone}"] = values

    provenance: Dict[str, Any] = {
        "fps_sources": sorted({r.get("fps_source") for r in rows if r.get("fps_source")}),
        "fps_scopes": sorted({r.get("fps_scope") for r in rows if r.get("fps_scope")}),
        "cpu_total_sources": sorted({r.get("cpu_total_source") for r in rows if r.get("cpu_total_source")}),
        "status_counts": {},
    }
    for row in rows:
        try:
            statuses = json.loads(row.get("metric_status") or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(statuses, dict):
            continue
        for key, status in statuses.items():
            if isinstance(status, dict) and status.get("status") in ("fresh", "held", "unavailable"):
                counts = provenance["status_counts"].setdefault(key, {})
                state = status["status"]
                counts[state] = counts.get(state, 0) + 1
    return SessionAnalysis(
        session_id=_resolve_session_id(path, metadata),
        file_name=os.path.basename(path),
        file_path=path,
        is_legacy=schema != SCHEMA_VERSION,
        schema_version=schema,
        session_name=(metadata.get(META_SESSION_NAME) or None),
        cuts=read_cuts(metadata, full_duration),
        app_label=app_label,
        app_icon=app_icon,
        bundle_id=metadata.get(META_BUNDLE_ID, "unknown"),
        app_version=(metadata.get(META_VERSION_NAME) or None),
        device_model=metadata.get(META_MODEL, "unknown"),
        device_display_name=(metadata.get(META_DISPLAY_NAME) or None),
        platform=(metadata.get(META_PLATFORM) or "android"),
        os_version=metadata.get(META_OS_VERSION, "unknown"),
        code_name=metadata.get(META_CODE_NAME, ""),
        hw_ver=metadata.get(META_HW_VER, ""),
        serial_hash=metadata.get(META_SERIAL_HASH, ""),
        started_at=(rows[0].get(COL_CAPTURED_AT) or None) if rows else None,
        ended_at=(rows[-1].get(COL_CAPTURED_AT) or None) if rows else None,
        duration_sec=_duration_seconds(rows, metadata),
        sample_count=n,
        metrics=metrics,
        fps_stability_percent=_fps_stability(fps),
        frames=_frame_summary(rows),
        temp_zones=zones,
        battery_zone=battery_zone,
        max_temp_battery=battery_max,
        hottest_sensor_zone=hottest_zone,
        hottest_sensor_max=hottest_max,
        peak_app_memory_mb=peak_mem,
        memory_ratio_percent=mem_ratio,
        battery_start_level=batt_first,
        battery_end_level=batt_last,
        battery_change_pp=batt_change,
        timeseries=timeseries,
        metric_provenance=provenance,
        device_build_version=metadata.get(META_BUILD_VERSION, ""),
        device_build_name=metadata.get(META_BUILD_NAME, ""),
        device_model_identifier=metadata.get(META_MODEL_IDENTIFIER, ""),
        device_model_number=metadata.get(META_MODEL_NUMBER, ""),
        memory_source=ANDROID_MEMORY_SOURCE if has_typed_memory else None,
        memory_reference_context={
            key: latest_memory.get(key)
            for key in ("play_memory_sdk_int", "play_memory_ram_kib", "play_memory_form_factor",
                        "play_memory_state", "play_memory_state_source", "play_memory_category", "play_memory_category_source")
        } if has_typed_memory else {},
    )
