"""CSV v2 metadata/columns + parser bug-fix regression tests.

Covers: schema.version + session.id/name + package.label + de-identified device
metadata rows; the iOS-platform / label / serial-hash restoration fixes on load;
new v2 time-series fields; and the report-writer -> analysis-reader bridge.
"""

from __future__ import annotations

import csv
import os
from typing import Dict, List

import pytest

from mpi import api
from mpi.analysis import analyze_session
from mpi.report import build_endpoint_payload, stable_session_id
from mpi.schema import Platform


def _snapshot(captured_at: str, platform: str = "ios") -> dict:
    return {
        "device": {
            "serial": "00008030-ABCDEF",
            "platform": platform,
            "model": "iPhone 15",
            "os_version": "18.0",
            "code_name": "d84ap",
            "hw_ver": "board-x",
        },
        "package": {"bundle_id": "com.test.game", "label": "테스트 게임", "version_name": "2.0"},
        "captured_at": captured_at,
        "duration_sec": 1.0,
        "frames": {
            "fps_estimate": 120.0,
            "total_frames": 120,
            "janky_frames": 3,
            "missed_vsync": 2,
            "slow_ui_thread": 1,
            "frame_deadline_missed": 4,
        },
        "memory": {"pss_total_kb": 250000, "rss_total_kb": 300000, "code_kb": 12000, "stack_kb": 3400},
        "cpu": {"cpu_percent": 60.0, "total_percent": 30.0},
        "network": {
            "rx_bytes": 123456,
            "tx_bytes": 654321,
            "rx_kbps": 12.5,
            "tx_kbps": 6.25,
        },
        "gpu": {"busy_percent": 55.0},
        "thermal": {"zones_c": {"battery": 34.0, "cpu": 48.0}},
        "battery": {"level_percent": 90, "voltage_mv": 4100.0, "current_ma": -800.0},
    }


def _snapshots(platform: str = "ios") -> List[dict]:
    return [_snapshot(f"2026-07-11T10:00:0{i}", platform) for i in range(3)]


def _read_metadata(path: str) -> Dict[str, str]:
    meta: Dict[str, str] = {}
    section = None
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.reader(fh):
            if not row:
                section = None
                continue
            if len(row) >= 2 and row[0] == "# Section":
                section = row[1]
                continue
            if section == "Metadata" and len(row) >= 2 and row[0] != "key":
                meta[row[0]] = row[1]
    return meta


def _export(tmp_path, snaps, **extra) -> str:
    payload = {"save_path": str(tmp_path / "session.csv"), "snapshots": snaps}
    payload.update(extra)
    return api._handle_export_live_csv(payload)["save_path"]


def test_v2_metadata_rows_written(tmp_path):
    target = _export(tmp_path, _snapshots("ios"))
    meta = _read_metadata(target)
    assert meta["schema.version"] == "2"
    assert len(meta["session.id"]) == 16
    assert meta["package.label"] == "테스트 게임"
    assert meta["device.platform"] == "ios"
    assert meta["device.code_name"] == "d84ap"
    assert meta["device.hw_ver"] == "board-x"


def test_ios_model_identity_survives_csv_analysis_and_cloud_payload(tmp_path):
    snapshots = _snapshots("ios")
    for snapshot in snapshots:
        snapshot["device"].update(model="iPhone 18 Pro Max", model_identifier="iPhone19,3", model_number="A3473")
    target = _export(tmp_path, snapshots)
    meta = _read_metadata(target)
    assert meta["device.model_identifier"] == "iPhone19,3"
    assert meta["device.model_number"] == "A3473"
    loaded = api._handle_load_file({"path": target})["snapshots"]
    assert loaded[0].device.model == "iPhone 18 Pro Max"
    assert loaded[0].device.model_identifier == "iPhone19,3"
    assert loaded[0].device.model_number == "A3473"
    analysis = analyze_session(target)
    assert analysis.device_model_identifier == "iPhone19,3"
    assert analysis.device_model_number == "A3473"
    payload = build_endpoint_payload(loaded)
    assert payload["metadata"]["device"]["model_number"] == "A3473"
    assert payload["metadata"]["device"]["model_identifier"] == "iPhone19,3"


def test_platform_and_label_round_trip(tmp_path):
    target = _export(tmp_path, _snapshots("ios"))
    loaded = api._handle_load_file({"path": target})
    snaps = loaded["snapshots"]
    # iOS must not be forced back to Android.
    assert snaps[0].device.platform == Platform.IOS
    # package.label must survive the round-trip.
    assert snaps[0].package.label == "테스트 게임"
    # De-identified device metadata survives.
    assert snaps[0].device.code_name == "d84ap"


