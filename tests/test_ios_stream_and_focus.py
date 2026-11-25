"""iOS streamer FPS/temperature helpers + focused-app end conditions.

Covers the three field-reported regressions:
  * intermittent 0 fps over the Wi-Fi tunnel — the kdebug staleness rule must
    HOLD through bursty chunk delivery and then fall back to the graphics FPS,
    never force 0 (test_current_fps_*);
  * missing battery temperature — the extractor must find Temperature keys at
    any depth (excluding the charger's AdapterDetails) and decode centi-/deci-/
    plain-Celsius scales (test_battery_temp_*);
  * capture continuing after the app leaves the foreground — iOS suspended
    (zero-CPU) windows and Android resumed-activity misses must end the capture
    with the app-backgrounded sentinel (test_*_backgrounded_*).
"""

from __future__ import annotations

import sys
import types
from collections import namedtuple

import pytest

from mpi.android import backend as ab
from mpi.backend import AppBackgroundedError, AppExitedError
from mpi.ios import backend as ib
from mpi.ios import pmd3_stream as ps
from mpi.schema import DeviceInfo, PackageInfo, Platform

# --- kdebug FPS stamping ------------------------------------------------------


def test_current_fps_prefers_kdebug_when_present():
    state = {"fps": 58.0, "fps_kdebug": 119.5, "fps_kdebug_at": 100.0}
    assert ps._current_fps(state, 101.0) == 119.5


def test_current_fps_reports_true_zero_windows():
    # A frozen display yields genuine 0-fps windows — they must NOT be
    # overridden by the (stale) graphics value.
    state = {"fps": 58.0, "fps_kdebug": 0.0, "fps_kdebug_at": 100.0}
    assert ps._current_fps(state, 101.0) == 0.0


def test_current_fps_without_kdebug_uses_graphics():
    assert ps._current_fps({"fps": 59.0, "fps_at": 4.0, "fps_kdebug": None}, 5.0) == 59.0


def test_fps_expires_and_falls_back_only_to_a_fresh_source():
    state = {"fps": 59.0, "fps_at": 100.0, "fps_kdebug": 120.0, "fps_kdebug_at": 90.0}
    assert ps._current_fps(state, 101.0) == 59.0
    assert ps._current_fps(state, 104.0) is None
    assert ps._current_fps({"fps": 60.0}, 100.0) is None


def test_kdebug_tap_is_opt_in(monkeypatch):
    # Default OFF: on the tested hardware the tap counts display refreshes,
    # not game frames, so it must never run unless explicitly requested.
    monkeypatch.delenv("MPI_IOS_KDEBUG_FPS", raising=False)
    assert ps._kdebug_fps_enabled() is False
    monkeypatch.setenv("MPI_IOS_KDEBUG_FPS", "0")
    assert ps._kdebug_fps_enabled() is False
    monkeypatch.setenv("MPI_IOS_KDEBUG_FPS", "1")
    assert ps._kdebug_fps_enabled() is True


# --- kdebug windowing ----------------------------------------------------------

_Kev = namedtuple("Kev", ["debugid", "eventid", "func_qualifier", "timestamp"])
_NS_PER_TICK = 125.0 / 3.0  # A-series mach timebase
_QUAL_NONE, _QUAL_START, _QUAL_END = 0, 1, 2


def _frame_ev(ts, qual=_QUAL_NONE):
    return _Kev(ps._KDEBUG_FPS_EVENT | qual, ps._KDEBUG_FPS_EVENT, qual, ts)


def _beat_ev(ts):
    # Companion-subclass heartbeat (DBG_PERF) — advances the window clock only.
    return _Kev(0x25990010, 0x25990010, _QUAL_NONE, ts)


class _FakeParser:
    filter_class: list = []

    def __init__(self, events):
        self._events = events

    def kevents(self, stream):
        return iter(self._events)


def _ticks(ns):
    return int(ns / _NS_PER_TICK)


def _events_from_deltas(deltas_ns, start=10_000_000):
    events = [_frame_ev(start)]
    t = start
    for d in deltas_ns:
        t += _ticks(d)
        events.append(_frame_ev(t))
    return events


def _run_counter(monkeypatch, events):
    # The production tap treats pykdebugparser as optional. These unit tests
    # exercise our windowing logic with a fake parser, so install that fake in
    # sys.modules instead of requiring the optional package in every dev/CI
    # environment merely to monkeypatch it.
    package = types.ModuleType("pykdebugparser")
    package.__path__ = []  # type: ignore[attr-defined]
    parser_module = types.ModuleType("pykdebugparser.pykdebugparser")
    parser_module.PyKdebugParser = lambda: _FakeParser(events)  # type: ignore[attr-defined]
    package.pykdebugparser = parser_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pykdebugparser", package)
    monkeypatch.setitem(sys.modules, "pykdebugparser.pykdebugparser", parser_module)
    state = {"kdebug_windows": ps.deque(), "clock": lambda: 42.0}
    ps._count_display_frames(None, state, _NS_PER_TICK, state["clock"])
    return state


