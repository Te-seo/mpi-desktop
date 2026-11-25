"""Sidecar session-library commands: list / import (dedupe) / load analyses."""

from __future__ import annotations

import csv
import os
import sys
from typing import Dict, List

import pytest

from mpi import api
from mpi.report import generate_csv_report


def _snap(captured_at: str, fps: float = 60.0) -> dict:
    return {
        "device": {"serial": "S1", "platform": "android", "model": "Pixel", "os_version": "14"},
        "package": {"bundle_id": "com.test.game", "label": "Game"},
        "captured_at": captured_at,
        "duration_sec": 1.0,
        "frames": {"fps_estimate": fps, "total_frames": 60, "janky_frames": 1},
        "memory": {"pss_total_kb": 200000},
        "cpu": {"cpu_percent": 40.0},
        "thermal": {"zones_c": {"battery": 33.0}},
        "battery": {"level_percent": 80, "voltage_mv": 4000.0, "current_ma": -500.0},
    }


def _make_session(path: str, start: str, session_id: str = None, count: int = 3) -> str:
    snaps = [api._snapshot_from_dict(_snap(f"{start[:-1]}{i}")) for i in range(count)]
    generate_csv_report(snaps, path, info_hash="deadbeefdeadbeef", session_id=session_id)
    return path


def _write_legacy(path: str, rows: List[Dict[str, object]], model: str = "Legacy") -> str:
    """A v1/header-less CSV: no schema.version / session.id metadata rows."""
    header = ["captured_at", "fps", "frames_total", "frames_janky"]
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["# Section", "Metadata"])
        w.writerow(["key", "value"])
        w.writerow(["device.platform", "android"])
        w.writerow(["device.model", model])
        w.writerow(["package.bundle_id", "com.legacy"])
        w.writerow([])
        w.writerow(["# Section", "Time-series"])
        w.writerow(header)
        for r in rows:
            w.writerow(["" if r.get(c) is None else r.get(c) for c in header])
    return path


# --- list_saved_sessions ----------------------------------------------------


def test_list_saved_sessions_newest_first(tmp_path):
    _make_session(str(tmp_path / "old.csv"), "2026-07-10T10:00:00", session_id="old")
    _make_session(str(tmp_path / "new.csv"), "2026-07-11T10:00:00", session_id="new")
    result = api._handle_list_saved_sessions({"directory": str(tmp_path)})
    sessions = result["sessions"]
    assert len(sessions) == 2
    assert sessions[0].session_id == "new"
    assert sessions[0].app_label == "Game"


def test_list_empty_directory(tmp_path):
    result = api._handle_list_saved_sessions({"directory": str(tmp_path / "nope")})
    assert result["sessions"] == []


@pytest.mark.parametrize(
    ("data", "prefix"),
    [
        (b"\x89PNG\r\n\x1a\nicon", "data:image/png;base64,"),
        (b"RIFF\x04\x00\x00\x00WEBPicon", "data:image/webp;base64,"),
    ],
)
def test_saved_icon_data_uri_uses_the_actual_image_mime(monkeypatch, data, prefix):
    monkeypatch.setattr(api._android_backend, "fetch_app_icon_bytes", lambda *args, **kwargs: data)
    result = api._fetch_app_icon_data_uri("android", "S1", "com.example.game", None)
    assert result is not None and result.startswith(prefix)


# --- delete_saved_sessions --------------------------------------------------


def test_delete_saved_session_accepts_uppercase_csv(monkeypatch, tmp_path):
    managed = tmp_path / "managed"
    managed.mkdir()
    target = _make_session(str(managed / "SESSION.CSV"), "2026-07-11T10:00:00")
    monkeypatch.setattr(api, "_managed_raw_dir", lambda: str(managed))

    result = api._handle_delete_saved_sessions({"paths": [target]})

    assert result["deleted"] == 1
    assert result["failed"] == 0
    assert result["results"][0]["status"] == "deleted"
    assert not os.path.exists(target)


def test_delete_saved_sessions_contains_malformed_and_partial_failures(monkeypatch, tmp_path):
    events = []
    monkeypatch.setattr(api, "_emit", events.append)
    managed = tmp_path / "managed"
    managed.mkdir()
    good = _make_session(str(managed / "good.csv"), "2026-07-11T10:00:00")
    missing = str(managed / "missing.csv")
    not_session = managed / "not-session.csv"
    not_session.write_text("plain,csv\n1,2\n", encoding="utf-8")
    csv_directory = managed / "folder.csv"
    csv_directory.mkdir()
    outside = _make_session(str(tmp_path / "outside.csv"), "2026-07-11T11:00:00")
    traversal = str(managed / ".." / "outside.csv")
    malformed = str(managed / "bad") + "\x00.csv"
    monkeypatch.setattr(api, "_managed_raw_dir", lambda: str(managed))

    result = api._handle_delete_saved_sessions(
        {
            "paths": [
                malformed,
                good,
                good,  # duplicate: first succeeds, second is a per-file miss
                missing,
                str(not_session),
                str(csv_directory),
                outside,
                traversal,
                None,
            ],
            # A renderer cannot widen deletion authority with this field.
            "directory": str(tmp_path),
        }
    )

    assert result["deleted"] == 1
    assert result["failed"] == 8
    assert [item["status"] for item in result["results"]].count("deleted") == 1
    reasons = [item.get("reason", "") for item in result["results"]]
    assert any("outside" in reason for reason in reasons)
    assert any("file not found" in reason for reason in reasons)
    assert any("not a saved session CSV" in reason for reason in reasons)
    assert any("not a file" in reason for reason in reasons)
    assert not os.path.exists(good)
    assert os.path.exists(outside)
    assert not_session.exists()
    assert events == [{"event": "session_library_changed"}]


