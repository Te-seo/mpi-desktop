from __future__ import annotations

import csv
from pathlib import Path
from typing import Dict, List, Optional

import pytest

from mpi.report import build_endpoint_payload, generate_csv_report, hash_serial
from mpi.schema import (
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
)


def _make_snap(
    captured_at: str = "2026-04-27T10:00:00",
    fps: Optional[float] = 60.0,
    cores: Optional[List[float]] = None,
    zones: Optional[Dict[str, float]] = None,
    jank_percent: float = 2.0,
) -> MetricsSnapshot:
    return MetricsSnapshot(
        device=DeviceInfo(
            serial="ABC123",
            platform=Platform.ANDROID,
            model="Pixel 7",
            os_version="14",
        ),
        package=PackageInfo(
            bundle_id="com.example.game",
            version_name="1.2.3",
            version_code="123",
            uid="u0a100",
        ),
        captured_at=captured_at,
        duration_sec=1.0,
        frames=FrameStats(
            total_frames=60,
            janky_frames=1,
            jank_percent=jank_percent,
            p50_ms=8.0,
            p90_ms=12.0,
            p95_ms=15.0,
            p99_ms=22.0,
            fps_estimate=fps,
        ),
        memory=MemStats(
            pss_total_kb=400_000,
            rss_total_kb=600_000,
            java_heap_kb=80_000,
            native_heap_kb=120_000,
            graphics_kb=50_000,
        ),
        cpu=CpuStats(
            pid=4321,
            cpu_percent=25.0,
            mem_percent=8.0,
            per_core_freq_mhz=cores if cores is not None else [1800.0, 1800.0],
        ),
        gpu=GpuStats(busy_percent=40.0, clock_mhz=600.0, temp_c=45.0),
        thermal=ThermalStats(
            zones_c=zones if zones is not None else {"cpu": 50.0, "skin": 35.0}
        ),
        battery=BatteryStats(
            level_percent=80, current_ma=-150.0, voltage_mv=3850.0, status="Discharging"
        ),
    )


def _read_rows(path: Path) -> List[List[str]]:
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return list(csv.reader(fh))


def test_report_sections_include_separate_frame_event_statistics(tmp_path: Path) -> None:
    snaps = [_make_snap(captured_at=f"2026-04-27T10:00:0{i}") for i in range(3)]
    out = tmp_path / "report.csv"
    generate_csv_report(snaps, str(out))

    rows = _read_rows(out)
    section_markers = [
        (i, row[1])
        for i, row in enumerate(rows)
        if row and row[0] == "# Section"
    ]
    assert [name for _, name in section_markers] == [
        "Metadata",
        "Summary statistics",
        "Frame event statistics",
        "Time-series",
    ]
    # Blank row separator before each non-first section marker.
    for idx, _ in section_markers[1:]:
        assert rows[idx - 1] == [], f"expected blank row before section at {idx}"


def test_summary_stats_known_inputs(tmp_path: Path) -> None:
    snaps = [
        _make_snap(captured_at="2026-04-27T10:00:00", fps=30.0),
        _make_snap(captured_at="2026-04-27T10:00:01", fps=60.0),
        _make_snap(captured_at="2026-04-27T10:00:02", fps=90.0),
    ]
    out = tmp_path / "report.csv"
    generate_csv_report(snaps, str(out))

    rows = _read_rows(out)
    fps_row = next(r for r in rows if r and r[0] == "fps")
    # columns: metric, avg, min, max, p50, p95, p99
    assert fps_row[1] == "60"  # avg
    assert fps_row[2] == "30"  # min
    assert fps_row[3] == "90"  # max
    assert fps_row[4] == "60"  # p50


def test_variable_cpu_cores_and_thermal_zones(tmp_path: Path) -> None:
    snaps = [
        _make_snap(
            captured_at="2026-04-27T10:00:00",
            cores=[1000.0, 1000.0, 1000.0, 1000.0],
            zones={"cpu": 50.0},
        ),
        _make_snap(
            captured_at="2026-04-27T10:00:01",
            cores=[2000.0] * 8,
            zones={"gpu": 60.0, "skin": 35.0},
        ),
    ]
    out = tmp_path / "report.csv"
    generate_csv_report(snaps, str(out))

    rows = _read_rows(out)
    # Find the time-series header row (right after "# Section, Time-series")
    ts_marker = next(i for i, r in enumerate(rows) if r and r[0] == "# Section" and r[1] == "Time-series")
    header = rows[ts_marker + 1]
    # 8 cpu columns (max across snapshots).
    cpu_cols = [c for c in header if c.startswith("cpu") and c.endswith("_mhz")]
    assert cpu_cols == [f"cpu{i}_mhz" for i in range(8)]
    # Three thermal zone columns: union sorted alphabetically.
    thermal_cols = [c for c in header if c.startswith("thermal_") and c.endswith("_c")]
    assert thermal_cols == ["thermal_cpu_c", "thermal_gpu_c", "thermal_skin_c"]

    # First data row: 4 cores filled, last 4 empty; cpu zone present, gpu+skin empty.
    data_row_1 = rows[ts_marker + 2]
    cpu_idx = header.index("cpu0_mhz")
    assert data_row_1[cpu_idx] == "1000"
    assert data_row_1[cpu_idx + 3] == "1000"
    assert data_row_1[cpu_idx + 4] == ""  # 5th core missing
    assert data_row_1[cpu_idx + 7] == ""  # 8th core missing
    assert data_row_1[header.index("thermal_cpu_c")] == "50"
    assert data_row_1[header.index("thermal_gpu_c")] == ""
    assert data_row_1[header.index("thermal_skin_c")] == ""