def test_kdebug_120fps_and_60fps_windows(monkeypatch):
    state = _run_counter(monkeypatch, _events_from_deltas([8_333_333] * 300))
    assert state["fps_kdebug"] == pytest.approx(120.0, abs=2.0)
    state = _run_counter(monkeypatch, _events_from_deltas([16_666_667] * 120))
    assert state["fps_kdebug"] == pytest.approx(60.0, abs=2.0)


def test_kdebug_start_end_pairs_not_double_counted(monkeypatch):
    # 60 frames/s emitted as START/END pairs 6 ms apart (past the 4 ms de-dup):
    # counting the masked eventid would read ~120 fps; only STARTs may count.
    events = []
    t = 10_000_000
    for _ in range(180):
        events.append(_frame_ev(t, _QUAL_START))
        events.append(_frame_ev(t + _ticks(6_000_000), _QUAL_END))
        t += _ticks(16_666_667)
    state = _run_counter(monkeypatch, events)
    assert state["fps_kdebug"] == pytest.approx(60.0, abs=2.0)


def test_kdebug_jank_split(monkeypatch):
    deltas = (
        [8_333_333] * 130 + [100_000_000] + [8_333_333] * 130 + [200_000_000] + [8_333_333] * 130
    )
    state = _run_counter(monkeypatch, _events_from_deltas(deltas))
    wins = list(state["kdebug_windows"])
    assert sum(w[1] for w in wins) == 2  # one small + one big
    assert sum(w[2] for w in wins) == 1


def test_kdebug_double_commit_dedup(monkeypatch):
    deltas = []
    for _ in range(120):
        deltas += [2_000_000, 14_666_667]
    state = _run_counter(monkeypatch, _events_from_deltas(deltas))
    assert state["fps_kdebug"] == pytest.approx(60.0, abs=2.0)


def test_kdebug_freeze_produces_zero_windows_and_big_jank(monkeypatch):
    # 60 fps, a 10 s freeze, then 60 fps: the freeze must be VISIBLE — empty
    # (0-frame) windows during the gap plus one big jank for the stall frame.
    deltas = [16_666_667] * 90 + [10_000_000_000] + [16_666_667] * 90
    state = _run_counter(monkeypatch, _events_from_deltas(deltas))
    wins = list(state["kdebug_windows"])
    assert sum(1 for w in wins if w[0] == 0) >= 8  # ~9 empty seconds
    assert sum(w[2] for w in wins) >= 1  # the stall is a big jank
    assert state["fps_kdebug"] == pytest.approx(60.0, abs=2.0)


def test_kdebug_heartbeats_close_zero_windows_without_frames(monkeypatch):
    # Frames stop entirely but companion-subclass events keep flowing: the
    # window clock must keep closing 0-fps windows (live freeze reporting).
    events = _events_from_deltas([16_666_667] * 90)
    t = events[-1].timestamp
    for _ in range(50):  # 5 s of 10 Hz heartbeats, no frames
        t += _ticks(100_000_000)
        events.append(_beat_ev(t))
    state = _run_counter(monkeypatch, events)
    assert state["fps_kdebug"] == 0.0
    assert sum(1 for w in state["kdebug_windows"] if w[0] == 0) >= 4


def test_kdebug_window_percentiles_capture_stutter(monkeypatch):
    # A window mixing 8.3 ms frames with one 100 ms hitch must show the hitch
    # in its tail percentile, not average it away.
    deltas = [8_333_333] * 110 + [100_000_000] + [8_333_333] * 10
    state = _run_counter(monkeypatch, _events_from_deltas(deltas))
    wins = list(state["kdebug_windows"])
    p99s = [w[3][3] for w in wins if w[3][3] is not None]
    assert max(p99s) >= 99.0  # ms


# --- battery temperature extraction ------------------------------------------


def test_battery_temp_top_level_centi_c():
    assert ps._battery_temp_c({"Temperature": 3980}) == 39.8


def test_battery_temp_nested_battery_data_deci_c():
    # Gauge sub-dict with an analytics-style deci-C scale.
    assert ps._battery_temp_c({"BatteryData": {"Temperature": 305}}) == 30.5


def test_battery_temp_plain_celsius():
    assert ps._battery_temp_c({"Temperature": 30.5}) == 30.5


def test_battery_temp_ignores_charger_adapter_details():
    info = {"AdapterDetails": {"Temperature": 4500}}
    assert ps._battery_temp_c(info) is None


