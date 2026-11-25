"""Regression tests for the canonical session-analysis calculation path."""

from __future__ import annotations

import base64
import csv
import math
import os
from typing import Dict, List, Optional, Sequence

import pytest

from mpi.analysis import (
    SCHEMA_VERSION,
    analyze_session,
    compute_metric_stats,
    list_saved_sessions,
    read_list_item,
)


DEFAULT_HEADER = [
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
    "cpu_percent",
    "mem_percent",
    "cpu_total_percent",
    "cpu_norm",
    "gpu_busy_percent",
    "gpu_clock_mhz",
    "gpu_temp_c",
    "thermal_battery_c",
    "thermal_cpu_c",
    "battery_level_percent",
    "battery_current_ma",
    "battery_voltage_mv",
    "battery_status",
    "mem_pss_kb",
    "mem_rss_kb",
    "mem_sys_total_kb",
]


def write_session_csv(
    path: str,
    metadata: Dict[str, str],
    rows: Sequence[Dict[str, object]],
    header: Sequence[str] = DEFAULT_HEADER,
    mpi_hash: Optional[str] = None,
) -> str:
    """Write a minimal 2-section (Metadata + Time-series) live CSV for tests."""
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        if mpi_hash is not None:
            w.writerow(["MPI"])
            w.writerow([mpi_hash])
            w.writerow([])
        w.writerow(["# Section", "Metadata"])
        w.writerow(["key", "value"])
        for k, v in metadata.items():
            w.writerow([k, v])
        w.writerow([])
        w.writerow(["# Section", "Time-series"])
        w.writerow(list(header))
        for r in rows:
            w.writerow(["" if r.get(c) is None else r.get(c) for c in header])
    return path


# --- compute_metric_stats ---------------------------------------------------


def test_median_odd_and_even():
    assert compute_metric_stats([1, 2, 3]).median == 2
    assert compute_metric_stats([1, 2, 3, 4]).median == 2.5


def test_single_sample_percentile_and_stddev():
    s = compute_metric_stats([5.0])
    assert s.valid_count == 1
    assert s.average == 5.0
    assert s.median == 5.0
    assert s.min == s.max == 5.0
    assert s.p05 == s.p95 == s.p99 == 5.0
    assert s.stddev == 0.0


def test_real_zero_vs_missing_and_coverage():
    # 0.0 is a real measured value; None/"" are missing and drop out.
    s = compute_metric_stats([0.0, None, 2.0], total_count=3)
    assert s.valid_count == 2
    assert s.total_count == 3
    assert s.average == 1.0
    assert s.min == 0.0
    assert s.coverage_percent == pytest.approx(200.0 / 3)


def test_non_finite_dropped():
    s = compute_metric_stats([float("nan"), float("inf"), float("-inf"), 4.0])
    assert s.valid_count == 1
    assert s.average == 4.0


def test_no_valid_values_all_none():
    s = compute_metric_stats([None, None], total_count=2)
    assert s.valid_count == 0
    assert s.coverage_percent == 0.0
    assert s.average is None and s.median is None
    assert s.p05 is None and s.p95 is None and s.p99 is None
    assert s.stddev is None


def test_population_stddev():
    assert compute_metric_stats([2.0, 4.0]).stddev == pytest.approx(1.0)


def test_inclusive_percentiles():
    s = compute_metric_stats([0.0, 10.0])
    assert s.p05 == pytest.approx(0.5)
    assert s.p95 == pytest.approx(9.5)
    assert s.p99 == pytest.approx(9.9)


def test_bool_values_rejected():
    # bools must not be counted as numbers.
    s = compute_metric_stats([True, False, 3.0])
    assert s.valid_count == 1
    assert s.average == 3.0


# --- analyze_session --------------------------------------------------------


def _base_metadata(**overrides: str) -> Dict[str, str]:
    md = {
        "schema.version": str(SCHEMA_VERSION),
        "session.id": "sess-1",
        "device.platform": "android",
        "device.model": "Pixel 8",
        "device.os_version": "14",
        "package.bundle_id": "com.example.game",
        "package.label": "Example Game",
    }
    md.update(overrides)
    return md


