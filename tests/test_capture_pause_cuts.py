"""Pause timing, in-flight samples, and durable non-destructive session cuts."""
import threading
from queue import Queue
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from mpi import api
from mpi.analysis import _read_sections, analyze_session, read_list_item
from mpi.report import generate_csv_report
from mpi.schema import DeviceInfo, MetricsSnapshot, PackageInfo, Platform
from mpi.session_cuts import save_cuts, validate_cuts


def snapshot(second, elapsed):
    return MetricsSnapshot(
        device=DeviceInfo(serial="test", model="Test", os_version="16", platform=Platform.ANDROID),
        package=PackageInfo(bundle_id="com.test"),
        captured_at=f"2026-10-25T00:{second // 60:02}:{second % 60:02}+00:00",
        duration_sec=1.0, elapsed_sec=elapsed,
    )


def cut(start=0, end=2):
    return {"id": "cut-1", "name": "구간 A", "start_sec": start, "end_sec": end}


def context(monkeypatch, session=None):
    ctx = api._CaptureContext(threading.Thread(), threading.Event(), session or SimpleNamespace(), 1.0, "pause-test")
    monkeypatch.setattr(api, "_active_captures", {"pause-test": ctx})
    monkeypatch.setattr(api, "_emit", lambda event: None)
    return ctx