def test_battery_temp_rejects_boot_zero_and_garbage():
    assert ps._battery_temp_c({"Temperature": 0}) is None
    assert ps._battery_temp_c({"Temperature": None}) is None
    assert ps._battery_temp_c({}) is None
    assert ps._battery_temp_c(None) is None


def test_battery_temp_prefers_documented_centi_scale():
    # 3050 must decode as 30.5 °C (centi), not 305 °C or 3.05 °C.
    assert ps._battery_temp_c({"Temperature": 3050}) == 30.5


# --- iOS backgrounded / exited detection --------------------------------------


def _ios_session():
    s = object.__new__(ib.IOSPymobiledeviceStreamingCaptureSession)
    s._serial = "UDID"
    s.device = DeviceInfo(serial="UDID", platform=Platform.IOS)
    s.package = PackageInfo(bundle_id="com.example.game")
    s.pid = 42
    s._first_sample = False
    s._legacy = False
    s._closing = False
    s._restarts = 0
    s._metric_cache = {}
    s._raw_last_timestamp = {}
    s._raw_dropped_seen = 0
    s._battery_field_times = {}
    s._battery_metric_seen = {}
    s._last_cpu = 0.0
    s._last_mem_kb = 0
    s._last_fps = None
    s._last_gpu = None
    s._last_battery = None
    s._last_total_raw = None
    s._last_cores = None
    s._missing_streak = 0
    s._suspended_streak = 0
    return s


def test_ios_suspended_app_ends_capture():
    s = _ios_session()
    for _ in range(s._SUSPENDED_WINDOWS - 1):
        s._snapshot_from_frames([{"cpu": 0.0, "mem_bytes": 1024}], 1.0)
    with pytest.raises(AppBackgroundedError, match="app-backgrounded"):
        s._snapshot_from_frames([{"cpu": 0.0, "mem_bytes": 1024}], 1.0)


def test_ios_active_cpu_resets_suspended_streak():
    s = _ios_session()
    for _ in range(s._SUSPENDED_WINDOWS - 1):
        s._snapshot_from_frames([{"cpu": 0.0}], 1.0)
    s._snapshot_from_frames([{"cpu": 12.5}], 1.0)
    assert s._suspended_streak == 0


def test_ios_missing_process_ends_capture():
    s = _ios_session()
    s._snapshot_from_frames([{"missing": True}], 1.0)
    s._snapshot_from_frames([{"missing": True}], 1.0)
    with pytest.raises(AppExitedError, match="app-exited"):
        s._snapshot_from_frames([{"missing": True}], 1.0)


def test_ios_empty_window_counts_nothing():
    s = _ios_session()
    s._snapshot_from_frames([], 1.0)
    assert s._missing_streak == 0 and s._suspended_streak == 0


# --- Android foreground detection ---------------------------------------------


class _FakeBackend:
    def _safe_shell(self, serial, command):
        return ""

    def _detect_max_refresh_hz(self, serial):
        return 60


def _android_session():
    return ab.StreamingCaptureSession(
        backend=_FakeBackend(),
        serial="S",
        device=DeviceInfo(serial="S", platform=Platform.ANDROID),
        package=PackageInfo(bundle_id="com.example.game", uid="10056"),
        pid=111,
        hz=100,
        mem_total_kb=0,
        core_count=1,
        thermal_map=[],
    )


_FOREGROUND = "    mResumedActivity: ActivityRecord{abc u0 com.example.game/.GameActivity t42}"
_OTHER_APP = "    mResumedActivity: ActivityRecord{abc u0 com.android.launcher3/.Home t1}"


def test_android_foreground_match_resets_streak():
    s = _android_session()
    s._background_streak = 3
    s._check_app_foreground(_FOREGROUND)
    assert s._background_streak == 0


def test_android_backgrounded_ends_capture_after_streak():
    s = _android_session()
    for _ in range(s._BACKGROUND_SAMPLES - 1):
        s._check_app_foreground(_OTHER_APP)
    with pytest.raises(AppBackgroundedError, match="app-backgrounded"):
        s._check_app_foreground(_OTHER_APP)


def test_android_prefix_collision_does_not_match():
    s = _android_session()
    line = "    mResumedActivity: ActivityRecord{abc u0 com.example.gamepro/.Main t9}"
    s._check_app_foreground(line)
    assert s._background_streak == 1


def test_android_adb_error_is_not_background_evidence():
    s = _android_session()
    s._background_streak = ab.StreamingCaptureSession._BACKGROUND_SAMPLES - 1
    s._check_app_foreground("(adb error: device offline)")
    assert s._background_streak == ab.StreamingCaptureSession._BACKGROUND_SAMPLES - 1


def test_android_empty_focus_counts_toward_background():
    # Screen off / transition: no resumed activity at all.
    s = _android_session()
    s._check_app_foreground("")
    assert s._background_streak == 1