def test_failed_delete_does_not_notify_library(monkeypatch, tmp_path):
    events = []
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setattr(api, "_managed_raw_dir", lambda: str(managed))
    monkeypatch.setattr(api, "_emit", events.append)

    result = api._handle_delete_saved_sessions({"paths": [str(managed / "missing.csv")]})

    assert result["deleted"] == 0
    assert result["failed"] == 1
    assert events == []


def test_delete_saved_session_rejects_symlink(monkeypatch, tmp_path):
    managed = tmp_path / "managed"
    managed.mkdir()
    target = _make_session(str(managed / "target.csv"), "2026-07-11T10:00:00")
    link = managed / "link.csv"
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")
    monkeypatch.setattr(api, "_managed_raw_dir", lambda: str(managed))

    result = api._handle_delete_saved_sessions({"paths": [str(link)]})

    assert result["deleted"] == 0
    assert result["failed"] == 1
    assert "symbolic-link" in result["results"][0]["reason"]
    assert os.path.exists(target)
    assert link.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows handle-delete race regression")
def test_delete_saved_session_is_pinned_when_parent_reparse_is_retargeted(monkeypatch, tmp_path):
    managed = tmp_path / "managed"
    inside = managed / "inside"
    outside = tmp_path / "outside"
    inside.mkdir(parents=True)
    outside.mkdir()
    inside_target = _make_session(str(inside / "race.csv"), "2026-07-11T10:00:00")
    outside_target = _make_session(str(outside / "race.csv"), "2026-07-11T11:00:00")
    parent_link = managed / "current"

    def point_at(target):
        try:
            parent_link.symlink_to(target, target_is_directory=True)
        except (NotImplementedError, OSError) as exc:
            pytest.skip(f"directory reparse points are unavailable: {exc}")

    def remove_link():
        try:
            parent_link.unlink()
        except IsADirectoryError:
            os.rmdir(parent_link)

    # Preflight retargeting before opening the file so environmental failures
    # do not masquerade as a delete implementation regression.
    point_at(inside)
    try:
        remove_link()
        point_at(outside)
        remove_link()
        point_at(inside)
    except OSError as exc:
        pytest.skip(f"directory reparse retargeting is unavailable: {exc}")

    candidate = str(parent_link / "race.csv")
    original_validate = api._validate_saved_session_stream
    retargeted = False

    def validate_then_retarget(fh):
        nonlocal retargeted
        original_validate(fh)
        remove_link()
        point_at(outside)
        retargeted = True

    monkeypatch.setattr(api, "_validate_saved_session_stream", validate_then_retarget)
    monkeypatch.setattr(api, "_managed_raw_dir", lambda: str(managed))

    result = api._handle_delete_saved_sessions({"paths": [candidate]})

    assert retargeted
    assert result["deleted"] == 1
    assert not os.path.exists(inside_target)
    assert os.path.exists(outside_target)


def test_delete_saved_sessions_requires_non_empty_paths(monkeypatch):
    events = []
    monkeypatch.setattr(api, "_emit", events.append)
    with pytest.raises(ValueError, match="non-empty"):
        api._handle_delete_saved_sessions({"paths": []})
    assert events == []


# --- import_session_files ---------------------------------------------------


