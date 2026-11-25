"""Focused Android CPU core-count and unavailable-data regressions."""

from __future__ import annotations

import pytest

from mpi.android import adb, parsers
from mpi.android.backend import (
    AndroidBackend,
    StreamingCaptureSession,
    _frequency_normalized_app_cpu,
    _parse_cpu_list_count,
    _positional_sysfs_read_command,
)
from mpi.schema import DeviceInfo, PackageInfo, Platform


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0", 1),
        ("0-7\n", 8),
        ("0,1,2,3", 4),
        ("0-3,4,5-7", 8),
        ("0-3, 4-7", 8),
    ],
)
def test_parse_cpu_list_count_accepts_contiguous_linux_cpulists(text, expected):
    assert _parse_cpu_list_count(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "7",
        "0-3,5-7",
        "0-3,3-7",
        "0-3,2-7",
        "0-3,",
        "0--3",
        "3-0",
        "cpu0-7",
        "0-4096",
        "9" * 5000,
        "0-" + "9" * 5000,
    ],
)
def test_parse_cpu_list_count_rejects_unavailable_or_ambiguous_layouts(text):
    assert _parse_cpu_list_count(text) == 0


def test_count_cpu_cores_never_guesses_after_adb_failure(monkeypatch):
    def fail(*_args, **_kwargs):
        raise adb.AdbError("device offline")

    monkeypatch.setattr(adb, "shell", fail)

    assert AndroidBackend()._count_cpu_cores("SERIAL") == 0


def test_zero_core_count_disables_cpu_frequency_layout_and_normalization_probe(monkeypatch):
    backend = AndroidBackend()
    monkeypatch.setattr(backend, "_count_cpu_cores", lambda _serial: 0)

    def unexpected_shell(*_args, **_kwargs):
        raise AssertionError("zero cores must not trigger a max-frequency adb read")

    monkeypatch.setattr(backend, "_safe_shell", unexpected_shell)
    assert backend._read_cpu_metadata("SERIAL") == (0, 0.0)

    class EmptyBackend:
        @staticmethod
        def _safe_shell(_serial, _command):
            return ""

    session = StreamingCaptureSession(
        EmptyBackend(),
        "SERIAL",
        DeviceInfo(serial="SERIAL", platform=Platform.ANDROID),
        PackageInfo(bundle_id="com.example.game"),
        pid=0,
        hz=100,
        mem_total_kb=0,
        core_count=0,
        thermal_map=[("battery", 0)],
        max_freq_total_mhz=0.0,
        fps_ceiling_hz=60.0,
    )
    assert "/cpu/cpu" not in session._sysfs_script
    assert session._max_freq_total_mhz == 0.0

    parsed = parsers.parse_sysfs_bundle("50 %\n600\n34500", ["battery"], core_count=0)
    assert parsed["cpu_freqs_mhz"] == []
    assert parsed["thermal_c"] == {"battery": 34.5}


def test_current_frequency_missing_middle_keeps_positions_and_disables_norm():
    output = (
        "50 %\n"
        "600\n"
        "1000000\n"
        "__MPI_MISSING__\n"
        "3000000\n"
        "34500\n"
    )

    parsed = parsers.parse_sysfs_bundle(
        output, ["battery"], core_count=3
    )

    assert parsed["cpu_freqs_mhz"] == [1000.0, 0.0, 3000.0]
    assert parsed["cpu_freqs_complete"] is False
    assert parsed["thermal_c"] == {"battery": 34.5}
    assert (
        _frequency_normalized_app_cpu(
            180.0,
            3,
            parsed["cpu_freqs_mhz"],
            parsed["cpu_freqs_complete"],
            6000.0,
        )
        is None
    )


def test_current_frequency_short_or_zero_layout_disables_norm():
    for output in (
        # A legacy multi-file cat omitted the middle core and shifted the thermal
        # row into the CPU slice. The short overall layout must still fail closed.
        "50 %\n600\n1000000\n3000000\n34500\n",
        # A position exists but is not a positive current-frequency reading.
        "50 %\n600\n1000000\n0\n3000000\n34500\n",
    ):
        parsed = parsers.parse_sysfs_bundle(output, ["battery"], core_count=3)
        assert parsed["cpu_freqs_complete"] is False
        assert (
            _frequency_normalized_app_cpu(
                180.0,
                3,
                parsed["cpu_freqs_mhz"],
                parsed["cpu_freqs_complete"],
                6000.0,
            )
            is None
        )


def test_max_frequency_requires_every_positive_core_value(monkeypatch):
    backend = AndroidBackend()

    def incomplete(_serial, command):
        assert "__MPI_MISSING__" in command
        if "cpuinfo_max_freq" in command:
            return "1000000\n__MPI_MISSING__\n3000000\n"
        assert "scaling_max_freq" in command
        return "1000000\n0\n3000000\n"

    monkeypatch.setattr(backend, "_safe_shell", incomplete)

    assert backend._read_max_freq_total_mhz("SERIAL", 3) == 0.0


def test_max_frequency_rejects_oversized_numeric_device_output(monkeypatch):
    backend = AndroidBackend()
    monkeypatch.setattr(
        backend,
        "_safe_shell",
        lambda _serial, _command: "9" * 5000,
    )

    assert backend._read_max_freq_total_mhz("SERIAL", 1) == 0.0


def test_complete_current_and_max_frequencies_compute_norm(monkeypatch):
    backend = AndroidBackend()
    monkeypatch.setattr(
        backend,
        "_safe_shell",
        lambda _serial, _command: "1000000\n2000000\n3000000\n",
    )

    max_total_mhz = backend._read_max_freq_total_mhz("SERIAL", 3)
    parsed = parsers.parse_sysfs_bundle(
        "50 %\n600\n500000\n1000000\n1500000\n34500\n",
        ["battery"],
        core_count=3,
    )

    assert max_total_mhz == 6000.0
    assert parsed["cpu_freqs_mhz"] == [500.0, 1000.0, 1500.0]
    assert parsed["cpu_freqs_complete"] is True
    assert _frequency_normalized_app_cpu(
        180.0,
        3,
        parsed["cpu_freqs_mhz"],
        parsed["cpu_freqs_complete"],
        max_total_mhz,
    ) == pytest.approx(30.0)


def test_positional_sysfs_reader_has_one_missing_value_sentinel_path():
    command = _positional_sysfs_read_command(["/sys/a", "/sys/b", "/sys/c"])

    assert command.startswith("for f in /sys/a /sys/b /sys/c; do v=;")
    assert "__MPI_MISSING__" in command
    assert 'read -r v < "$f"' in command
