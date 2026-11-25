from __future__ import annotations

import csv
import hashlib
import json
import statistics
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .schema import MetricsSnapshot
from .session_cuts import validate_cuts


# Marker written into cell A1 of an exported raw CSV. Cell A2 holds the info
# hash. The loader checks both before parsing.
MPI_MARKER = "MPI"
# Read-only compatibility for raw CSVs created before the namespace migration.
LEGACY_MPI_MARKER = "PerfInspect"

# CSV metadata schema version. v1 (or header-less legacy) files omit the
# ``schema.version`` metadata row. Keep in sync with
# ``mpi.analysis.SCHEMA_VERSION``.
CSV_SCHEMA_VERSION = 2


def stable_session_id(serial: str, bundle_id: str, first_captured_at: str) -> str:
    """A stable 16-hex id for a capture session.

    Unlike :func:`gen_info_hash` (which folds in the save time and therefore
    changes on every re-export), this is anchored to the first sample's
    timestamp, so the same session keeps the same id across CSV, XLSX and
    endpoint exports. The desktop may generate its own id at capture start and
    pass it through instead; this is the fallback when it does not.
    """
    payload = f"{serial}\x1f{bundle_id}\x1f{first_captured_at}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def gen_info_hash(serial: str, bundle_id: str, save_time_iso: str) -> str:
    """16-char hex identity hash for a raw CSV export.

    Derived from device serial + package bundle id + the global save time, so a
    file can be recognized/labelled. Truncated SHA-256 (identification, not
    cryptographic integrity).
    """
    payload = f"{serial}\x1f{bundle_id}\x1f{save_time_iso}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def hash_serial(serial: str) -> str:
    """SHA-256 hex of a device serial for privacy-preserving endpoint transport.

    The raw serial never leaves the desktop: outbound payloads carry only this
    hash (PIPA/GDPR data minimization).
    The stable hash lets a configured receiver deduplicate the same device.
    """
    return hashlib.sha256((serial or "").encode("utf-8")).hexdigest()


_STAT_HEADER = ("metric", "avg", "min", "max", "p50", "p95", "p99")

_FIXED_TIMESERIES_COLUMNS_PREFIX = (
    "captured_at",
    "duration_sec",
    "fps",
    "jank_percent",
    "frames_total",
    "frames_janky",
    "frames_small_jank",
    "frames_big_jank",
    "frame_p50_ms",
    "frame_p90_ms",
    "frame_p95_ms",
    "frame_p99_ms",
    "frames_missed_vsync",
    "frames_slow_ui_thread",
    "frames_deadline_missed",
    "cpu_percent",
    "mem_percent",
    "cpu_total_percent",
    "cpu_norm",
)

_FIXED_TIMESERIES_COLUMNS_GPU = (
    "gpu_busy_percent",
    "gpu_clock_mhz",
    "gpu_temp_c",
)

_FIXED_TIMESERIES_COLUMNS_NETWORK = (
    "network_rx_bytes",
    "network_tx_bytes",
    "network_rx_kbps",
    "network_tx_kbps",
)

_FIXED_TIMESERIES_COLUMNS_TAIL = (
    "battery_level_percent",
    "battery_current_ma",
    "battery_voltage_mv",
    "battery_status",
    "mem_pss_kb",
    "mem_rss_kb",
    "mem_java_heap_kb",
    "mem_native_heap_kb",
    "mem_graphics_kb",
    "mem_code_kb",
    "mem_stack_kb",
    "mem_sys_total_kb",
    "mem_sys_used_kb",
    "fps_source", "fps_scope", "fps_experimental",
    "raw_frame_events", "raw_frame_events_dropped", "metric_status",
    "cpu_per_core_percent", "cpu_logical_core_count", "cpu_total_source", "cpu_total_status",
    "mem_memory_source",
    "mem_anon_rss_kib",
    "mem_swap_kib",
    "mem_anon_rss_plus_swap_kib",
    "mem_play_memory_state",
    "mem_play_memory_state_source",
    "mem_play_memory_category",
    "mem_play_memory_category_source",
    "mem_play_memory_ram_kib",
    "mem_play_memory_sdk_int",
    "mem_play_memory_form_factor",
    "mem_play_memory_pid",
    "mem_play_memory_start_ticks",
    "elapsed_sec",
)


