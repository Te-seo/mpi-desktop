"""Live raw-CSV export/load: MPI header, Raw folder, new columns, round-trip.

Covers the spec contract:
  * Saved CSV carries A1="MPI" / A2=<16-hex hash> and lands in a Raw/ subfolder.
  * Loading validates the header and replays the full dashboard (replay=True),
    preserving the extended columns (system CPU%, normalized CPU%, system RAM).
  * A non-MPI live CSV still loads (offline path), but replay=False.
"""

from __future__ import annotations

import csv
import os
import re
from datetime import datetime

import pytest

from mpi import api
from mpi.report import MPI_MARKER, gen_info_hash, generate_csv_report
from mpi.schema import (
    BatteryStats,
    CpuStats,
    DeviceInfo,
    GpuStats,
    MemStats,
    MetricsSnapshot,
    PackageInfo,
    Platform,
    ThermalStats,
)


def _snapshot(captured_at: str, i: int) -> dict:
    return {
        "device": {
            "serial": "192.168.0.10:5555",
            "platform": "android",
            "display_name": "QA Phone",
            "model": "GalaxyTest",
            "os_version": "14",
        },
        "package": {"bundle_id": "com.test.game", "label": "TestGame"},
        "captured_at": captured_at,
        "duration_sec": 1.0,
        "frames": {"fps_estimate": 60.0 - i},
        "memory": {"rss_total_kb": 100000, "sys_total_kb": 8000000, "sys_used_kb": 4000000 + i},
        "cpu": {
            "cpu_percent": 150.0 + i,
            "total_percent": 40.0 + i,
            "cpu_norm": 18.0 + i,
            "per_core_freq_mhz": [1800.0, 1800.0, 2200.0, 2200.0],
        },
        "gpu": {"busy_percent": 50.0, "clock_mhz": 500.0},
        "thermal": {"zones_c": {"battery": 33.0 + i, "cpu_max": 55.0}},
        "battery": {"level_percent": 80, "voltage_mv": 4000.0, "current_ma": -1000.0},
    }


def _snapshots() -> list[dict]:
    times = [f"2026-06-26T10:00:0{i}+00:00" for i in range(3)]
    return [_snapshot(t, i) for i, t in enumerate(times)]


def test_export_writes_mpi_header_into_raw_subfolder(tmp_path):
    save_path = tmp_path / "session.csv"
    result = api._handle_export_live_csv(
        {"save_path": str(save_path), "snapshots": _snapshots()}
    )
    target = result["save_path"]
    # Routed into a Raw/ subfolder beside the chosen path.
    assert os.path.basename(os.path.dirname(target)) == "Raw"
    assert os.path.isfile(target)
    with open(target, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0][0] == MPI_MARKER
    assert re.fullmatch(r"[0-9a-f]{16}", rows[1][0])


