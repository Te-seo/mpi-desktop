"""Tests for live report adapters that split aggregates from sample snapshots."""

from __future__ import annotations

from datetime import datetime

from mpi.reports.adapter import summary_to_report
from mpi.schema import MetricsSnapshot, ReportSummary


SYNTHETIC_LIVE_SUMMARY = {
    "median": 60,
    "average": 58.4,
    "median_stability": 95.5,
    "average_stability": 90.0,
    "first_half": 60.1,
    "first_half_stability": 96.0,
    "second_half": 56.7,
    "second_half_stability": 88.0,
    "max_fps": 62.0,
    "fps_std": 4.3,
    "fps_below_90pct_ratio": 8.2,
    "actual_measurement_time": (5, 0),
    "cpu_usage": 23.4,
    "gpu_usage": 0.0,
    "power": 1850.0,
    "abs_time_start": "2026/04/27 09:24:02.478",
    "abs_time_end": "2026/04/27 09:29:02.478",
    "fps_series": [60.0, 59.5, 58.0, 60.0, 61.0],
    "norm_fps_analysis": {
        "norm_fps_std": 2.1,
        "norm_fps_avg": 59.5,
        "norm_fps_max": 62.0,
    },
    "temp_analysis": {
        "battery": {"start": 30.5, "end": 38.2, "max": 41.7},
        "gpu": {"start": 32.0, "end": 40.1, "max": 43.0},
    },
    "metadata": {
        "date": "2026/04/27 09:24:02",
        "game_name": "Vampire Survivors",
        "game_ver": "1.14.105",
        "package_name": "com.example.vs",
        "model": "SM-S938N",
        "os_ver": "16",
    },
}


def test_summary_to_report_basic_split():
    snapshots, report = summary_to_report(
        SYNTHETIC_LIVE_SUMMARY, "session.csv", "live_csv"
    )

    assert isinstance(report, ReportSummary)
    assert report.source == "live_csv"
    assert report.duration_min == 5
    assert report.fps is not None
    assert report.fps.median == 60
    assert report.fps.average == 58.4
    assert report.fps.max_fps == 62.0
    assert report.cpu_usage_avg == 23.4
    assert report.gpu_usage_avg == 0.0
    assert report.power_mw_avg == 1850.0

    assert report.thermal is not None
    assert "battery" in report.thermal.zones
    assert report.thermal.zones["battery"] == (30.5, 38.2, 41.7)

    assert len(snapshots) == 5
    assert all(isinstance(s, MetricsSnapshot) for s in snapshots)


def test_summary_to_report_snapshots_have_monotonic_timestamps():
    snapshots, _ = summary_to_report(
        SYNTHETIC_LIVE_SUMMARY, "session.csv", "live_csv"
    )
    times = [datetime.fromisoformat(s.captured_at) for s in snapshots]
    diffs = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
    assert all(d == 1.0 for d in diffs)


def test_summary_to_report_fps_lands_on_frames_estimate():
    snapshots, _ = summary_to_report(
        SYNTHETIC_LIVE_SUMMARY, "session.csv", "live_csv"
    )
    assert [s.frames.fps_estimate for s in snapshots] == [60.0, 59.5, 58.0, 60.0, 61.0]


def test_summary_to_report_does_not_fake_per_snapshot_cpu_or_temp():
    """CPU/temp aggregates must NOT be smeared into per-snapshot fields."""
    snapshots, report = summary_to_report(
        SYNTHETIC_LIVE_SUMMARY, "session.csv", "live_csv"
    )
    for snap in snapshots:
        assert snap.cpu.cpu_percent is None  # no per-snapshot CPU measurement
        assert snap.thermal.zones_c == {}
    assert report.cpu_usage_avg == 23.4


def test_summary_to_report_handles_missing_abs_time():
    payload = {**SYNTHETIC_LIVE_SUMMARY, "abs_time_start": ""}
    snapshots, report = summary_to_report(payload, "x.csv", "live_csv")
    assert len(snapshots) == 5
    assert report.abs_time_start in (None, "")


def test_summary_to_report_metadata_only_str_values():
    """Adapter must not propagate dict subfields like dumpstate into the flat metadata."""
    payload = dict(SYNTHETIC_LIVE_SUMMARY)
    payload["metadata"] = dict(payload["metadata"])
    payload["metadata"]["dumpstate"] = {"Code": "abc"}
    _, report = summary_to_report(payload, "x.csv", "live_csv")
    assert "dumpstate" not in report.metadata
    assert report.metadata.get("game_name") == "Vampire Survivors"