def test_weighted_jank(tmp_path):
    rows = [
        {"captured_at": "2026-07-11T00:00:00", "frames_total": 100, "frames_janky": 10,
         "frames_small_jank": 7, "frames_big_jank": 3},
        {"captured_at": "2026-07-11T00:00:01", "frames_total": 200, "frames_janky": 10,
         "frames_small_jank": 6, "frames_big_jank": 4},
        {"captured_at": "2026-07-11T00:00:02", "frames_total": 0, "frames_janky": 5},
        {"captured_at": "2026-07-11T00:00:03", "frames_total": None, "frames_janky": 9},
    ]
    p = write_session_csv(str(tmp_path / "s.csv"), _base_metadata(), rows)
    a = analyze_session(p)
    # (10 + 10) / (100 + 200) * 100
    assert a.frames.weighted_jank_percent == pytest.approx(20.0 / 300 * 100)
    assert a.frames.total_frames == 300
    assert a.frames.small_jank == 13
    assert a.frames.big_jank == 7


def test_saved_app_icon_is_exposed_in_list_and_analysis(tmp_path):
    png = b"\x89PNG\r\n\x1a\nfixture"
    icon = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    p = write_session_csv(
        str(tmp_path / "icon.csv"),
        _base_metadata(**{"package.icon": icon}),
        [{"captured_at": "2026-07-11T00:00:00", "fps": 60}],
    )
    assert read_list_item(p).app_icon == icon
    assert analyze_session(p).app_icon == icon


@pytest.mark.parametrize(
    ("platform", "label_dir", "icon_dir"),
    [
        ("android", "labels", "icons"),
        ("ios", "ios-labels", "ios-icons"),
    ],
)
def test_legacy_app_identity_recovers_from_platform_cache(
    monkeypatch, tmp_path, platform, label_dir, icon_dir
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    bundle_id = "com.example.cached"
    label_path = tmp_path / ".cache" / "mpi" / label_dir / f"{bundle_id}.txt"
    icon_path = tmp_path / ".cache" / "mpi" / icon_dir / f"{bundle_id}.png"
    label_path.parent.mkdir(parents=True)
    icon_path.parent.mkdir(parents=True)
    label_path.write_text("  Cached\n Game  ", encoding="utf-8")
    png = b"\x89PNG\r\n\x1a\nlegacy-fixture"
    icon_path.write_bytes(png)
    metadata = _base_metadata(
        **{
            "device.platform": platform,
            "package.bundle_id": bundle_id,
            "package.label": "",
        }
    )
    path = write_session_csv(
        str(tmp_path / "legacy-cache.csv"),
        metadata,
        [{"captured_at": "2026-07-11T00:00:00", "fps": 60}],
    )

    item = read_list_item(path)
    full = analyze_session(path)

    assert item.app_label == "Cached Game"
    assert full.app_label == "Cached Game"
    assert item.app_icon == full.app_icon
    assert item.app_icon == "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def test_untrusted_session_icon_uri_is_not_exposed(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    path = write_session_csv(
        str(tmp_path / "external-icon.csv"),
        _base_metadata(**{"package.icon": "https://example.invalid/tracker.png"}),
        [{"captured_at": "2026-07-11T00:00:00", "fps": 60}],
    )

    assert read_list_item(path).app_icon is None
    assert analyze_session(path).app_icon is None


def test_worst_window_frame_percentiles(tmp_path):
    rows = [
        {"captured_at": "2026-07-11T00:00:00", "frame_p95_ms": 10, "frame_p99_ms": 15},
        {"captured_at": "2026-07-11T00:00:01", "frame_p95_ms": 30, "frame_p99_ms": 35},
        {"captured_at": "2026-07-11T00:00:02", "frame_p95_ms": 20, "frame_p99_ms": None},
    ]
    p = write_session_csv(str(tmp_path / "s.csv"), _base_metadata(), rows)
    a = analyze_session(p)
    assert a.frames.worst_window_p95_ms == 30
    assert a.frames.worst_window_p99_ms == 35


def test_pss_rss_fallback(tmp_path):
    rows = [
        {"captured_at": "2026-07-11T00:00:00", "mem_pss_kb": 0, "mem_rss_kb": 1024},
        {"captured_at": "2026-07-11T00:00:01", "mem_pss_kb": 2048, "mem_rss_kb": 4096},
        {"captured_at": "2026-07-11T00:00:02", "mem_pss_kb": 0, "mem_rss_kb": 0},
    ]
    p = write_session_csv(str(tmp_path / "s.csv"), _base_metadata(), rows)
    a = analyze_session(p)
    mem = a.metrics["app_memory_mb"]
    assert mem.valid_count == 2
    assert mem.min == pytest.approx(1.0)  # RSS fallback: 1024 KB -> 1 MB
    assert mem.max == pytest.approx(2.0)  # PSS: 2048 KB -> 2 MB
    assert a.peak_app_memory_mb == pytest.approx(2.0)