def test_auto_save_raw_csv_default_location(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(api, "_emit", events.append)
    monkeypatch.setattr(api.os.path, "expanduser", lambda _p: str(tmp_path))
    result = api._handle_auto_save_raw_csv({"snapshots": _snapshots()})
    assert result["saved"] is True
    target = result["save_path"]
    assert os.path.isfile(target)
    assert os.path.basename(os.path.dirname(target)) == "Raw"
    assert "MPI" in target
    assert events == [{"event": "session_library_changed"}]


def test_late_close_backup_is_separate_from_complete_csv_and_session_library(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(api, "_emit", events.append)
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 10, 12, 0, 0, tzinfo=tz)

    managed = tmp_path / "Documents" / "MPI" / "Raw"
    monkeypatch.setattr(api, "datetime", FixedDateTime)
    monkeypatch.setattr(api, "_managed_raw_dir", lambda: str(managed))
    monkeypatch.setattr(api, "_legacy_managed_raw_dir", lambda: str(tmp_path / "legacy" / "Raw"))
    monkeypatch.setattr(api, "_fetch_app_icon_data_uri", lambda *_args: None)
    session_id = "close-session"
    complete = api._handle_auto_save_raw_csv({
        "snapshots": _snapshots(), "session_id": session_id, "session_name": "Complete capture",
    })
    assert events == [{"event": "session_library_changed"}]
    events.clear()
    # A delayed fallback finishes after the complete save in the same clock second.
    backup = api._handle_auto_save_raw_csv({
        "snapshots": _snapshots()[:1], "session_id": session_id,
        "session_name": "Close backup", "close_backup": True,
    })
    assert events == []
    assert complete["save_path"] != backup["save_path"]
    assert os.path.dirname(complete["save_path"]) == str(managed)
    assert os.path.dirname(backup["save_path"]) == str(managed.parent / "Recovery" / "Raw")
    assert os.path.basename(complete["save_path"]) == os.path.basename(backup["save_path"])
    assert len(api._handle_load_file({"path": complete["save_path"]})["snapshots"]) == 3
    assert len(api._handle_load_file({"path": backup["save_path"]})["snapshots"]) == 1
    for saved in (complete, backup):
        with open(saved["save_path"], encoding="utf-8-sig", newline="") as stream:
            assert ["session.id", session_id] in list(csv.reader(stream))
    library = api._handle_list_saved_sessions({})["sessions"]
    assert len(library) == 1
    assert library[0].session_id == session_id
    assert library[0].sample_count == 3
    assert library[0].file_path == complete["save_path"]


def test_auto_save_empty_session_is_noop(monkeypatch):
    events = []
    monkeypatch.setattr(api, "_emit", events.append)
    assert api._handle_auto_save_raw_csv({"snapshots": []})["saved"] is False
    assert events == []


def test_failed_auto_save_does_not_notify_library(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(api, "_emit", events.append)
    monkeypatch.setattr(api.os.path, "expanduser", lambda _p: str(tmp_path))

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(api, "_write_live_csv", fail_write)
    with pytest.raises(OSError, match="disk full"):
        api._handle_auto_save_raw_csv({"snapshots": _snapshots()})
    assert events == []


def test_validate_mpi_header(tmp_path):
    good = tmp_path / "good.csv"
    snaps = [api._snapshot_from_dict(s) for s in _snapshots()]
    h = gen_info_hash("S", "p", "2026-06-26T00:00:00")
    generate_csv_report(snaps, str(good), info_hash=h)
    assert api._validate_mpi_header(str(good)) == h

    legacy = tmp_path / "legacy-header.csv"
    legacy.write_text(
        f"PerfInspect\n{h}\n\n# Section,Metadata\n",
        encoding="utf-8",
    )
    assert api._validate_mpi_header(str(legacy)) == h

    # No MPI marker -> None (legacy/offline file, not an error).
    plain = tmp_path / "plain.csv"
    generate_csv_report(snaps, str(plain))
    assert api._validate_mpi_header(str(plain)) is None

    # MPI marker but malformed hash -> error.
    bad = tmp_path / "bad.csv"
    bad.write_text("MPI\nNOTAHASH\n\n# Section,Metadata\n", encoding="utf-8")
    with pytest.raises(ValueError):
        api._validate_mpi_header(str(bad))


def test_round_trip_preserves_extended_columns_and_replays(tmp_path):
    save_path = tmp_path / "session.csv"
    target = api._handle_export_live_csv(
        {"save_path": str(save_path), "snapshots": _snapshots()}
    )["save_path"]

    loaded = api._handle_load_file({"path": target})
    assert loaded["source"] == "live_csv"
    assert loaded["replay"] is True
    assert re.fullmatch(r"[0-9a-f]{16}", loaded["mpi_hash"])

    snaps = loaded["snapshots"]
    assert len(snaps) == 3
    assert snaps[0].device.display_name == "QA Phone"
    assert snaps[0].device.model == "GalaxyTest"
    # Handler returns dataclass instances; IPC serializes them later.
    cpu1 = snaps[1].cpu
    assert cpu1.cpu_percent == 151.0
    assert cpu1.total_percent == 41.0
    assert cpu1.cpu_norm == 19.0
    mem1 = snaps[1].memory
    assert mem1.sys_total_kb == 8000000
    assert mem1.sys_used_kb == 4000001


def test_non_mpi_live_csv_loads_without_replay(tmp_path):
    snaps = [api._snapshot_from_dict(s) for s in _snapshots()]
    plain = tmp_path / "legacy_live.csv"
    generate_csv_report(snaps, str(plain))  # no info_hash -> no MPI header
    loaded = api._handle_load_file({"path": str(plain)})
    assert loaded["source"] == "live_csv"
    assert loaded["replay"] is False
    assert loaded["mpi_hash"] is None


def test_csv_replay_preserves_missing_app_cpu_as_unavailable(tmp_path):
    snapshots = [api._snapshot_from_dict(_snapshot("2026-06-26T10:00:00+00:00", 0))]
    path = tmp_path / "missing-app-cpu.csv"
    generate_csv_report(snapshots, str(path))

    rows = []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    header_index = next(i for i, row in enumerate(rows) if row and row[0] == "captured_at")
    cpu_index = rows[header_index].index("cpu_percent")
    rows[header_index + 1][cpu_index] = ""
    with open(path, "w", encoding="utf-8", newline="") as fh:
        csv.writer(fh).writerows(rows)

    loaded = api._handle_load_file({"path": str(path)})

    assert loaded["snapshots"][0].cpu.cpu_percent is None
    assert loaded["raw"]["cpu_usage"] is None
    assert loaded["report"].cpu_usage_avg is None


def test_live_summary_converts_raw_app_cpu_to_whole_device_share():
    snapshots = [api._snapshot_from_dict(_snapshot("2026-06-26T10:00:00+00:00", 0))]

    summary = api._live_summary_from_snapshots(snapshots)

    assert summary["cpu_usage"] == 37.5


@pytest.mark.parametrize("bad_layout", ["sparse", "duplicate"])
def test_csv_replay_rejects_untrusted_cpu_frequency_headers(tmp_path, bad_layout):
    snapshots = [api._snapshot_from_dict(_snapshot("2026-06-26T10:00:00+00:00", 0))]
    path = tmp_path / f"{bad_layout}-cpu-columns.csv"
    info_hash = gen_info_hash("SERIAL", "com.test.game", "2026-06-26T10:00:00")
    generate_csv_report(snapshots, str(path), info_hash=info_hash)

    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    header_index = next(i for i, row in enumerate(rows) if row and row[0] == "captured_at")
    header = rows[header_index]
    cpu1_index = header.index("cpu1_mhz")
    header[cpu1_index] = "cpu4_mhz" if bad_layout == "sparse" else "cpu0_mhz"
    with open(path, "w", encoding="utf-8", newline="") as fh:
        csv.writer(fh).writerows(rows)

    loaded = api._handle_load_file({"path": str(path)})

    assert loaded["replay"] is True
    assert loaded["snapshots"][0].cpu.per_core_freq_mhz == []
    assert loaded["snapshots"][0].cpu.cpu_norm == 18.0
    assert loaded["raw"]["cpu_usage"] is None
    assert loaded["report"].cpu_usage_avg is None