def _static_extractors() -> List[Tuple[str, Callable[[MetricsSnapshot], Optional[float]]]]:
    """Scalar metric extractors shared by the CSV summary and the JSON payload.

    Single source of truth so the summary section and ``build_endpoint_payload``
    never drift in metric names or order.
    """
    return [
        ("fps", lambda s: s.frames.fps_estimate),
        ("jank_percent", lambda s: s.frames.jank_percent),
        ("cpu_percent", lambda s: s.cpu.cpu_percent),
        ("cpu_total_percent", lambda s: s.cpu.total_percent),
        ("cpu_norm", lambda s: s.cpu.cpu_norm),
        ("network_rx_kbps", lambda s: s.network.rx_kbps),
        ("network_tx_kbps", lambda s: s.network.tx_kbps),
        ("gpu_busy_percent", lambda s: s.gpu.busy_percent),
        ("gpu_clock_mhz", lambda s: s.gpu.clock_mhz),
        (
            "thermal_max_c",
            lambda s: (
                max(s.thermal.zones_c.values()) if s.thermal.zones_c else None
            ),
        ),
        (
            "battery_level_percent",
            lambda s: (
                float(s.battery.level_percent)
                if s.battery.level_percent is not None
                else None
            ),
        ),
        (
            "mem_pss_kb",
            lambda s: (
                float(s.memory.pss_total_kb)
                if s.memory.pss_total_kb is not None
                else None
            ),
        ),
    ]