def test_pause_freezes_clock_and_auto_stop_extends_exactly_once(monkeypatch):
    ctx = context(monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(api.time, "monotonic", lambda: clock[0])
    timers = []

    class Timer:
        def __init__(self, delay, *_args, **_kwargs):
            self.delay = delay
            self.cancelled = False
            timers.append(self)
        def start(self): pass
        def cancel(self): self.cancelled = True

    monkeypatch.setattr(api.threading, "Timer", Timer)
    ctx.measurement_started_at = 90.0
    ctx.auto_stop_after_sec = 30.0
    api._arm_capture_auto_stop("pause-test")
    assert timers[0].delay == 20
    assert api._handle_pause_capture({"capture_id": "pause-test"})["elapsed_sec"] == 10
    assert timers[0].cancelled
    clock[0] = 200
    api._auto_stop_capture("pause-test")
    assert not ctx.stop.is_set()
    assert api._handle_pause_capture({"capture_id": "pause-test"})["elapsed_sec"] == 10
    assert api._handle_resume_capture({"capture_id": "pause-test"})["elapsed_sec"] == 10
    assert ctx.auto_stop_deadline == 220
    assert timers[-1].delay == 20
    api._handle_resume_capture({"capture_id": "pause-test"})
    assert ctx.paused_total_sec == 100
    clock[0] = 205
    assert api._capture_elapsed(ctx) == 15


@pytest.mark.parametrize("pauses", [0, 1, 3])
def test_collector_emits_sticky_pause_evidence_on_samples_and_end(monkeypatch, pauses):
    entered, release = Queue(), Queue()
    events = []
    resets = []

    class Session:
        def reset_observation_window(self):
            resets.append(threading.current_thread())

        def sample(self, duration):
            entered.put(True)
            release.get(timeout=3)
            return snapshot(1, None)
        def close(self): release.put(True)

    ctx = context(monkeypatch, Session())
    monkeypatch.setattr(api, "_emit", events.append)
    thread = threading.Thread(target=api._capture_pump, args=("pause-test", ctx.session, ctx.stop, 1.0))
    ctx.thread = thread
    thread.start()
    try:
        entered.get(timeout=3)
        release.put(True)
        entered.get(timeout=3)  # First unpaused sample has been emitted.
        for _ in range(pauses):
            assert api._handle_pause_capture({"capture_id": "pause-test"})["pause_used"] is True
            assert api._handle_resume_capture({"capture_id": "pause-test"})["pause_used"] is True
            release.put(True)  # Discard sample straddling the transition.
            entered.get(timeout=3)
        release.put(True)
        entered.get(timeout=3)
        samples = [e["data"] for e in events if e["event"] == "snapshot"]
        assert resets == [thread] * pauses
        assert samples[0].pause_used is False
        assert samples[-1].pause_used is bool(pauses)
        if pauses:
            api._handle_pause_capture({"capture_id": "pause-test"})
        api._handle_stop_capture({"capture_id": "pause-test"})
    finally:
        ctx.stop.set()
        release.put(True)
        thread.join(3)
    assert not thread.is_alive()
    assert events[-1]["event"] == "capture_ended"
    assert events[-1]["pause_used"] is bool(pauses)


@pytest.mark.parametrize("paused", [False, True])
def test_pause_policy_survives_save_reopen_cuts_rename_export_and_upload(tmp_path, monkeypatch, paused):
    monkeypatch.setattr(api, "_managed_raw_dir", lambda: str(tmp_path / "Raw"))
    monkeypatch.setattr(api, "_fetch_app_icon_data_uri", lambda *_: None)
    samples = [snapshot(0, 0), snapshot(1, 1), snapshot(62, 2)]
    samples[-1].pause_used = paused
    saved = api._handle_auto_save_raw_csv({"snapshots": [asdict(s) for s in samples], "session_id": "same", "cuts": [cut()]})
    path = saved["save_path"]
    metadata, header, rows = _read_sections(path)
    assert metadata["capture.pause_used"] == str(paused).lower()
    assert analyze_session(path).sample_count == 3
    assert analyze_session(path).timeseries["t"] == [0, 1, 2]
    save_cuts(path, [{**cut(1, 2), "name": "renamed cut"}])
    replay = api._parse_live_csv(path)
    assert all(s.pause_used is paused for s in replay)
    upload = api._handle_build_endpoint_payload({"snapshots": [asdict(s) for s in replay], "name": "renamed session", "cuts": [cut(1, 2)]})
    assert upload["metadata"]["capture"]["pause_used"] is paused
    assert upload["metadata"]["capture"]["name"] == "renamed session"
    assert upload["metadata"]["capture"]["cuts"] == [cut(1, 2)]
    assert len(upload["timeseries"]["rows"]) == 3
    exported = api._handle_export_live_csv({"save_path": str(tmp_path / "export.csv"), "snapshots": [asdict(s) for s in replay], "session_name": "renamed session"})
    assert _read_sections(exported["save_path"])[0]["capture.pause_used"] == str(paused).lower()
    assert _read_sections(path)[1:] == (header, rows)


def test_legacy_without_pause_evidence_remains_unknown_despite_wall_clock_gap(tmp_path):
    path = tmp_path / "legacy.csv"
    generate_csv_report([snapshot(0, 0), snapshot(62, 2)], str(path))
    path.write_text("\n".join(line for line in path.read_text(encoding="utf-8").splitlines() if not line.startswith("capture.pause_used,")), encoding="utf-8")
    assert all(s.pause_used is False for s in api._parse_live_csv(str(path)))
    assert analyze_session(str(path)).sample_count == 2


def test_pause_discards_inflight_samples_and_stop_while_paused(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    events = []

    class Session:
        def sample(self, duration):
            entered.set()
            assert release.wait(3)
            release.clear()
            return snapshot(1, None)
        def close(self): release.set()

    ctx = context(monkeypatch, Session())
    monkeypatch.setattr(api, "_emit", events.append)
    thread = threading.Thread(target=api._capture_pump, args=("pause-test", ctx.session, ctx.stop, 1.0))
    ctx.thread = thread
    thread.start()
    try:
        assert entered.wait(3)
        api._handle_pause_capture({"capture_id": "pause-test"})
        release.set()
        entered.clear()
        assert entered.wait(3)
        assert not any(e["event"] == "snapshot" for e in events)
        api._handle_stop_capture({"capture_id": "pause-test"})
    finally:
        ctx.stop.set()
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    assert events[-1]["event"] == "capture_ended"
    assert "pause-test" not in api._active_captures


def test_cuts_and_active_axis_round_trip_without_losing_samples(tmp_path, monkeypatch):
    path = tmp_path / "session.csv"
    samples = [snapshot(0, 0), snapshot(1, 1), snapshot(61, 2), snapshot(62, 3)]
    generate_csv_report(samples, str(path), session_id="same-session", cuts=[cut()])
    original = _read_sections(str(path))
    assert read_list_item(str(path)).cuts == [cut()]
    assert analyze_session(str(path)).duration_sec == 3
    assert analyze_session(str(path)).timeseries["t"] == [0, 1, 2, 3]
    assert [s.elapsed_sec for s in api._parse_live_csv(str(path))] == [0, 1, 2, 3]
    monkeypatch.setattr(api, "_managed_raw_dir", lambda: str(tmp_path))
    events = []
    monkeypatch.setattr(api, "_emit", events.append)
    updated = cut(1, 3)
    api._handle_save_session_cuts({"path": str(path), "cuts": [updated], "expected_cuts": [cut()]})
    metadata, header, rows = _read_sections(str(path))
    assert (header, rows) == original[1:]
    assert metadata["session.id"] == "same-session"
    cropped = analyze_session(str(path), start_sec=1, end_sec=2)
    assert cropped.sample_count == 2
    assert cropped.duration_sec == 1
    assert cropped.cuts == [updated]
    assert events == [{"event": "session_library_changed"}]
    payload = api._handle_build_endpoint_payload({"snapshots": [asdict(s) for s in samples], "cuts": [updated]})
    assert payload["metadata"]["capture"]["cuts"] == [updated]
    assert payload["metadata"]["capture"]["duration_total_sec"] == 3
    assert payload["timeseries"]["columns"][-1] == "elapsed_sec"


@pytest.mark.parametrize("value", [[cut(-1, 2)], [cut(1, 1)], [cut(0, 4)], [cut(float('nan'), 2)], [cut(), cut()], [{**cut(), "name": " "}]])
def test_invalid_cuts_rejected(value):
    with pytest.raises(ValueError): validate_cuts(value, 3)


def test_failed_or_conflicting_edit_preserves_original(tmp_path, monkeypatch):
    path = tmp_path / "session.csv"
    generate_csv_report([snapshot(0, 0), snapshot(1, 1), snapshot(2, 2)], str(path), cuts=[cut()])
    original = path.read_bytes()
    with pytest.raises(ValueError, match="changed"):
        save_cuts(str(path), [], expected=[])
    assert path.read_bytes() == original
    monkeypatch.setattr("mpi.session_cuts.os.replace", lambda *args: (_ for _ in ()).throw(OSError("disk unavailable")))
    with pytest.raises(OSError): save_cuts(str(path), [])
    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".mpi-cuts-*"))


