"""Adapter from a flat summary dict to mpi's data model.

The live CSV replay path builds a flat summary dict with mixed time-series and
aggregate fields (see ``api._live_summary_from_snapshots``). This module splits
that into:

- ``ReportSummary``: the aggregates (median, average, stabilities, max_fps,
  fps_std, temperature start/end/max, power, metadata).
- ``List[MetricsSnapshot]``: per-second snapshots reconstructed from
  ``fps_series`` only. Per-snapshot CPU/temp are intentionally NOT filled from
  the single session-average, because faking time-series would mislead any
  consumer reading the snapshots as ground truth.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..schema import (
    DeviceInfo,
    FpsStats,
    FrameStats,
    MetricsSnapshot,
    PackageInfo,
    Platform,
    ReportSummary,
    ThermalSummary,
)

logger = logging.getLogger(__name__)


_ABS_TIME_FORMATS = (
    "%Y/%m/%d %H:%M:%S.%f",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S",
)


def _parse_abs_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    for fmt in _ABS_TIME_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    logger.debug("could not parse abs_time value: %r", value)
    return None


def _build_device(metadata: Dict[str, Any]) -> DeviceInfo:
    model = str(metadata.get("model") or "unknown")
    os_ver = metadata.get("os_ver")
    os_version = f"Android {os_ver}" if os_ver else "unknown"
    return DeviceInfo(
        serial=str(metadata.get("dumpstate", {}).get("Code", "offline")),
        platform=Platform.ANDROID,
        model=model,
        os_version=os_version,
    )


def _build_package(metadata: Dict[str, Any]) -> PackageInfo:
    return PackageInfo(
        bundle_id=str(metadata.get("package_name") or "unknown"),
        label=metadata.get("game_name"),
        version_name=metadata.get("game_ver"),
    )


def _build_fps_stats(summary: Dict[str, Any]) -> FpsStats:
    norm = summary.get("norm_fps_analysis") or {}

    def _opt(value: Any) -> Optional[float]:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    return FpsStats(
        median=float(summary.get("median") or 0),
        average=float(summary.get("average") or 0),
        median_stability=float(summary.get("median_stability") or 0),
        average_stability=float(summary.get("average_stability") or 0),
        first_half=float(summary.get("first_half") or 0),
        first_half_stability=float(summary.get("first_half_stability") or 0),
        second_half=float(summary.get("second_half") or 0),
        second_half_stability=float(summary.get("second_half_stability") or 0),
        max_fps=float(summary.get("max_fps") or 0),
        fps_std=float(summary.get("fps_std") or 0),
        fps_below_90pct_ratio=float(summary.get("fps_below_90pct_ratio") or 0),
        norm_fps_std=_opt(norm.get("norm_fps_std")),
        norm_fps_avg=_opt(norm.get("norm_fps_avg")),
        norm_fps_max=_opt(norm.get("norm_fps_max")),
    )


def _build_thermal_summary(summary: Dict[str, Any]) -> Optional[ThermalSummary]:
    temp_analysis = summary.get("temp_analysis") or {}
    if not temp_analysis:
        return None
    zones: Dict[str, Tuple[float, float, float]] = {}
    for zone_name, stats in temp_analysis.items():
        try:
            zones[str(zone_name)] = (
                float(stats.get("start") or 0),
                float(stats.get("end") or 0),
                float(stats.get("max") or 0),
            )
        except (TypeError, ValueError):
            continue
    if not zones:
        return None
    return ThermalSummary(zones=zones)


def _opt_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def summary_to_report(
    summary: Dict[str, Any],
    file_path: str,
    source: str,
) -> Tuple[List[MetricsSnapshot], ReportSummary]:
    """Convert a live CSV summary into a report and per-second snapshots."""
    metadata = summary.get("metadata") or {}
    actual = summary.get("actual_measurement_time") or (0, 0)
    duration_min = int(actual[0]) if actual else 0
    duration_sec = int(actual[1]) if actual and len(actual) > 1 else 0

    report = ReportSummary(
        file_path=file_path,
        source=source,
        duration_min=duration_min,
        duration_sec=duration_sec,
        fps=_build_fps_stats(summary),
        thermal=_build_thermal_summary(summary),
        cpu_usage_avg=_opt_float(summary.get("cpu_usage")),
        gpu_usage_avg=_opt_float(summary.get("gpu_usage")),
        power_mw_avg=_opt_float(summary.get("power")),
        abs_time_start=summary.get("abs_time_start") or None,
        abs_time_end=summary.get("abs_time_end") or None,
        metadata={
            str(k): str(v)
            for k, v in metadata.items()
            if not isinstance(v, (dict, list))
        },
    )

    snapshots = _build_snapshots(summary, metadata)
    return snapshots, report


def _build_snapshots(
    summary: Dict[str, Any],
    metadata: Dict[str, Any],
) -> List[MetricsSnapshot]:
    fps_series = summary.get("fps_series") or []
    if not fps_series:
        return []

    device = _build_device(metadata)
    package = _build_package(metadata)

    base = _parse_abs_time(summary.get("abs_time_start"))
    if base is None:
        base = datetime.now(timezone.utc)
    elif base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)

    snapshots: List[MetricsSnapshot] = []
    for i, raw_fps in enumerate(fps_series):
        try:
            fps_value = float(raw_fps)
        except (TypeError, ValueError):
            fps_value = 0.0

        captured_at = (base + timedelta(seconds=i)).isoformat()
        snapshots.append(
            MetricsSnapshot(
                device=device,
                package=package,
                captured_at=captured_at,
                duration_sec=1.0,
                frames=FrameStats(fps_estimate=fps_value),
            )
        )
    return snapshots
