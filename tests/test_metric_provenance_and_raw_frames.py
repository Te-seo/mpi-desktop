import json
import statistics

import pytest

from mpi.ios import backend as ib
from mpi.ios import pmd3_stream as ps
from mpi.schema import DeviceInfo, PackageInfo, Platform, MetricsSnapshot, FrameStats
from mpi.report import generate_csv_report
from mpi.analysis import analyze_session, _frame_summary
from tests.test_ios_stream_and_focus import _ios_session, _run_counter, _events_from_deltas, _frame_ev


def test_source_timestamp_expires_despite_repeated_emission(monkeypatch):
    session = _ios_session()
    now = [100.0]
    monkeypatch.setattr(ib.time, "time", lambda: now[0])
    event = {"t": 100.0, "cpu": 25.0, "mem_bytes": 1024, "cores": 6,
             "fps": 60.0, "fps_at": 100.0, "gpu": 12.0, "gpu_at": 100.0,
             "battery": {"level_percent": 70}, "battery_at": 100.0}
    fresh = session._snapshot_from_frames([event], 1)
    assert fresh.metric_status["fps"]["status"] == "fresh"
    assert fresh.frames.scope == "system_display"
    assert fresh.frames.source == "ios_graphics_core_animation"
    assert fresh.frames.raw_events == []
    now[0] = 102.0
    held = session._snapshot_from_frames([{**event, "t": 102.0}], 1)
    assert held.frames.fps_estimate == 60
    assert held.metric_status["fps"] == {"source": "ios_graphics_core_animation", "scope": "system_display", "status": "held", "age_sec": 2.0}
    now[0] = 104.0
    expired = session._snapshot_from_frames([{**event, "t": 104.0}], 1)
    assert expired.frames.fps_estimate is None
    assert expired.gpu.busy_percent is None
    assert expired.battery.level_percent == 70
    now[0] = 120.0
    offline = session._snapshot_from_frames([], 1)
    assert offline.cpu.cpu_percent is None and offline.cpu.cpu_norm is None
    assert offline.memory.pss_total_kb is None
    assert offline.battery.level_percent is None
    assert offline.metric_status["fps"]["status"] == "unavailable"


def test_real_zero_is_preserved_and_recovers_after_gap(monkeypatch):
    session = _ios_session()
    monkeypatch.setattr(ib.time, "time", lambda: 100.0)
    snap = session._snapshot_from_frames([{"t": 100.0, "fps": 0.0, "fps_at": 100.0, "cpu": 1}], 1)
    assert snap.frames.fps_estimate == 0
    assert snap.metric_status["fps"]["status"] == "fresh"


def test_backend_does_not_repeat_raw_events_when_a_gauge_is_held(monkeypatch):
    session = _ios_session()
    monkeypatch.setattr(ib.time, "time", lambda: 100.0)
    raw = {"timestamp_ns": 1_000_000, "duration_ns": 16_000_000, "source": "ios_kdebug_display_experimental", "scope": "system_display", "stream_id": "s"}
    event = {"t": 100, "raw_frame_events": [raw], "raw_frame_events_dropped": 2}
    first = session._snapshot_from_frames([event], 1)
    held = session._snapshot_from_frames([event], 1)
    assert first.frames.raw_events == [raw]
    assert first.frames.raw_events_dropped == 2
    assert held.frames.raw_events == []
    assert held.frames.raw_events_dropped == 0
    assert "raw_frame_events" not in first.raw["pymobiledevice3_stream_last"]


def test_source_switch_does_not_average_incompatible_fps(monkeypatch):
    session = _ios_session()
    monkeypatch.setattr(ib.time, "time", lambda: 100.0)
    snap = session._snapshot_from_frames([
        {"t": 99, "fps": 120, "fps_at": 99, "fps_source": "ios_kdebug_display_experimental"},
        {"t": 100, "fps": 60, "fps_at": 100, "fps_source": "ios_graphics_core_animation"}], 1)
    assert snap.frames.fps_estimate == 60
    assert snap.frames.experimental is False