def test_v2_timeseries_fields_round_trip(tmp_path):
    target = _export(tmp_path, _snapshots("ios"))
    loaded = api._handle_load_file({"path": target})
    f = loaded["snapshots"][0].frames
    assert f.missed_vsync == 2
    assert f.slow_ui_thread == 1
    assert f.frame_deadline_missed == 4
    m = loaded["snapshots"][0].memory
    assert m.code_kb == 12000
    assert m.stack_kb == 3400
    network = loaded["snapshots"][0].network
    assert network.rx_bytes == 123456
    assert network.tx_bytes == 654321
    assert network.rx_kbps == 12.5
    assert network.tx_kbps == 6.25


def test_session_id_and_name_passthrough(tmp_path):
    target = _export(
        tmp_path,
        _snapshots("ios"),
        session_id="my-stable-id-01",
        session_name="  라운드 A  ",
    )
    meta = _read_metadata(target)
    assert meta["session.id"] == "my-stable-id-01"
    assert meta["session.name"] == "라운드 A"


def test_stable_session_id_is_deterministic():
    a = stable_session_id("serial", "com.x", "2026-07-11T10:00:00")
    b = stable_session_id("serial", "com.x", "2026-07-11T10:00:00")
    c = stable_session_id("serial", "com.x", "2026-07-11T10:00:01")
    assert a == b
    assert a != c
    assert len(a) == 16


def test_endpoint_payload_carries_session_id():
    snaps = [api._snapshot_from_dict(s) for s in _snapshots("android")]
    payload = build_endpoint_payload(snaps, info_hash="deadbeefdeadbeef")
    assert payload["metadata"]["capture"]["session_id"]
    # Explicit override wins.
    payload2 = build_endpoint_payload(snaps, info_hash="deadbeefdeadbeef", session_id="abc")
    assert payload2["metadata"]["capture"]["session_id"] == "abc"


def test_per_core_freq_position_preserved_on_gap(tmp_path):
    """A mid-list empty cpuN_mhz cell must NOT shift later cores down an index."""
    p = tmp_path / "gap.csv"
    with open(p, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["MPI"])
        w.writerow(["deadbeefdeadbeef"])
        w.writerow([])
        w.writerow(["# Section", "Metadata"])
        w.writerow(["key", "value"])
        w.writerow(["device.platform", "android"])
        w.writerow(["package.bundle_id", "com.x"])
        w.writerow([])
        w.writerow(["# Section", "Time-series"])
        w.writerow(["captured_at", "fps", "cpu0_mhz", "cpu1_mhz"])
        # cpu0 empty, cpu1=1500 — core 1 must stay at index 1.
        w.writerow(["2026-07-11T00:00:00", "60", "", "1500"])
    loaded = api._handle_load_file({"path": str(p)})
    freqs = loaded["snapshots"][0].cpu.per_core_freq_mhz
    assert freqs == [0.0, 1500.0]


def test_auto_save_persists_session_name(tmp_path, monkeypatch):
    monkeypatch.setattr(api.os.path, "expanduser", lambda _p: str(tmp_path))
    res = api._handle_auto_save_raw_csv(
        {"snapshots": _snapshots("android"), "session_name": "Login flow test"}
    )
    assert res["saved"] is True
    meta = _read_metadata(res["save_path"])
    assert meta["session.name"] == "Login flow test"


def test_reserve_dest_path_is_atomic_and_unique(tmp_path):
    a = api._reserve_dest_path(str(tmp_path), "capture.csv")
    b = api._reserve_dest_path(str(tmp_path), "capture.csv")
    assert a != b
    assert os.path.basename(a) == "capture.csv"
    assert os.path.basename(b) == "capture_1.csv"
    # Both are reserved (created) so a concurrent caller can't reuse the name.
    assert os.path.isfile(a) and os.path.isfile(b)


def test_report_writer_bridges_to_analysis_reader(tmp_path):
    """A v2 CSV produced by the writer must analyze correctly end-to-end."""
    target = _export(tmp_path, _snapshots("ios"))
    a = analyze_session(target)
    assert a.platform == "ios"
    assert a.is_legacy is False
    assert a.schema_version == 2
    assert a.app_label == "테스트 게임"
    assert a.metrics["fps"].average == pytest.approx(120.0)
    # weighted jank = 3 janky / 120 total over 3 identical samples => 2.5%
    assert a.frames.weighted_jank_percent == pytest.approx(2.5)
    assert a.frames.missed_vsync == 6  # 2 per sample * 3
    assert a.battery_zone == "battery"
    assert a.hottest_sensor_zone == "cpu"