def test_power_abs_negative_current(tmp_path):
    rows = [
        {"captured_at": "2026-07-11T00:00:00", "battery_voltage_mv": 4000, "battery_current_ma": -500},
        {"captured_at": "2026-07-11T00:00:01", "battery_voltage_mv": 4000, "battery_current_ma": 500},
        {"captured_at": "2026-07-11T00:00:02", "battery_voltage_mv": 4000, "battery_current_ma": None},
    ]
    p = write_session_csv(str(tmp_path / "s.csv"), _base_metadata(), rows)
    a = analyze_session(p)
    power = a.metrics["power_mw"]
    assert power.valid_count == 2
    assert power.average == pytest.approx(2000.0)
    assert power.max == pytest.approx(2000.0)


def test_battery_max_vs_hottest_zone_separated(tmp_path):
    rows = [
        {"captured_at": "2026-07-11T00:00:00", "thermal_battery_c": 30, "thermal_cpu_c": 40},
        {"captured_at": "2026-07-11T00:00:01", "thermal_battery_c": 35, "thermal_cpu_c": 50},
    ]
    p = write_session_csv(str(tmp_path / "s.csv"), _base_metadata(), rows)
    a = analyze_session(p)
    assert a.battery_zone == "battery"
    assert a.max_temp_battery == 35
    assert a.hottest_sensor_zone == "cpu"
    assert a.hottest_sensor_max == 50
    cpu_zone = next(z for z in a.temp_zones if z.zone == "cpu")
    assert cpu_zone.start == 40 and cpu_zone.end == 50 and cpu_zone.change == 10


def test_irregular_timestamp_duration_uses_span(tmp_path):
    # Samples nominally 1s apart, but the real span jumps to 100s.
    rows = [
        {"captured_at": "2026-07-11T00:00:00", "duration_sec": 1, "fps": 60},
        {"captured_at": "2026-07-11T00:00:01", "duration_sec": 1, "fps": 60},
        {"captured_at": "2026-07-11T00:01:40", "duration_sec": 1, "fps": 60},
    ]
    p = write_session_csv(str(tmp_path / "s.csv"), _base_metadata(), rows)
    a = analyze_session(p)
    assert a.duration_sec == pytest.approx(100.0)


def test_ios_platform_round_trip(tmp_path):
    md = _base_metadata(**{"device.platform": "ios", "device.model": "iPhone 15"})
    rows = [{"captured_at": "2026-07-11T00:00:00", "fps": 120}]
    p = write_session_csv(str(tmp_path / "s.csv"), md, rows)
    a = analyze_session(p)
    assert a.platform == "ios"  # not forced to android
    assert a.is_legacy is False
    assert a.app_label == "Example Game"


def test_dashboard_cpu_normalizes_raw_by_stable_header_core_count(tmp_path):
    header = [*DEFAULT_HEADER, "cpu0_mhz", "cpu1_mhz", "cpu2_mhz", "cpu3_mhz"]
    rows = [
        {
            "captured_at": "2026-07-11T00:00:00",
            "cpu_percent": 200,
            "cpu_total_percent": 75,
            "cpu_norm": 30,
            "cpu0_mhz": 1000,
            "cpu1_mhz": 1000,
            "cpu2_mhz": 1000,
            "cpu3_mhz": 1000,
        },
        {
            "captured_at": "2026-07-11T00:00:01",
            "cpu_percent": 80,
            "cpu_total_percent": 125,
            # Offline/unknown core frequencies must not shrink the divisor for
            # this row: the session header still proves a four-core device.
            "cpu0_mhz": 800,
        },
    ]
    p = write_session_csv(str(tmp_path / "cpu.csv"), _base_metadata(), rows, header)

    a = analyze_session(p)

    assert a.timeseries["cpu_app"] == pytest.approx([50.0, 20.0])
    assert a.timeseries["cpu_system"] == pytest.approx([75.0, 100.0])
    # Preserve a valid saved frequency-normalized value. Android must not
    # synthesize a missing frequency-weighted value from core-only app CPU.
    assert a.timeseries["cpu_norm"] == [30.0, None]
    for key in ("cpu_app", "cpu_system", "cpu_norm"):
        series = [v for v in a.timeseries[key] if v is not None]
        stats = a.metrics[key]
        assert stats.average == pytest.approx(sum(series) / len(series))
        assert stats.min == min(series)
        assert stats.max == max(series)
        assert all(0.0 <= v <= 100.0 for v in series)