def test_import_new_then_duplicate_by_session_id(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(api, "_emit", events.append)
    src = _make_session(str(tmp_path / "src.csv"), "2026-07-11T10:00:00", session_id="fixed-id")
    dest = tmp_path / "dest"
    dest.mkdir()

    r1 = api._handle_import_session_files({"paths": [src], "directory": str(dest)})
    assert r1["imported"] == 1
    assert r1["results"][0]["status"] == "imported"
    assert os.path.isfile(r1["results"][0]["path"])
    assert events == [{"event": "session_library_changed"}]
    events.clear()

    # Re-importing the same session id is a duplicate, not a second copy.
    r2 = api._handle_import_session_files({"paths": [src], "directory": str(dest)})
    assert r2["imported"] == 0
    assert r2["duplicates"] == 1
    assert r2["results"][0]["status"] == "duplicate"
    assert events == []


def test_import_legacy_dedupe_by_content_fingerprint(tmp_path):
    rows = [
        {"captured_at": "2026-07-11T10:00:00", "fps": 60, "frames_total": 60, "frames_janky": 1},
        {"captured_at": "2026-07-11T10:00:01", "fps": 59, "frames_total": 60, "frames_janky": 2},
    ]
    # Same time-series content, different metadata model -> same fingerprint.
    a = _write_legacy(str(tmp_path / "a.csv"), rows, model="DeviceA")
    b = _write_legacy(str(tmp_path / "b.csv"), rows, model="DeviceB")
    dest = tmp_path / "dest"
    dest.mkdir()
    result = api._handle_import_session_files({"paths": [a, b], "directory": str(dest)})
    assert result["imported"] == 1
    assert result["duplicates"] == 1


def test_import_partial_failure(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(api, "_emit", events.append)
    good = _make_session(str(tmp_path / "good.csv"), "2026-07-11T10:00:00", session_id="g")
    missing = str(tmp_path / "missing.csv")
    corrupt = tmp_path / "corrupt.csv"
    corrupt.write_text("just,garbage\n1,2\n", encoding="utf-8")
    dest = tmp_path / "dest"
    dest.mkdir()

    result = api._handle_import_session_files(
        {"paths": [good, missing, str(corrupt)], "directory": str(dest)}
    )
    assert result["imported"] == 1
    assert result["failed"] == 2
    statuses = {r["file_name"]: r["status"] for r in result["results"]}
    assert statuses["good.csv"] == "imported"
    assert statuses["missing.csv"] == "failed"
    assert statuses["corrupt.csv"] == "failed"
    assert events == [{"event": "session_library_changed"}]


def test_failed_import_does_not_notify_library(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(api, "_emit", events.append)

    result = api._handle_import_session_files({
        "paths": [str(tmp_path / "missing.csv")], "directory": str(tmp_path / "dest"),
    })

    assert result["imported"] == 0
    assert result["failed"] == 1
    assert events == []


def test_import_empty_paths_raises(monkeypatch):
    events = []
    monkeypatch.setattr(api, "_emit", events.append)
    with pytest.raises(ValueError):
        api._handle_import_session_files({"paths": []})
    assert events == []


# --- load_session_analyses --------------------------------------------------


def test_load_session_analyses(tmp_path):
    a = _make_session(str(tmp_path / "a.csv"), "2026-07-11T10:00:00", session_id="a")
    b = _make_session(str(tmp_path / "b.csv"), "2026-07-11T11:00:00", session_id="b")
    result = api._handle_load_session_analyses({"paths": [a, b]})
    assert len(result["analyses"]) == 2
    assert result["errors"] == []
    assert result["dropped"] == 0
    assert result["analyses"][0].metrics["fps"].average == pytest.approx(60.0)


def test_load_session_analyses_caps_at_four(tmp_path):
    paths = [
        _make_session(str(tmp_path / f"s{i}.csv"), f"2026-07-11T10:00:0{i}", session_id=f"s{i}")
        for i in range(5)
    ]
    result = api._handle_load_session_analyses({"paths": paths})
    assert len(result["analyses"]) == 4
    assert result["dropped"] == 1


def test_load_session_analyses_collects_errors(tmp_path):
    good = _make_session(str(tmp_path / "good.csv"), "2026-07-11T10:00:00", session_id="g")
    bad = tmp_path / "bad.csv"
    bad.write_text("nope\n", encoding="utf-8")
    result = api._handle_load_session_analyses({"paths": [good, str(bad)]})
    assert len(result["analyses"]) == 1
    assert len(result["errors"]) == 1
    assert result["errors"][0]["path"] == str(bad)


def test_load_session_analyses_applies_per_path_range(tmp_path):
    a = _make_session(str(tmp_path / "a.csv"), "2026-07-11T10:00:00", session_id="a", count=5)
    result = api._handle_load_session_analyses({
        "paths": [a],
        "ranges": [{"path": a, "start_sec": 1, "end_sec": 3}],
    })
    analysis = result["analyses"][0]
    assert analysis.sample_count == 3
    assert analysis.duration_sec == pytest.approx(2.0)
    assert analysis.timeseries["t"] == [0.0, 1.0, 2.0]
    assert analysis.frames.total_frames == 180


def test_load_session_analyses_rejects_empty_range_without_failing_batch(tmp_path):
    a = _make_session(str(tmp_path / "a.csv"), "2026-07-11T10:00:00", session_id="a")
    result = api._handle_load_session_analyses({
        "paths": [a],
        "ranges": [{"path": a, "start_sec": 99, "end_sec": 100}],
    })
    assert result["analyses"] == []
    assert result["errors"][0]["path"] == a
    assert "no samples" in result["errors"][0]["error"]