def test_kdebug_raw_events_deduplicate_and_keep_trailing_duration(monkeypatch):
    events = _events_from_deltas([16_666_667] * 4 + [2_000_000_000] + [16_666_667])
    events.insert(3, _frame_ev(events[2].timestamp))
    state = _run_counter(monkeypatch, events)
    raw = list(state["kdebug_raw_events"])
    assert len(raw) == 6
    assert max(e["duration_ns"] for e in raw) == pytest.approx(2_000_000_000, abs=100)
    assert raw[-1]["duration_ns"] == pytest.approx(16_666_667, abs=100)
    line = {}
    ps._drain_raw_events(state, line)
    assert len(line["raw_frame_events"]) == 6
    second = {}
    ps._drain_raw_events(state, second)
    assert "raw_frame_events" not in second


def test_bounded_raw_queue_marks_loss(monkeypatch):
    state = _run_counter(monkeypatch, _events_from_deltas([16_666_667] * 4200))
    assert len(state["kdebug_raw_events"]) == 4096
    assert state["kdebug_raw_dropped"] == 104


def test_csv_roundtrip_true_session_percentiles_and_held_dedup(tmp_path):
    from mpi.api import _parse_live_csv
    durations = [10.0] * 100 + [100.0]
    events = [{"timestamp_ns": (i + 1) * 1_000_000_000, "duration_ns": d * 1_000_000,
               "source": "ios_kdebug_display_experimental", "scope": "system_display", "stream_id": "s"}
              for i, d in enumerate(durations)]
    device = DeviceInfo("serial", Platform.IOS)
    package = PackageInfo("app")
    snapshots = [MetricsSnapshot(device, package, "2026-10-16T00:00:00+00:00", 1,
                    frames=FrameStats(fps_estimate=60, source="ios_graphics_core_animation", scope="system_display", raw_events=events[:100], p95_ms=10)),
                 MetricsSnapshot(device, package, "2026-10-16T00:00:01+00:00", 1,
                    frames=FrameStats(raw_events=events[100:], raw_events_dropped=2, p95_ms=100)),
                 MetricsSnapshot(device, package, "2026-10-16T00:00:02+00:00", 1,
                    frames=FrameStats(raw_events=[events[-1]]))]
    snapshots[0].metric_status = {"fps": {"source": "ios_graphics_core_animation", "scope": "system_display", "status": "fresh", "age_sec": 0}}
    snapshots[0].cpu.per_core_percent = [10.0, None, 50.0]
    snapshots[0].cpu.logical_core_count = 3
    path = str(tmp_path / "raw.csv")
    generate_csv_report(snapshots, path)
    loaded = _parse_live_csv(path)
    assert loaded[0].frames.raw_events == events[:100]
    assert loaded[0].metric_status == snapshots[0].metric_status
    assert loaded[0].cpu.per_core_percent == [10.0, None, 50.0]
    assert loaded[0].frames.scope == "system_display"
    analysis = analyze_session(path)
    assert analysis.frames.raw_frame_count == 101
    assert analysis.frames.raw_frames_dropped == 2
    assert analysis.frames.session_p95_ms == statistics.quantiles(durations, n=100, method="inclusive")[94]
    assert analysis.frames.session_p95_ms == 10
    assert analysis.frames.worst_window_p95_ms == 100
    assert analysis.metric_provenance["fps_scopes"] == ["system_display"]
    with open(path, encoding="utf-8") as stream:
        contents = stream.read()
    assert "# Section,Frame event statistics" in contents
    assert "session_p95_ms,10" in contents


def test_no_raw_events_does_not_infer_frames_from_aggregate_fps():
    summary = _frame_summary([{"fps": "60", "frames_total": "0", "raw_frame_events": "[]"}])
    assert summary.raw_frame_count == 0
    assert summary.session_p95_ms is None


