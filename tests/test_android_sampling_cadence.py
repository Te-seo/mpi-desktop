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
                return "level: 9"
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
        recovered = session.sample(0.5)
        assert recovered.battery.level_percent == 9
        assert recovered.metric_status["battery_level"]["status"] == "held"
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


def test_late_frame_dump_is_consumed_once_with_its_actual_reset_interval():
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
        recovered = session.sample(0.1)
        assert recovered.frames.total_frames == 1
        window = recovered.metric_status["fps"]["window_sec"]
        assert recovered.frames.fps_estimate == pytest.approx(1 / window, rel=0.005)
        assert session.sample(0.1).frames.total_frames == 2
        assert session.sample(0.1).frames.total_frames == 3
    finally:
        release.set()
        session.close()


def test_variable_proc_delays_keep_counter_deltas_and_cannot_cross_pause():
    readings = Readings()
    shell = readings._safe_shell
    release = threading.Event()
    proc_calls = 0

    def delayed(serial, command):
        nonlocal proc_calls
        if "_MPI_PROC_BOUNDARY_" in command:
            proc_calls += 1
            if proc_calls in (1, 3):
                assert release.wait(3)
        if command == "dumpsys battery":
            return "level: 50"
        return shell(serial, command)

    readings._safe_shell = delayed
    session = make_session(readings)
    try:
        for _ in range(3):
            assert session.sample(0.06).cpu.cpu_percent is None
        assert proc_calls == 1
        release.set()
        recovered = session.sample(0.06)
        assert recovered.cpu.cpu_percent == 0
        assert recovered.metric_status["cpu"]["window_sec"] > 0.06
        assert session.sample(0.06).cpu.cpu_percent == 0
        release.clear()
        for _ in range(2):
            assert session.sample(0.06).cpu.cpu_percent is None
        session.reset_observation_window()
        release.set()
        assert session.sample(0.06).cpu.cpu_percent is None
        assert proc_calls == 4
        assert session.sample(0.06).cpu.cpu_percent == 0
    finally:
        release.set()
        session.close()


def test_delayed_memory_from_an_old_pid_cannot_be_attached_to_restarted_process():
    readings = Readings()
    readings.process = ""
    readings._resolve_pid = lambda *_args, **_kwargs: 222
    session = make_session(readings)
    release = threading.Event()

    def memory(pid):
        if pid == 123:
            assert release.wait(3)
        return pid, 0, "foreground", 7

    session._read_chart_proc = memory
    try:
        assert session.sample(0.08).memory.anon_rss_kib is None
        assert session.pid == 222
        release.set()
        sample = session.sample(0.08)
        assert sample.memory.play_memory_pid == sample.memory.anon_rss_kib == 222
    finally:
        release.set()
        session.close()


def test_failed_frame_reset_reseeds_before_using_the_next_count():
    readings = Readings()
    shell = readings._safe_shell
    results = iter(("(adb error: timeout)", "Total frames rendered: 50", "Total frames rendered: 3"))
    readings._safe_shell = lambda serial, command: (
        next(results) if command.endswith(" framestats") else shell(serial, command)
    )
    session = make_session(readings)
    try:
        assert session.sample(0.05).frames.fps_estimate is None
        assert session.sample(0.05).frames.fps_estimate is None
        assert session.sample(0.05).frames.total_frames == 3
    finally:
        session.close()


def test_persistent_fast_bundle_shares_verified_process_read_with_cpu_and_rss():
    readings = Readings()
    session = make_session(readings)
    package = session.package.bundle_id
    status = "RssAnon:\t2048 kB\nVmSwap:\t0 kB\nVmRSS:\t4200 kB"
    memory = "\n_MPI_MEM_BOUNDARY_\n".join((
        readings.process, package, "0", status, "0", package, readings.process, "",
    ))

    class Stream:
        def read(self, command):
            assert command.count("cat /proc/123/status") == 1
            assert "cat /proc/123/stat " not in command
            return "\n_MPI_FAST_SECTION_\n".join((memory, readings.system, "69 %\n422\n10\n10\n"))

        def close(self):
            pass

    session.proc_stream = Stream()
    try:
        sample = session.sample(0.05)
        assert sample.memory.anon_rss_plus_swap_kib == 2048
        assert sample.memory.rss_total_kb == 4200
        assert sample.cpu.cpu_percent == 0
        assert sample.gpu.busy_percent == 69
    finally:
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