def test_none_values_become_empty_cells(tmp_path: Path) -> None:
    snaps = [_make_snap(fps=None), _make_snap(captured_at="2026-04-27T10:00:01", fps=None)]
    out = tmp_path / "report.csv"
    generate_csv_report(snaps, str(out))

    rows = _read_rows(out)
    # Stat row for fps is all-empty (all values were None).
    fps_stat_row = next(r for r in rows if r and r[0] == "fps")
    assert fps_stat_row == ["fps", "", "", "", "", "", ""]
    # Time-series fps cells are empty, not the literal string "None".
    ts_marker = next(i for i, r in enumerate(rows) if r and r[0] == "# Section" and r[1] == "Time-series")
    header = rows[ts_marker + 1]
    fps_idx = header.index("fps")
    for data_row in rows[ts_marker + 2 :]:
        if not data_row:
            continue
        assert data_row[fps_idx] == ""
        assert "None" not in data_row


def test_network_columns_and_endpoint_payload_preserve_counters_and_rates(tmp_path: Path) -> None:
    snapshots = [_make_snap(captured_at="2026-04-27T10:00:00"), _make_snap(captured_at="2026-04-27T10:00:01")]
    snapshots[0].network = NetworkStats(rx_bytes=1000, tx_bytes=2000)
    snapshots[1].network = NetworkStats(
        rx_bytes=3048,
        tx_bytes=4048,
        rx_kbps=2.0,
        tx_kbps=2.0,
    )
    out = tmp_path / "network.csv"
    generate_csv_report(snapshots, str(out))

    rows = _read_rows(out)
    ts_marker = next(i for i, r in enumerate(rows) if r and r[0] == "# Section" and r[1] == "Time-series")
    header = rows[ts_marker + 1]
    first, second = rows[ts_marker + 2 : ts_marker + 4]
    assert [c for c in header if c.startswith("network_")] == [
        "network_rx_bytes",
        "network_tx_bytes",
        "network_rx_kbps",
        "network_tx_kbps",
    ]
    assert first[header.index("network_rx_bytes")] == "1000"
    assert first[header.index("network_rx_kbps")] == ""
    assert second[header.index("network_tx_bytes")] == "4048"
    assert second[header.index("network_tx_kbps")] == "2"

    payload = build_endpoint_payload(snapshots)
    network_offset = header.index("network_rx_bytes")
    assert payload["timeseries"]["columns"][network_offset : network_offset + 4] == [
        "network_rx_bytes",
        "network_tx_bytes",
        "network_rx_kbps",
        "network_tx_kbps",
    ]
    assert payload["timeseries"]["rows"][1][network_offset : network_offset + 4] == [3048, 4048, 2.0, 2.0]
    assert payload["summary"]["network_rx_kbps"]["avg"] == 2.0


def test_empty_input_raises_value_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        generate_csv_report([], str(tmp_path / "should_not_exist.csv"))


def test_endpoint_payload_hashes_serial_and_omits_raw() -> None:
    """The outbound payload must carry serial_hash, never the raw serial
    (PIPA/GDPR data minimization — the desktop hashes before sending)."""
    import hashlib
    import json

    snaps = [_make_snap(captured_at=f"2026-04-27T10:00:0{i}") for i in range(2)]
    payload = build_endpoint_payload(snaps, info_hash="deadbeefdeadbeef")
    device = payload["metadata"]["device"]

    assert "serial" not in device, "raw serial must not be in the payload"
    assert device["serial_hash"] == hashlib.sha256(b"ABC123").hexdigest()
    assert device["serial_hash"] == hash_serial("ABC123")
    # The whole serialized payload must not leak the raw serial anywhere.
    assert "ABC123" not in json.dumps(payload)


def test_endpoint_payload_duration_is_wall_clock_span_not_sample_sum() -> None:
    """capture.duration_total_sec must be the real elapsed span (first -> last
    captured_at), not the sum of nominal per-sample intervals, so the cloud
    dashboard shows the true measurement time even when sampling is irregular."""
    # Two samples 5 s apart, each with a nominal duration_sec of 1.0 (sum would
    # be 2.0). The real elapsed span is 5.0.
    snaps = [
        _make_snap(captured_at="2026-04-27T10:00:00"),
        _make_snap(captured_at="2026-04-27T10:00:05"),
    ]
    payload = build_endpoint_payload(snaps, info_hash="deadbeefdeadbeef")
    assert payload["metadata"]["capture"]["duration_total_sec"] == 5.0


def test_endpoint_payload_carries_session_name() -> None:
    """An explicit session name round-trips to metadata.capture.name; when absent
    the key is present but null (stable payload shape for the ingest reader)."""
    snaps = [_make_snap(captured_at=f"2026-04-27T10:00:0{i}") for i in range(2)]

    named = build_endpoint_payload(snaps, info_hash="deadbeefdeadbeef", name="  My Run  ")
    assert named["metadata"]["capture"]["name"] == "My Run"

    anon = build_endpoint_payload(snaps, info_hash="deadbeefdeadbeef")
    assert "name" in anon["metadata"]["capture"]
    assert anon["metadata"]["capture"]["name"] is None


def test_hash_serial_handles_empty() -> None:
    import hashlib

    assert hash_serial("") == hashlib.sha256(b"").hexdigest()
