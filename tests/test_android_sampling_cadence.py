"""Sampling cadence with independent slow Android services and ADB transport."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from mpi.android import adb, backend
from mpi.schema import DeviceInfo, PackageInfo, Platform
from tests.test_android_cpu_measurements import Readings


def make_session(readings):
    session = backend.StreamingCaptureSession(
        readings, "SERIAL", DeviceInfo(serial="SERIAL", platform=Platform.ANDROID),
        PackageInfo(bundle_id="com.example.game"), pid=123, hz=100,
        core_count=2, mem_total_kb=16_000, max_freq_total_mhz=0, fps_ceiling_hz=120,
    )
    session._read_chart_proc = lambda pid: (None, None, None, None)
    return session


def test_stalled_probe_does_not_delay_ticks_or_replay_its_value():
    readings = Readings()
    shell = readings._safe_shell
    release = threading.Event()
    calls = []

    def delayed(serial, command):
        if command == "dumpsys battery":
            calls.append(command)
            if len(calls) == 1:
                release.wait(4)
                return "level: 9"  # too late for its sample
            return "level: 42"
        return shell(serial, command)

    readings._safe_shell = delayed
    session = make_session(readings)
    try:
        started = time.monotonic()
        first = session.sample(0.5)
        # Include time that the caller spent serializing a row.
        time.sleep(0.25)
        second = session.sample(0.5)
        assert time.monotonic() - started < 1.18
        assert first.battery.level_percent is second.battery.level_percent is None
        assert first.cpu.cpu_percent == second.cpu.cpu_percent == 0.0
        assert len(calls) == 1
        release.set()
        session._pending["battery"].result(timeout=1)
        assert session.sample(0.5).battery.level_percent == 42
    finally:
        release.set()
        session.close()


def test_long_frame_dump_has_the_entire_tick_and_resets_in_one_round_trip():
    readings = Readings()
    shell = readings._safe_shell
    commands = []

    def delayed(serial, command):
        if "gfxinfo" in command:
            commands.append(command)
            if command.endswith("framestats"):
                time.sleep(0.28)
                return "Total frames rendered: 2"
        return shell(serial, command)

    readings._safe_shell = delayed
    session = make_session(readings)
    try:
        assert session.sample(0.5).frames.total_frames == 2
        assert session.sample(0.5).frames.total_frames == 2
        assert commands == [
            "dumpsys gfxinfo com.example.game reset",
            "dumpsys gfxinfo com.example.game reset framestats",
            "dumpsys gfxinfo com.example.game reset framestats",
        ]
    finally:
        session.close()


def test_missed_frame_reset_requires_a_fresh_baseline():
    readings = Readings()
    shell = readings._safe_shell
    release = threading.Event()
    dumps = []

    def delayed(serial, command):
        if command.endswith(" framestats"):
            dumps.append(command)
            if len(dumps) == 1:
                release.wait(3)
            return f"Total frames rendered: {len(dumps)}"
        return shell(serial, command)

    readings._safe_shell = delayed
    session = make_session(readings)
    try:
        missed = session.sample(0.1)
        assert missed.frames.fps_estimate is None
        release.set()
        session._pending["gfxinfo"].result(timeout=1)
        reseed = session.sample(0.1)
        assert reseed.frames.fps_estimate is None
        assert reseed.frames.total_frames == 0
        assert session.sample(0.1).frames.total_frames == 3
    finally:
        release.set()
        session.close()


def test_count_only_fps_uses_symmetric_command_timestamps(monkeypatch):
    readings = Readings()
    session = make_session(readings)
    clock = [0.0]
    monkeypatch.setattr(backend.time, "monotonic", lambda: clock[0])

    def timed(serial, command):
        # The device acts halfway through a 100ms reset/dump round trip.
        clock[0] += 0.1
        return "Total frames rendered: 6" if command.endswith(" framestats") else ""

    readings._safe_shell = timed
    try:
        _, reset = session._frame_counter("gfxinfo", False)
        raw, dump = session._frame_counter("gfxinfo", True)
        assert backend.parsers.parse_gfxinfo(raw).total_frames / (dump - reset) == pytest.approx(60)
    finally:
        session.close()


def test_close_waits_for_active_mutation_and_interrupts_tick_without_a_late_reset():
    readings = Readings()
    shell = readings._safe_shell
    dumping = threading.Event()
    release = threading.Event()
    commands = []

    def delayed(serial, command):
        if "gfxinfo" in command:
            commands.append(command)
            if "framestats" in command:
                dumping.set()
                release.wait(3)
        return shell(serial, command)

    readings._safe_shell = delayed
    session = make_session(readings)
    with ThreadPoolExecutor(max_workers=2) as pool:
        sample = pool.submit(session.sample, 0.5)
        try:
            assert dumping.wait(1)
            closed = pool.submit(session.close)
            with pytest.raises(RuntimeError, match="session closed"):
                sample.result(timeout=0.5)
            assert not closed.done()
            release.set()
            closed.result(timeout=1)
        finally:
            release.set()
            session.close()
        assert len(commands) == 2


def test_transport_loss_does_not_create_a_measurement_row():
    readings = Readings()
    readings._safe_shell = lambda *_args: "(adb error: device offline)"
    session = make_session(readings)
    try:
        with pytest.raises(adb.AdbError, match="no Android metrics"):
            session.sample(0)
    finally:
        session.close()


def test_shell_timeout_is_scoped_to_probe_even_with_nested_commands(monkeypatch):
    calls = []
    monkeypatch.setattr(adb, "run", lambda *args, **kwargs: calls.append(kwargs["timeout"]) or "")
    token = adb.shell_deadline.set(time.monotonic() + 0.5)
    try:
        adb.shell("dumpsys battery")
        adb.shell("cat /proc/stat", timeout=0.02)
    finally:
        adb.shell_deadline.reset(token)
    adb.shell("dumpsys package")
    assert 0 < calls[0] <= 0.5
    assert calls[1:] == [0.02, 30.0]
