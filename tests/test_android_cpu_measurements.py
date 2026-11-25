"""CPU interval validity and observed Android frame event regressions."""
from pathlib import Path

import pytest

from mpi.android import backend, parsers
from mpi.schema import DeviceInfo, PackageInfo, Platform


def proc_stat(jiffies=100, start=10):
    fields = ["S"] + ["0"] * 19
    fields[11], fields[12], fields[19] = str(jiffies), "0", str(start)
    return "123 (game process) " + " ".join(fields)


class Readings:
    process = proc_stat()
    system = "cpu 200 0 0 200 0 0 0 0\ncpu0 100 0 0 100 0 0 0 0\ncpu1 100 0 0 100 0 0 0 0"
    frames = ""

    def _safe_shell(self, _serial, command):
        if "_MPI_PROC_BOUNDARY_" in command:
            return "\n_MPI_PROC_BOUNDARY_\n".join((self.system, "", self.process))
        if command == "cat /proc/123/stat":
            return self.process
        if command.startswith("cat /proc/stat"):
            return self.system
        if "ResumedActivity" in command:
            return "mResumedActivity: com.example.game/.Main"
        if "framestats" in command:
            return self.frames
        return ""

    def _reset_gfxinfo(self, *_args):
        pass

    def _resolve_pid(self, *_args, **_kwargs):
        return 123


@pytest.fixture
def capture(monkeypatch):
    clock = [1_000_000_000]
    monkeypatch.setattr(backend.time, "monotonic_ns", lambda: clock[0])
    readings = Readings()
    session = backend.StreamingCaptureSession(
        readings, "SERIAL", DeviceInfo(serial="SERIAL", platform=Platform.ANDROID),
        PackageInfo(bundle_id="com.example.game"), pid=123, hz=100,
        mem_total_kb=0, core_count=2, max_freq_total_mhz=0, fps_ceiling_hz=60,
    )

    def sample():
        clock[0] += 1_000_000_000
        return session.sample(0)

    yield readings, sample
    session.close()


def test_guest_counters_are_not_counted_twice_and_malformed_values_are_missing():
    assert parsers.parse_proc_stat_cpu("cpu 100 20 30 40 10 0 0 0 70 5") == (200, 50)
    assert parsers.parse_proc_stat_cpu("cpu 100 20 30 40 10 nope") is None
    assert parsers.parse_proc_stat_cpu("cpu 100 20 30 40 -1") is None
    assert parsers.parse_proc_stat_cpu_cores("cpu2 1 0 2 3 0\ncpu0 bad\ncpu999999 1 0 2 3 0") == {2: (1, 0, 2, 3, 0, 0, 0, 0)}


@pytest.mark.parametrize("output", ["", "permission denied", "0", "-1"])
def test_unreadable_clock_rate_is_not_guessed(monkeypatch, output):
    monkeypatch.setattr(backend.adb, "shell", lambda *_args, **_kwargs: output)
    assert backend.AndroidBackend()._read_clock_ticks("SERIAL") == 0


def test_total_is_arithmetic_core_average_not_weighted_aggregate(capture):
    readings, sample = capture
    readings.process = proc_stat(150)
    readings.system = "cpu 380 0 0 320 0\ncpu0 120 0 0 180 0\ncpu1 260 0 0 140 0"
    cpu = sample().cpu
    assert cpu.cpu_percent == 50
    assert cpu.per_core_percent == [20, 80]
    assert cpu.total_percent == 50  # aggregate row would produce 60%
    assert cpu.total_status == "measured"
    assert cpu.total_source == "proc_stat_per_core"


def test_missing_core_retains_index_and_rejoining_core_requires_new_baseline(capture):
    readings, sample = capture
    readings.system = "cpu0 100 0 0 200 0"
    cpu = sample().cpu
    assert cpu.per_core_percent == [0, None]
    assert cpu.total_percent == 0
    assert cpu.total_status == "partial"
    readings.system = "cpu0 100 0 0 300 0\ncpu1 200 0 0 200 0"
    assert sample().cpu.per_core_percent == [0, None]
    readings.system = "cpu0 100 0 0 400 0\ncpu1 250 0 0 250 0"
    assert sample().cpu.per_core_percent == [0, 50]


def test_counter_rollback_and_missing_proc_data_do_not_fabricate_zero(capture):
    readings, sample = capture
    readings.process = "permission denied"
    readings.system = "cpu0 10 0 0 20 0\ncpu1 10 0 0 20 0"
    first = sample().cpu
    assert first.cpu_percent is None
    assert first.total_percent is None
    assert first.per_core_percent == [None, None]
    readings.process = proc_stat(150)
    readings.system = ""
    assert sample().cpu.cpu_percent is None  # first readable point reseeds
    readings.process = proc_stat(150)
    readings.system = "cpu0 10 0 0 120 0"
    recovered = sample().cpu
    assert recovered.cpu_percent == 0  # valid idle interval
    assert recovered.total_percent is None


def test_pid_reuse_reseeds_process_cpu(capture):
    readings, sample = capture
    readings.process = proc_stat(300, start=20)
    assert sample().cpu.cpu_percent is None
    readings.process = proc_stat(350, start=20)
    assert sample().cpu.cpu_percent == 50


def test_aggregate_only_fallback_is_explicit_and_reset_is_missing(capture):
    readings, sample = capture
    readings.system = "cpu 250 0 0 250 0"
    first = sample().cpu
    assert first.total_percent == 50
    assert first.total_source == "proc_stat_aggregate"
    assert first.per_core_percent == [None, None]
    readings.system = "cpu 1 0 0 2 0"
    assert sample().cpu.total_percent is None


def test_aggregate_component_rollback_cannot_hide_behind_increasing_total(capture):
    readings, sample = capture
    readings.system = "cpu 100 0 200 300 0"  # user decreased but sum increased
    assert sample().cpu.total_percent is None


def test_actual_frame_events_keep_source_times_and_deduplicate_ring_buffer(capture):
    readings, sample = capture
    readings.frames = (Path(__file__).parent / "fixtures" / "gfxinfo_sample.txt").read_text()
    first = sample().frames
    assert len(first.raw_events) == 5
    assert first.raw_events[0]["timestamp_ns"] == 100_000_000
    assert first.raw_events[0]["duration_ns"] == 16_666_666
    assert first.raw_events[0]["scope"] == "app_window"
    assert len(sample().frames.raw_events) == 0


def test_raw_event_parser_skips_incomplete_flagged_duplicate_rows_and_reports_cap():
    text = "\n".join([
        "window-1", "---PROFILEDATA---", "Flags,IntendedVsync,FrameCompleted",
        "0,100,120", "0,100,120", "1,200,220", "0,300,9223372036854775807",
        "0,400,420", "0,500,520", "---PROFILEDATA---",
    ])
    events, dropped = parsers._parse_frame_events(text, limit=2)
    assert [event["timestamp_ns"] for event in events] == [100, 400]
    assert dropped == 1
    assert parsers.parse_gfxinfo("Total frames rendered: 20").raw_events == []