def test_dashboard_cpu_uses_saved_norm_when_core_count_is_unavailable(tmp_path):
    rows = [
        {
            "captured_at": "2026-07-11T00:00:00",
            "cpu_percent": 160,
            "cpu_total_percent": 55,
            "cpu_norm": 20,
        },
        {
            "captured_at": "2026-07-11T00:00:01",
            "cpu_percent": 240,
            "cpu_total_percent": 65,
            "cpu_norm": 30,
        },
    ]
    md = _base_metadata(**{"device.platform": "ios", "device.model": "iPhone 15"})
    p = write_session_csv(str(tmp_path / "ios.csv"), md, rows)

    a = analyze_session(p)

    # iOS does not write per-core frequency columns; cpu_norm is already the
    # app's whole-device/core-normalized percentage.
    assert a.timeseries["cpu_app"] == pytest.approx([20.0, 30.0])
    assert a.timeseries["cpu_norm"] == pytest.approx([20.0, 30.0])
    assert a.metrics["cpu_app"].max == 30.0


def test_ios_missing_norm_may_fallback_to_core_normalized_app_series(tmp_path):
    header = [*DEFAULT_HEADER, "cpu0_mhz", "cpu1_mhz"]
    rows = [
        {
            "captured_at": "2026-07-11T00:00:00",
            "cpu_percent": 80,
            "cpu_total_percent": 50,
        }
    ]
    md = _base_metadata(**{"device.platform": "ios", "device.model": "iPhone"})
    p = write_session_csv(str(tmp_path / "ios-core-columns.csv"), md, rows, header)

    a = analyze_session(p)

    assert a.timeseries["cpu_app"] == [40.0]
    assert a.timeseries["cpu_norm"] == [40.0]


def test_android_saved_norm_does_not_masquerade_as_app_utilization(tmp_path):
    rows = [
        {
            "captured_at": "2026-07-11T00:00:00",
            "cpu_percent": 160,
            "cpu_total_percent": 55,
            "cpu_norm": 20,
        }
    ]
    p = write_session_csv(str(tmp_path / "android-no-cores.csv"), _base_metadata(), rows)

    a = analyze_session(p)

    # Android cpu_norm is a frequency-weighted capacity estimate. Without a
    # trustworthy core divisor it cannot stand in for whole-device app CPU.
    assert a.timeseries["cpu_app"] == [None]
    assert a.timeseries["cpu_norm"] == [20.0]
    assert a.metrics["cpu_app"].coverage_percent == 0.0
    assert a.metrics["cpu_norm"].coverage_percent == 100.0


@pytest.mark.parametrize(
    "core_columns",
    [
        ["cpu0_mhz", "cpu2_mhz"],
        ["cpu0_mhz", "cpu0_mhz", "cpu1_mhz"],
    ],
)
def test_dashboard_cpu_rejects_sparse_or_duplicate_core_headers(tmp_path, core_columns):
    header = [*DEFAULT_HEADER, *core_columns]
    row = {
        "captured_at": "2026-07-11T00:00:00",
        "cpu_percent": 160,
        "cpu_norm": 25,
        "cpu0_mhz": 1000,
        "cpu1_mhz": 1000,
        "cpu2_mhz": 1000,
    }
    p = write_session_csv(str(tmp_path / "bad-cores.csv"), _base_metadata(), [row], header)

    a = analyze_session(p)

    # Android's saved cpu_norm is frequency weighted, so an ambiguous core
    # header must make app utilization unavailable rather than aliasing it.
    assert a.timeseries["cpu_app"] == [None]
    assert a.timeseries["cpu_norm"] == [25.0]


def test_dashboard_cpu_bounds_invalid_and_non_finite_values(tmp_path):
    header = [*DEFAULT_HEADER, "cpu0_mhz", "cpu1_mhz", "cpu2_mhz", "cpu3_mhz"]
    rows = [
        {
            "captured_at": "2026-07-11T00:00:00",
            "cpu_percent": -80,
            "cpu_total_percent": -3,
            "cpu_norm": -2,
        },
        {
            "captured_at": "2026-07-11T00:00:01",
            "cpu_percent": "nan",
            "cpu_total_percent": "inf",
            "cpu_norm": 25,
        },
        {
            "captured_at": "2026-07-11T00:00:02",
            "cpu_percent": "inf",
            "cpu_total_percent": 50,
            "cpu_norm": "nan",
        },
        {
            "captured_at": "2026-07-11T00:00:03",
            "cpu_percent": 900,
            "cpu_total_percent": 200,
            "cpu_norm": 150,
        },
    ]
    p = write_session_csv(str(tmp_path / "invalid-cpu.csv"), _base_metadata(), rows, header)

    a = analyze_session(p)

    assert a.timeseries["cpu_app"] == [0.0, None, None, 100.0]
    assert a.timeseries["cpu_system"] == [0.0, None, 50.0, 100.0]
    assert a.timeseries["cpu_norm"] == [0.0, 25.0, None, 100.0]
    for key in ("cpu_app", "cpu_system", "cpu_norm"):
        assert a.metrics[key].min is not None and a.metrics[key].min >= 0.0
        assert a.metrics[key].max is not None and a.metrics[key].max <= 100.0