def test_fractional_cut_survives_csv_precision_and_xlsx(tmp_path):
    from openpyxl import load_workbook
    path = tmp_path / "fractional.csv"
    samples = [snapshot(0, 0), snapshot(1, 1.123456), snapshot(2, 2.123456)]
    cuts = [cut(1.123456, 2.123456)]
    generate_csv_report(samples, str(path), cuts=cuts)
    assert read_list_item(str(path)).cuts == cuts
    assert analyze_session(str(path), start_sec=1.123456, end_sec=2.123456).sample_count == 2
    xlsx = tmp_path / "cuts.xlsx"
    api._handle_export_live_xlsx({"save_path": str(xlsx), "snapshots": [asdict(s) for s in samples], "cuts": cuts})
    workbook = load_workbook(xlsx)
    assert list(workbook["Session Cuts"].values)[1] == ("구간 A", 1.123456, 2.123456, 1)
    workbook.close()


def test_empty_interval_and_outside_session_cannot_be_saved(tmp_path, monkeypatch):
    path = tmp_path / "session.csv"
    generate_csv_report([snapshot(0, 0), snapshot(2, 2)], str(path))
    with pytest.raises(ValueError, match="no samples"):
        save_cuts(str(path), [cut(0.5, 1.5)])
    monkeypatch.setattr(api, "_managed_raw_dir", lambda: str(tmp_path / "other"))
    with pytest.raises(ValueError, match="outside"):
        api._handle_save_session_cuts({"path": str(path), "cuts": []})