def generate_csv_report(
    snapshots: Sequence[MetricsSnapshot],
    path: str,
    info_hash: Optional[str] = None,
    session_id: Optional[str] = None,
    session_name: Optional[str] = None,
    app_icon: Optional[str] = None,
    cuts: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Write a 3-section CSV report (metadata / summary stats / time-series).

    Sections are separated by a blank row. Section 1 holds device + package +
    capture metadata (key,value rows). Section 2 holds avg/min/max/p50/p95/p99
    for each scalar metric. Section 3 holds one row per snapshot.

    When ``info_hash`` is given, two identity rows precede everything: cell A1 =
    ``"MPI"`` and cell A2 = the hash. The loader validates these before parsing.

    Variable per-device fields (CPU core count, thermal zone keys) are unioned
    across all snapshots, with empty cells where a snapshot lacks the value.

    Raises ValueError if `snapshots` is empty. Caller should catch OSError.
    """
    if not snapshots:
        raise ValueError("snapshots is empty")

    cuts = validate_cuts(cuts or [], _duration_total_sec(snapshots))
    cpu_cols, thermal_cols = _collect_dynamic_columns(snapshots)

    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        if info_hash is not None:
            w.writerow([MPI_MARKER])
            w.writerow([info_hash])
            w.writerow([])
        _write_metadata(w, snapshots, session_id, session_name, app_icon, cuts)
        w.writerow([])
        _write_summary(w, snapshots, cpu_cols, thermal_cols)
        w.writerow([])
        _write_frame_event_statistics(w, snapshots)
        w.writerow([])
        _write_timeseries(w, snapshots, cpu_cols, thermal_cols)


# --- helpers ----------------------------------------------------------------


def _collect_dynamic_columns(
    snapshots: Sequence[MetricsSnapshot],
) -> Tuple[List[str], List[str]]:
    max_cores = max(
        (len(s.cpu.per_core_freq_mhz) for s in snapshots), default=0
    )
    cpu_cols = [f"cpu{i}_mhz" for i in range(max_cores)]
    zone_keys = sorted({k for s in snapshots for k in s.thermal.zones_c.keys()})
    thermal_cols = [f"thermal_{k}_c" for k in zone_keys]
    return cpu_cols, thermal_cols


def captured_span_seconds(snapshots: Sequence[MetricsSnapshot]) -> Optional[float]:
    """Active seconds between samples, falling back to legacy wall-clock time.

    Returns None when there are fewer than two samples or the timestamps cannot
    be parsed, so the caller can fall back to another estimate. Shared by the CSV
    report, the cloud/endpoint payload, and the XLSX summary so all three report
    the same real measurement time.
    """
    if len(snapshots) >= 2 and all(s.elapsed_sec is not None for s in snapshots):
        return snapshots[-1].elapsed_sec - snapshots[0].elapsed_sec
    if len(snapshots) < 2:
        return None
    try:
        start = datetime.fromisoformat(snapshots[0].captured_at)
        end = datetime.fromisoformat(snapshots[-1].captured_at)
    except (ValueError, TypeError):
        return None
    return (end - start).total_seconds()


def _duration_total_sec(snapshots: Sequence[MetricsSnapshot]) -> float:
    """The session's real length: the wall-clock span between the first and last
    sample, falling back to the sum of nominal per-sample intervals only when the
    span is unavailable (single sample / unparseable timestamps).

    Summing per-sample ``duration_sec`` drifts from the true elapsed time whenever
    samples arrive irregularly or are dropped, so the span is preferred — matching
    the XLSX summary path (api.py ``_live_summary_from_snapshots``).
    """
    span = captured_span_seconds(snapshots)
    if span is not None and span > 0:
        return span
    return sum(s.duration_sec for s in snapshots)


def _write_metadata(
    w,
    snapshots: Sequence[MetricsSnapshot],
    session_id: Optional[str] = None,
    session_name: Optional[str] = None,
    app_icon: Optional[str] = None,
    cuts: Optional[List[Dict[str, Any]]] = None,
) -> None:
    first = snapshots[0]
    last = snapshots[-1]
    d = first.device
    p = first.package
    duration_total = _duration_total_sec(snapshots)
    sid = session_id or stable_session_id(d.serial, p.bundle_id, first.captured_at)

    w.writerow(["# Section", "Metadata"])
    w.writerow(["key", "value"])
    rows: List[Tuple[str, Any]] = [
        # CSV v2 identity/schema fields. Older files omit these; the loader treats
        # their absence as a v1/legacy file.
        ("schema.version", CSV_SCHEMA_VERSION),
        ("session.id", sid),
        ("session.name", (session_name or "").strip()),
        ("session.cuts", json.dumps(validate_cuts(cuts or [], duration_total), ensure_ascii=False)),
        # Hash the serial in the CSV too (not just the cloud payload) so shared/
        # exported CSVs never carry the raw device identifier (PIPA/GDPR).
        ("device.serial_hash", hash_serial(d.serial)),
        ("device.model", d.model),
        ("device.model_identifier", d.model_identifier or ""),
        ("device.model_number", d.model_number or ""),
        ("device.display_name", d.display_name or ""),
        ("device.os_version", d.os_version),
        ("device.build_version", d.build_version or ""),
        ("device.build_name", d.build_name or ""),
        ("device.platform", d.platform.value),
        # De-identified board/codename metadata (never the raw serial).
        ("device.code_name", d.code_name or ""),
        ("device.hw_ver", d.hw_ver or ""),
        ("package.bundle_id", p.bundle_id),
        ("package.label", p.label or ""),
        # Optional launcher icon is stored as a PNG data URI so saved-session
        # lists remain self-contained when the measured device is disconnected.
        ("package.icon", app_icon or ""),
        ("package.version_name", p.version_name or ""),
        ("package.version_code", p.version_code or ""),
        ("package.uid", p.uid or ""),
        ("capture.started_at", first.captured_at),
        ("capture.ended_at", last.captured_at),
        ("capture.snapshot_count", len(snapshots)),
        ("capture.duration_total_sec", f"{duration_total:.3f}"),
        ("capture.pause_used", str(any(s.pause_used is True for s in snapshots)).lower()),
    ]
    for key, value in rows:
        w.writerow([key, value])


def _write_summary(
    w,
    snapshots: Sequence[MetricsSnapshot],
    cpu_cols: List[str],
    thermal_cols: List[str],
) -> None:
    w.writerow(["# Section", "Summary statistics"])
    w.writerow(list(_STAT_HEADER))

    # Static rows
    for name, extractor in _static_extractors():
        values = [extractor(s) for s in snapshots]
        w.writerow(_stat_row(name, values))

    # Per-core CPU MHz
    for i, col in enumerate(cpu_cols):
        values = [
            s.cpu.per_core_freq_mhz[i]
            if i < len(s.cpu.per_core_freq_mhz)
            else None
            for s in snapshots
        ]
        w.writerow(_stat_row(col, values))

    # Per-zone thermal °C
    for col in thermal_cols:
        zone_key = col[len("thermal_") : -len("_c")]
        values = [s.thermal.zones_c.get(zone_key) for s in snapshots]
        w.writerow(_stat_row(col, values))


def _write_frame_event_statistics(w, snapshots: Sequence[MetricsSnapshot]) -> None:
    # Imported lazily: analysis reads the CSV marker constants from this module.
    from .analysis import _frame_summary
    summary = _frame_summary([
        {"raw_frame_events": json.dumps(s.frames.raw_events),
         "raw_frame_events_dropped": str(s.frames.raw_events_dropped),
         "frame_p95_ms": str(s.frames.p95_ms), "frame_p99_ms": str(s.frames.p99_ms)}
        for s in snapshots
    ])
    w.writerow(["# Section", "Frame event statistics"])
    w.writerow(["metric", "value"])
    for key in ("raw_frame_count", "raw_frames_dropped", "session_p50_ms", "session_p90_ms",
                "session_p95_ms", "session_p99_ms", "worst_window_p95_ms", "worst_window_p99_ms"):
        w.writerow([key, _cell(getattr(summary, key))])
    w.writerow(["raw_frame_sources", ", ".join(summary.raw_frame_sources)])
    w.writerow(["raw_frame_scope", ", ".join(summary.raw_frame_scope)])


def _write_timeseries(
    w,
    snapshots: Sequence[MetricsSnapshot],
    cpu_cols: List[str],
    thermal_cols: List[str],
) -> None:
    w.writerow(["# Section", "Time-series"])
    header = (
        list(_FIXED_TIMESERIES_COLUMNS_PREFIX)
        + cpu_cols
        + list(_FIXED_TIMESERIES_COLUMNS_GPU)
        + list(_FIXED_TIMESERIES_COLUMNS_NETWORK)
        + thermal_cols
        + list(_FIXED_TIMESERIES_COLUMNS_TAIL)
    )
    w.writerow(header)
    for s in snapshots:
        w.writerow(_flatten_snapshot(s, cpu_cols, thermal_cols))


def _flatten_snapshot(
    s: MetricsSnapshot, cpu_cols: List[str], thermal_cols: List[str]
) -> List[str]:
    cells = [_cell(v) for v in _snapshot_values(s, cpu_cols, thermal_cols)]
    # Cuts use microsecond precision. Rounding this axis to the metric cells'
    # three decimals could place a saved end point beyond the reloaded session.
    if s.elapsed_sec is not None:
        cells[-1] = f"{s.elapsed_sec:.6f}".rstrip("0").rstrip(".")
    return cells


def _snapshot_values(
    s: MetricsSnapshot, cpu_cols: List[str], thermal_cols: List[str]
) -> List[Any]:
    """One time-series row as typed values (None/int/float/str), column order
    matching the CSV header. ``_flatten_snapshot`` stringifies these for CSV;
    ``build_endpoint_payload`` keeps them typed for JSON."""
    row: List[Any] = [
        s.captured_at,
        s.duration_sec,
        s.frames.fps_estimate,
        s.frames.jank_percent,
        s.frames.total_frames,
        s.frames.janky_frames,
        s.frames.small_jank,
        s.frames.big_jank,
        s.frames.p50_ms,
        s.frames.p90_ms,
        s.frames.p95_ms,
        s.frames.p99_ms,
        s.frames.missed_vsync,
        s.frames.slow_ui_thread,
        s.frames.frame_deadline_missed,
        s.cpu.cpu_percent,
        s.cpu.mem_percent,
        s.cpu.total_percent,
        s.cpu.cpu_norm,
    ]
    for i in range(len(cpu_cols)):
        row.append(
            s.cpu.per_core_freq_mhz[i]
            if i < len(s.cpu.per_core_freq_mhz)
            else None
        )
    row.extend([s.gpu.busy_percent, s.gpu.clock_mhz, s.gpu.temp_c])
    row.extend([
        s.network.rx_bytes,
        s.network.tx_bytes,
        s.network.rx_kbps,
        s.network.tx_kbps,
    ])
    for col in thermal_cols:
        zone_key = col[len("thermal_") : -len("_c")]
        row.append(s.thermal.zones_c.get(zone_key))
    row.extend(
        [
            s.battery.level_percent,
            s.battery.current_ma,
            s.battery.voltage_mv,
            s.battery.status or "",
            s.memory.pss_total_kb,
            s.memory.rss_total_kb,
            s.memory.java_heap_kb,
            s.memory.native_heap_kb,
            s.memory.graphics_kb,
            s.memory.code_kb,
            s.memory.stack_kb,
            s.memory.sys_total_kb,
            s.memory.sys_used_kb,
            s.frames.source or "", s.frames.scope or "", s.frames.experimental,
            json.dumps(s.frames.raw_events, separators=(",", ":")),
            s.frames.raw_events_dropped,
            json.dumps(s.metric_status, separators=(",", ":")),
            json.dumps(s.cpu.per_core_percent, separators=(",", ":")),
            s.cpu.logical_core_count, s.cpu.total_source or "", s.cpu.total_status or "",
            s.memory.memory_source,
            s.memory.anon_rss_kib,
            s.memory.swap_kib,
            s.memory.anon_rss_plus_swap_kib,
            s.memory.play_memory_state,
            s.memory.play_memory_state_source,
            s.memory.play_memory_category,
            s.memory.play_memory_category_source,
            s.memory.play_memory_ram_kib,
            s.memory.play_memory_sdk_int,
            s.memory.play_memory_form_factor,
            s.memory.play_memory_pid,
            s.memory.play_memory_start_ticks,
            s.elapsed_sec,
        ]
    )
    return row


def _stat_row(name: str, values: Sequence[Optional[float]]) -> List[str]:
    nums = [float(v) for v in values if v is not None]
    if not nums:
        return [name, "", "", "", "", "", ""]
    avg = statistics.fmean(nums)
    lo = min(nums)
    hi = max(nums)
    p50 = statistics.median(nums)
    p95, p99 = _percentiles_95_99(nums)
    return [
        name,
        _num(avg),
        _num(lo),
        _num(hi),
        _num(p50),
        _num(p95),
        _num(p99),
    ]


def _percentiles_95_99(nums: List[float]) -> Tuple[float, float]:
    if len(nums) < 2:
        only = nums[0]
        return only, only
    qs = statistics.quantiles(nums, n=100, method="inclusive")
    # quantiles(n=100) returns 99 cut points; index 94 = p95, 98 = p99.
    return qs[94], qs[98]


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return _num(v)
    return str(v)


def _num(v: float) -> str:
    if v == int(v):
        return str(int(v))
    return f"{v:.3f}".rstrip("0").rstrip(".")


# --- JSON endpoint payload --------------------------------------------------


def _stat_dict(values: Sequence[Optional[float]]) -> Optional[Dict[str, float]]:
    """avg/min/max/p50/p95/p99 as floats, or None when no values are present.
    Mirrors ``_stat_row`` but keeps numbers typed for JSON."""
    nums = [float(v) for v in values if v is not None]
    if not nums:
        return None
    p95, p99 = _percentiles_95_99(nums)
    return {
        "avg": statistics.fmean(nums),
        "min": min(nums),
        "max": max(nums),
        "p50": statistics.median(nums),
        "p95": p95,
        "p99": p99,
    }


def build_endpoint_payload(
    snapshots: Sequence[MetricsSnapshot],
    info_hash: Optional[str] = None,
    name: Optional[str] = None,
    icon: Optional[str] = None,
    session_id: Optional[str] = None,
    cuts: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """JSON-serializable dict mirroring the 3-section CSV report.

    Sections (``metadata`` / ``summary`` / ``timeseries``) match
    ``generate_csv_report`` so a receiver can losslessly reconstruct the CSV or
    consume structured data. ``summary`` keys equal ``_static_extractors`` names
    (plus dynamic ``cpuN_mhz`` / ``thermal_<zone>_c``); ``timeseries.columns``
    equals the CSV header column order.

    ``name`` is the optional user-set session name, carried at
    ``metadata.capture.name`` (null when absent) so the receiver can label the
    run. ``icon`` is the optional app launcher icon (base64 PNG data URI) at
    ``metadata.package.icon``. Both keys are always present so receivers can rely
    on them.

    Raises ValueError if ``snapshots`` is empty.
    """
    if not snapshots:
        raise ValueError("snapshots is empty")

    cpu_cols, thermal_cols = _collect_dynamic_columns(snapshots)
    first = snapshots[0]
    last = snapshots[-1]
    d = first.device
    p = first.package
    duration_total = _duration_total_sec(snapshots)

    metadata: Dict[str, Any] = {
        "device": {
            "serial_hash": hash_serial(d.serial),
            "model": d.model,
            "model_identifier": d.model_identifier or "",
            "model_number": d.model_number or "",
            "display_name": d.display_name or "",
            "os_version": d.os_version,
            "build_version": d.build_version or "",
            "build_name": d.build_name or "",
            "platform": d.platform.value,
            "code_name": d.code_name,
            "hw_ver": d.hw_ver,
        },
        "package": {
            "bundle_id": p.bundle_id,
            "label": p.label or "",
            "version_name": p.version_name or "",
            "version_code": p.version_code or "",
            "uid": p.uid or "",
            # App launcher icon as a base64 PNG data URI (null when unavailable).
            "icon": icon if isinstance(icon, str) and icon else None,
        },
        "capture": {
            "started_at": first.captured_at,
            "ended_at": last.captured_at,
            "snapshot_count": len(snapshots),
            "cuts": validate_cuts(cuts or [], duration_total),
            "duration_total_sec": round(duration_total, 3),
            "pause_used": any(s.pause_used is True for s in snapshots),
            "name": name.strip() if isinstance(name, str) and name.strip() else None,
            # Stable session id (optional). Defaults to the timestamp-anchored id
            # so a receiver can dedupe re-exports of the same session.
            "session_id": (
                session_id
                if isinstance(session_id, str) and session_id
                else stable_session_id(d.serial, p.bundle_id, first.captured_at)
            ),
        },
    }

    summary: Dict[str, Any] = {}
    for name, extractor in _static_extractors():
        summary[name] = _stat_dict([extractor(s) for s in snapshots])
    for i, col in enumerate(cpu_cols):
        summary[col] = _stat_dict(
            [
                s.cpu.per_core_freq_mhz[i]
                if i < len(s.cpu.per_core_freq_mhz)
                else None
                for s in snapshots
            ]
        )
    for col in thermal_cols:
        zone_key = col[len("thermal_") : -len("_c")]
        summary[col] = _stat_dict([s.thermal.zones_c.get(zone_key) for s in snapshots])

    columns = (
        list(_FIXED_TIMESERIES_COLUMNS_PREFIX)
        + cpu_cols
        + list(_FIXED_TIMESERIES_COLUMNS_GPU)
        + list(_FIXED_TIMESERIES_COLUMNS_NETWORK)
        + thermal_cols
        + list(_FIXED_TIMESERIES_COLUMNS_TAIL)
    )
    rows = [_snapshot_values(s, cpu_cols, thermal_cols) for s in snapshots]

    payload: Dict[str, Any] = {
        "mpi_marker": MPI_MARKER,
        "schema_version": 1,
        "metadata": metadata,
        "summary": summary,
        "timeseries": {"columns": columns, "rows": rows},
    }
    if info_hash is not None:
        payload["info_hash"] = info_hash
    return payload