def test_invalid_raw_rows_are_ignored():
    summary = _frame_summary([{"raw_frame_events": json.dumps([{"timestamp_ns": 1, "duration_ns": -1}, {"timestamp_ns": 2, "duration_ns": "NaN"}])}])
    assert summary.raw_frame_count == 0


def test_temperature_updates_do_not_refresh_old_battery_fields(monkeypatch):
    now = [100.0]
    state = {"clock": lambda: now[0]}
    ps._update_battery(state, {"level_percent": 70, "temp_c": 30})
    now[0] = 120.0
    ps._update_battery(state, {"temp_c": 31}, merge=True)
    assert state["battery_field_times"]["level_percent"] == 100
    session = _ios_session()
    monkeypatch.setattr(ib.time, "time", lambda: now[0])
    snap = session._snapshot_from_frames([{"t": 120, "battery": state["battery"], "battery_at": state["battery_at"], "battery_field_times": state["battery_field_times"]}], 1)
    assert snap.battery.level_percent is None
    assert snap.thermal.zones_c["battery"] == 31


def test_excel_exports_provenance_and_true_frame_distribution(tmp_path):
    from mpi.api import _live_summary_from_snapshots
    from mpi.reports.excel_export import export_data_to_excel
    from openpyxl import load_workbook
    snap = MetricsSnapshot(DeviceInfo("serial", Platform.IOS), PackageInfo("app"), "2026-10-16T00:00:00+00:00", 1,
        frames=FrameStats(source="ios_graphics_core_animation", scope="system_display"))
    summary = _live_summary_from_snapshots([snap])
    path = str(tmp_path / "report.xlsx")
    export_data_to_excel(path, {"capture": summary}, {})
    book = load_workbook(path)
    evidence = {row[1]: row[2] for row in book["Measurement evidence"].iter_rows(min_row=2, values_only=True)}
    assert evidence["FPS scope"] == "system_display"
    assert evidence["FPS source"] == "ios_graphics_core_animation"
    assert evidence["Session frame P95 (ms)"] is None
    assert evidence["Worst window P95 (ms)"] is None
    assert evidence["Worst window P99 (ms)"] is None
    assert evidence["Raw frames observed"] == 0
    assert summary["average"] is None


def test_raw_burst_larger_than_default_csv_field_limit_roundtrips(tmp_path):
    from mpi.api import _parse_live_csv
    events = [{"timestamp_ns": i + 1, "duration_ns": 16_000_000, "source": "ios_kdebug_display_experimental", "scope": "system_display", "stream_id": "s"} for i in range(4096)]
    snap = MetricsSnapshot(DeviceInfo("serial", Platform.IOS), PackageInfo("app"), "2026-10-16T00:00:00+00:00", 1, frames=FrameStats(raw_events=events))
    path = str(tmp_path / "burst.csv")
    generate_csv_report([snap], path)
    assert len(_parse_live_csv(path)[0].frames.raw_events) == 4096
    assert analyze_session(path).frames.raw_frame_count == 4096


def test_cli_renders_unavailable_cpu_and_memory_without_coercing_zero():
    from mpi.cli import _render_parsed
    from mpi.schema import MemStats
    snap = MetricsSnapshot(DeviceInfo("serial", Platform.IOS), PackageInfo("app"), "2026-10-16T00:00:00+00:00", 1,
                           memory=MemStats(pss_total_kb=None, rss_total_kb=None))
    _render_parsed(snap)


def test_xctrace_preserves_only_observed_timestamp_duration_pairs():
    from mpi.ios.parsers import parse_core_animation
    frames = parse_core_animation('<trace><row><timestamp>0s</timestamp><frameDuration>16ms</frameDuration></row>'
                                  '<row><timestamp>0s</timestamp><frameDuration>16ms</frameDuration></row>'
                                  '<row><fps>60</fps></row></trace>')
    assert len(frames.raw_events) == 1
    assert frames.raw_events[0]["timestamp_ns"] == 0
    assert frames.raw_events[0]["duration_ns"] == 16_000_000
    assert frames.scope == "trace_unverified"