def test_legacy_cpu_without_norm_or_core_count_is_unavailable(tmp_path):
    header = [c for c in DEFAULT_HEADER if c != "cpu_norm"]
    md = {
        "device.platform": "android",
        "device.model": "Legacy",
        "package.bundle_id": "com.legacy",
    }
    rows = [
        {
            "captured_at": "2026-07-11T00:00:00",
            "cpu_percent": 40,
            "cpu_total_percent": 80,
        },
        {
            "captured_at": "2026-07-11T00:00:01",
            "cpu_percent": 240,
            "cpu_total_percent": 140,
        },
    ]
    p = write_session_csv(str(tmp_path / "legacy-cpu.csv"), md, rows, header)

    a = analyze_session(p)

    assert a.is_legacy is True
    # Even a raw value below 100 is app CPU summed across cores, not a proven
    # whole-device percentage. Without a core divisor or saved cpu_norm both
    # dashboard app series must remain N/A instead of depending on core count.
    assert a.timeseries["cpu_app"] == [None, None]
    assert a.timeseries["cpu_norm"] == [None, None]
    for key in ("cpu_app", "cpu_norm"):
        stats = a.metrics[key]
        assert stats.valid_count == 0
        assert stats.total_count == 2
        assert stats.coverage_percent == 0.0
        assert stats.average is None
        assert stats.min is None
        assert stats.max is None

    # System CPU is explicitly a whole-device percentage in the CSV contract.
    assert a.timeseries["cpu_system"] == [80.0, 100.0]
    assert a.metrics["cpu_system"].coverage_percent == 100.0


def test_markerless_legacy_csv(tmp_path):
    # No MPI A1/A2 header and no schema.version -> legacy, still analyzable.
    md = {
        "device.platform": "android",
        "device.model": "Legacy",
        "package.bundle_id": "com.legacy",
    }
    rows = [
        {"captured_at": "2026-07-11T00:00:00", "fps": 30},
        {"captured_at": "2026-07-11T00:00:01", "fps": 60},
    ]
    p = write_session_csv(str(tmp_path / "legacy.csv"), md, rows)
    a = analyze_session(p)
    assert a.is_legacy is True
    assert a.schema_version is None
    assert a.metrics["fps"].average == pytest.approx(45.0)


def test_fps_stability(tmp_path):
    # median 60; within +/-20% => [48..72]. 50 and 70 in-band, 30 and 90 out.
    rows = [
        {"captured_at": "2026-07-11T00:00:00", "fps": 60},
        {"captured_at": "2026-07-11T00:00:01", "fps": 50},
        {"captured_at": "2026-07-11T00:00:02", "fps": 70},
        {"captured_at": "2026-07-11T00:00:03", "fps": 30},
        {"captured_at": "2026-07-11T00:00:04", "fps": 90},
    ]
    p = write_session_csv(str(tmp_path / "s.csv"), _base_metadata(), rows)
    a = analyze_session(p)
    assert a.fps_stability_percent == pytest.approx(60.0)  # 3 of 5 in band


def test_list_saved_sessions_newest_first(tmp_path):
    write_session_csv(
        str(tmp_path / "a.csv"),
        _base_metadata(**{"session.id": "a", "capture.started_at": "2026-07-10T00:00:00"}),
        [{"captured_at": "2026-07-10T00:00:00", "fps": 60}],
    )
    write_session_csv(
        str(tmp_path / "b.csv"),
        _base_metadata(**{"session.id": "b"}),
        [{"captured_at": "2026-07-11T00:00:00", "fps": 60}],
    )
    items = list_saved_sessions(str(tmp_path))
    assert len(items) == 2
    assert items[0].session_id == "b"  # 2026-07-11 newest
    assert items[1].session_id == "a"


def test_list_skips_non_session_csv(tmp_path):
    (tmp_path / "junk.csv").write_text("not,a,session\n1,2,3\n", encoding="utf-8")
    write_session_csv(
        str(tmp_path / "ok.csv"),
        _base_metadata(),
        [{"captured_at": "2026-07-11T00:00:00", "fps": 60}],
    )
    items = list_saved_sessions(str(tmp_path))
    assert len(items) == 1
    assert items[0].file_name == "ok.csv"
