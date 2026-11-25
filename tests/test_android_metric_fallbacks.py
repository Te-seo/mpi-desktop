"""Android restricted-node fallback and canonical temperature regressions."""
import pytest
from mpi import api
from mpi.android import backend, parsers
from tests.test_android_cpu_measurements import capture, proc_stat


@pytest.mark.parametrize("text, expected", [
    ("  12.5% TOTAL: 7% user + 5.5% kernel", 12.5),
    ("  0% TOTAL: 0% user + 0% kernel", 0),
    ("  100% TOTAL: 50% user + 50% kernel", 100),
    ("  21% 123/game: 20% user + 1% kernel", None),
    ("101% TOTAL: invalid", None),
    ("-1% TOTAL: invalid", None),
    ("nan% TOTAL: invalid", None),
    ("12% TOTAL: one\n20% TOTAL: two", None),
    ("permission denied", None),
])
def test_cpuinfo_total_is_device_wide_and_bounded(text, expected):
    assert parsers.parse_cpuinfo_total(text) == expected


def test_restricted_proc_cpu_uses_service_total_on_desktop_and_mobile(capture):
    readings, sample = capture
    original = readings._safe_shell
    readings.system = "MemTotal: 1000 kB\nMemAvailable: 250 kB\n"
    readings._safe_shell = lambda serial, command: (
        "  37.5% TOTAL: 20% user + 17.5% kernel" if command == "dumpsys cpuinfo"
        else original(serial, command)
    )
    snapshot = sample()
    assert snapshot.cpu.total_percent == 37.5
    assert snapshot.cpu.total_source == "dumpsys_cpuinfo"
    assert snapshot.cpu.per_core_percent == [None, None]
    assert snapshot.memory.sys_used_kb == 750
    assert api._overlay_values(snapshot)["cpu"] == 37.5


def test_readable_proc_cpu_does_not_use_service_window(capture):
    readings, sample = capture
    original = readings._safe_shell
    calls = []
    def tracked(serial, command):
        calls.append(command)
        return original(serial, command)
    readings._safe_shell = tracked
    readings.system = "cpu 250 0 0 250 0"
    assert sample().cpu.total_percent == 50
    assert "dumpsys cpuinfo" not in calls
    # A denied /proc/net/dev must not make adb discard valid CPU/RAM stdout.
    assert "cat /proc/stat /proc/meminfo /proc/net/dev 2>/dev/null; true" in calls


@pytest.mark.parametrize("raw, busy, clock", [
    ("busy_pair:25 100\nhz:600000000", 25, 600),
    ("busy_pair:0 100\nhz:300000000", 0, 300),
    ("percent:45.5 %\nhz:450000000", 45.5, 450),
    ("percent:18\nkhz:255000", 18, 255),
    ("percent:0\nkhz:0", 0, None),
    ("khz:980000\nhz:980000", None, 980),
    ("khz:10000001", None, None),
    ("khz:permission denied 13", None, None),
    ("busy_pair:10 0\npercent:101\nhz:0", None, None),
    ("busy_pair:120 100\npercent:-3\nhz:nan", None, None),
    ("percent:permission denied 13\nhz:600 MHz", None, None),
    ("percent:40 10\nhz:-200", None, None),
])
def test_gpu_readings_preserve_units_and_missing_values(raw, busy, clock):
    assert parsers.parse_gpu_fallback(raw) == {"busy_percent": busy, "clock_mhz": clock}


def test_gpu_fallback_reaches_snapshot_without_fabricating_unsupported_values(capture):
    readings, sample = capture
    original = readings._safe_shell
    readings._safe_shell = lambda serial, command: (
        "busy_pair:35 100\nhz:600000000" if command == backend._GPU_FALLBACK_COMMAND
        else original(serial, command)
    )
    snapshot = sample()
    assert snapshot.gpu.busy_percent == 35
    assert snapshot.gpu.clock_mhz == 600
    assert api._overlay_values(snapshot)["gpu"] == 35


def test_samsung_sgpu_khz_reaches_snapshot_and_mobile_projection(capture):
    readings, sample = capture
    original = readings._safe_shell
    readings._safe_shell = lambda serial, command: (
        "percent:23\nkhz:550000" if command == backend._GPU_FALLBACK_COMMAND
        else original(serial, command)
    )
    snapshot = sample()
    assert snapshot.gpu.busy_percent == 23
    assert snapshot.gpu.clock_mhz == 550
    assert api._overlay_values(snapshot)["gpu"] == 23


def test_sysfs_gpu_rejects_error_digits_and_nonfinite_clock():
    parsed = parsers.parse_sysfs_bundle("error 13 permission denied\nnan", [], core_count=0)
    assert parsed["gpu_busy_percent"] is None
    assert parsed["gpu_clock_mhz"] is None


def test_battery_fallback_uses_canonical_zone_and_mobile_temperature(capture):
    readings, sample = capture
    original = readings._safe_shell
    readings._safe_shell = lambda serial, command: (
        "temperature: 347" if command == "dumpsys battery" else original(serial, command)
    )
    snapshot = sample()
    assert snapshot.thermal.zones_c == {"battery": 34.7}
    assert api._overlay_values(snapshot)["temp"] == 34.7
