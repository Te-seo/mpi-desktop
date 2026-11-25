from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import subprocess
import sys
import threading
import types
from collections import deque

import pytest

from mpi.ios import backend as ib
from mpi.ios import pmd3
from mpi.ios import pmd3_stream as ps
from mpi.schema import BatteryTemperatureReading, DeviceInfo, PackageInfo, Platform


def test_guard_requires_supported_pymobiledevice3_9x(monkeypatch):
    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "9.13.2")
    assert ps._validate_guard_pmd3_version() == "9.13.2"


@pytest.mark.parametrize("version", ["9.12.9", "10.0.0"])
def test_guard_rejects_unsupported_pymobiledevice3(monkeypatch, version):
    monkeypatch.setattr(importlib.metadata, "version", lambda _name: version)
    with pytest.raises(RuntimeError, match="unsupported_pymobiledevice3"):
        ps._validate_guard_pmd3_version()


class _FakeDiagnostics:
    def __init__(self, iopm=None, pack=None, smart=None, gas=None):
        self.iopm = iopm
        self.pack = pack
        self.smart = smart
        self.gas = gas
        self.calls: list[str] = []

    async def get_battery(self):
        self.calls.append("IOPMPowerSource")
        if isinstance(self.iopm, BaseException):
            raise self.iopm
        return self.iopm

    async def ioregistry(self, *, ioclass):
        assert ioclass in ("AppleSmartBatteryPack", "AppleSmartBattery")
        self.calls.append(ioclass)
        value = self.pack if ioclass == "AppleSmartBatteryPack" else self.smart
        if isinstance(value, BaseException):
            raise value
        return value

    async def info(self, name):
        assert name == "GasGauge"
        self.calls.append("GasGauge")
        if isinstance(self.gas, BaseException):
            raise self.gas
        return self.gas


@pytest.mark.parametrize(
    ("diag", "expected_temperature", "expected_source", "expected_calls"),
    [
        (
            _FakeDiagnostics(iopm={"Temperature": 3980}),
            39.8,
            "IOPMPowerSource",
            ["IOPMPowerSource"],
        ),
        (
            _FakeDiagnostics(
                iopm={"CurrentCapacity": 80},
                pack={"BatteryData": {"Temperature": 2709}},
            ),
            27.1,
            "AppleSmartBatteryPack",
            ["IOPMPowerSource", "AppleSmartBatteryPack"],
        ),
        (
            _FakeDiagnostics(
                iopm={"CurrentCapacity": 80},
                pack={},
                smart={"Temperature": 305},
            ),
            30.5,
            "AppleSmartBattery",
            ["IOPMPowerSource", "AppleSmartBatteryPack", "AppleSmartBattery"],
        ),
        (
            _FakeDiagnostics(
                iopm={"CurrentCapacity": 80},
                pack={},
                smart={},
                gas={"BatteryData": {"VirtualTemperature": 31.5}},
            ),
            31.5,
            "GasGauge",
            [
                "IOPMPowerSource",
                "AppleSmartBatteryPack",
                "AppleSmartBattery",
                "GasGauge",
            ],
        ),
    ],
)
def test_temperature_source_ladder_stops_at_first_valid(
    diag, expected_temperature, expected_source, expected_calls
):
    result = asyncio.run(ps._query_battery_temperature(diag))

    assert result is not None
    temperature_c, source, fields = result
    assert temperature_c == pytest.approx(expected_temperature)
    assert fields["temp_c"] == temperature_c
    assert source == expected_source
    assert diag.calls == expected_calls


def test_temperature_probe_status_distinguishes_disconnect_from_empty_response():
    failed_status: dict = {}
    failed = _FakeDiagnostics(
        iopm=ConnectionError("phone gone"),
        pack=ConnectionError("phone gone"),
        smart=ConnectionError("phone gone"),
        gas=ConnectionError("phone gone"),
    )

    assert (
        asyncio.run(
            ps._query_battery_temperature(failed, probe_status=failed_status)
        )
        is None
    )
    assert failed_status["attempted"] == 4
    assert failed_status["responses"] == 0
    assert len(failed_status["failures"]) == 4

    empty_status: dict = {}
    empty = _FakeDiagnostics(iopm={}, smart={}, gas={})
    assert (
        asyncio.run(ps._query_battery_temperature(empty, probe_status=empty_status))
        is None
    )
    assert empty_status == {"attempted": 4, "responses": 4, "failures": []}


def test_temperature_probe_status_counts_valid_low_temperature_response():
    status: dict = {}
    result = asyncio.run(
        ps._query_battery_temperature(
            _FakeDiagnostics(iopm={"Temperature": 3000}), probe_status=status
        )
    )

    assert result is not None and result[0] == 30.0
    assert status == {"attempted": 1, "responses": 1, "failures": []}


def test_temperature_probe_total_budget_does_not_starve_last_fallback():
    class SlowUpperSources(_FakeDiagnostics):
        async def get_battery(self):
            self.calls.append("IOPMPowerSource")
            await asyncio.sleep(1)
            return {}

        async def ioregistry(self, *, ioclass):
            self.calls.append(ioclass)
            await asyncio.sleep(1)
            return {}

        async def info(self, name):
            assert name == "GasGauge"
            self.calls.append("GasGauge")
            return {"Temperature": 4000}

    diagnostics = SlowUpperSources()

    result = asyncio.run(
        ps._query_battery_temperature(diagnostics, timeout_sec=0.08)
    )

    assert result is not None
    assert result[:2] == (40.0, "GasGauge")
    assert diagnostics.calls == [
        "IOPMPowerSource",
        "AppleSmartBatteryPack",
        "AppleSmartBattery",
        "GasGauge",
    ]


def test_pack_source_uses_centi_c_hint_for_cold_values():
    diagnostics = _FakeDiagnostics(
        iopm={"CurrentCapacity": 80},
        pack={"BatteryData": {"Temperature": 500}},
    )

    result = asyncio.run(ps._query_battery_temperature(diagnostics))

    assert result is not None
    assert result[:2] == (5.0, "AppleSmartBatteryPack")


def test_temperature_ladder_excludes_charger_and_uses_battery_fallback():
    diag = _FakeDiagnostics(
        iopm={"AdapterDetails": {"Temperature": 4500}},
        pack={"ChargerData": {"Temperature": 4300}},
        smart={"ChargerData": {"Temperature": 4400}},
        gas={"BatteryData": {"Temperature": 3200}},
    )

    result = asyncio.run(ps._query_battery_temperature(diag))

    assert result is not None
    assert result[:2] == (32.0, "GasGauge")
    assert diag.calls == [
        "IOPMPowerSource",
        "AppleSmartBatteryPack",
        "AppleSmartBattery",
        "GasGauge",
    ]


@pytest.mark.parametrize("name", ["CPU Die", "GPU", "Skin Sensor", "AC Adapter"])
def test_temperature_extractor_rejects_non_battery_sensor_records(name):
    assert ps._battery_temp_c({"Name": name, "Temperature": 3200}) is None
    assert (
        ps._battery_temp_c(
            {"Name": name, "Temperature": 4500, "Battery": {"Temperature": 3200}}
        )
        is None
    )


def test_temperature_extractor_accepts_explicit_battery_sensor_record():
    assert ps._battery_temp_c({"Sensor": "Battery", "Temperature": 3200}) == 32.0


@pytest.mark.parametrize(
    "value",
    [None, True, 0, -10, float("nan"), float("inf"), -float("inf"), 6100],
)
def test_temperature_extractor_rejects_invalid_values(value):
    assert ps._battery_temp_c({"Temperature": value}) is None


def test_temperature_extractor_scale_ladder():
    assert ps._battery_temp_c({"Temperature": 3050}) == 30.5
    assert ps._battery_temp_c({"Temperature": 305}) == 30.5
    assert ps._battery_temp_c({"Temperature": 30.5}) == 30.5
    assert ps._battery_temp_c({"Temperature": 500}) == 50.0
    assert ps._battery_temp_c({"Temperature": 50}) == 50.0


def test_temperature_extractor_reads_connected_device_pack_shape():
    pack = {
        "BatteryData": {
            "AlgoTemperature": 1776025,
            "LifetimeData": {"AverageTemperature": 27},
            "VirtualTemperature": 2700,
            "Temperature": 2709,
        }
    }

    assert ps._battery_temp_c(pack) == 27.1


def test_temperature_extractor_prefers_nested_physical_over_outer_virtual():
    info = {
        "VirtualTemperature": 2700,
        "BatteryData": {"Temperature": 4000},
    }

    assert ps._battery_temp_c(info) == 40.0


def test_temperature_extractor_ignores_stale_boot_averages_and_legend_names():
    outer_battery = {
        "DeadBatteryBootData": {
            "GeneralPayload": {
                "AverageBattSkinTemp": 25,
                "AverageBattVirtualTemp": 25,
            }
        },
        "IOReportLegend": ["BatteryMaxTemp", "BatteryAvgTemp"],
    }

    assert ps._battery_temp_c(outer_battery) is None


def test_live_battery_loop_uses_pack_temperature(monkeypatch):
    from pymobiledevice3.services import diagnostics as diagnostics_module

    class LiveDiagnostics:
        def __init__(self):
            self.calls: list[str] = []
            self.closed = 0

        async def get_battery(self):
            self.calls.append("IOPMPowerSource")
            return {"CurrentCapacity": 96, "IsCharging": True}

        async def ioregistry(self, *, ioclass):
            self.calls.append(ioclass)
            assert ioclass == "AppleSmartBatteryPack"
            return {"BatteryData": {"Temperature": 2709}}

        async def close(self):
            self.closed += 1

    diagnostics = LiveDiagnostics()

    async def no_syslog(_provider):
        raise RuntimeError("not available in this unit test")

    monkeypatch.setattr(
        diagnostics_module,
        "DiagnosticsService",
        lambda *args, **kwargs: diagnostics,
    )
    monkeypatch.setattr(ps, "_open_temperature_syslog_monitor", no_syslog)

    async def scenario():
        state: dict = {}
        task = asyncio.create_task(ps._battery_loop(object(), state))
        for _ in range(100):
            if state.get("battery"):
                break
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return state

    state = asyncio.run(scenario())

    assert state["battery"] == {
        "level_percent": 96,
        "current_ma": None,
        "voltage_mv": None,
        "status": "Charging",
        "temp_c": 27.1,
    }
    assert diagnostics.calls == ["IOPMPowerSource", "AppleSmartBatteryPack"]
    assert diagnostics.closed == 1


@pytest.mark.parametrize(
    ("filename", "message", "expected"),
    [
        (
            "/usr/libexec/dasd",
            "BatteryTemperatureReader returning value 2919",
            (29.19, "syslog:dasd", 2919.0),
        ),
        (
            "/usr/libexec/symptomsd",
            "Power: battery-temperature 3509 screen-brightness 100",
            (35.09, "syslog:symptomsd", 3509.0),
        ),
    ],
)
def test_unified_log_temperature_parser(filename, message, expected):
    entry = types.SimpleNamespace(filename=filename, message=message)

    assert ps._parse_syslog_battery_temperature(entry) == expected


@pytest.mark.parametrize(
    ("filename", "message"),
    [
        ("/usr/libexec/symptomsd", "Power: battery-temperature 0"),
        ("/usr/libexec/dasd", "BatteryTemperatureReader returning value -10"),
        ("/usr/libexec/dasd", "BatteryTemperatureReader returning value 6100"),
        ("/private/app/game", "BatteryTemperatureReader returning value 2919"),
        (
            "/private/var/containers/Bundle/Application/ABC/dasd",
            "BatteryTemperatureReader returning value 2919",
        ),
        ("/usr/libexec/dasd", "BatteryTemperatureReader cached value 2919"),
    ],
)
def test_unified_log_temperature_parser_rejects_invalid_or_untrusted(
    filename, message
):
    entry = types.SimpleNamespace(filename=filename, message=message)

    assert ps._parse_syslog_battery_temperature(entry) is None


def test_unified_log_monitor_deduplicates_near_cross_daemon_event_and_consumes_once(monkeypatch):
    monkeypatch.setattr(ps.time, "time", lambda: 100.0)
    service = types.SimpleNamespace()
    monitor = ps._BatteryTemperatureSyslogMonitor(object(), service=service)
    dasd = types.SimpleNamespace(
        filename="/usr/libexec/dasd",
        message="BatteryTemperatureReader returning value 2919",
    )
    symptomsd = types.SimpleNamespace(
        filename="/usr/libexec/symptomsd",
        message="Power: battery-temperature 2920",
    )

    assert monitor._accept_entry(dasd, received_at=10.0) is True
    assert monitor._accept_entry(symptomsd, received_at=10.5) is False
    assert monitor.take_nowait() == (29.19, "syslog:dasd", {"temp_c": 29.19, "temp_at": 100.0})
    assert monitor.take_nowait() is None
    assert monitor._accept_entry(symptomsd, received_at=30.0) is True


def test_unified_log_monitor_rejects_entries_before_start_time():
    monitor = ps._BatteryTemperatureSyslogMonitor(
        object(), service=types.SimpleNamespace()
    )
    monitor._started_at_epoch = ps.time.time()
    historical = types.SimpleNamespace(
        filename="/usr/libexec/dasd",
        message="BatteryTemperatureReader returning value 2919",
        timestamp=ps.datetime.fromtimestamp(monitor._started_at_epoch - 60),
    )

    assert monitor._accept_entry(historical) is False
    assert monitor.take_nowait() is None


def test_unified_log_monitor_becomes_stale_before_first_matching_event(monkeypatch):
    monitor = ps._BatteryTemperatureSyslogMonitor(
        object(), service=types.SimpleNamespace()
    )
    monitor._started_at_monotonic = 10.0
    monkeypatch.setattr(ps.time, "monotonic", lambda: 101.0)

    assert monitor.stale is True


def test_diagnostics_temperature_wins_and_discards_syslog_backlog():
    monitor = ps._BatteryTemperatureSyslogMonitor(
        object(), service=types.SimpleNamespace()
    )
    monitor._accept_entry(
        types.SimpleNamespace(
            filename="/usr/libexec/dasd",
            message="BatteryTemperatureReader returning value 2919",
        ),
        received_at=10.0,
    )
    diagnostics = (31.0, "IOPMPowerSource", {"temp_c": 31.0})

    selected, waiting = ps._select_temperature_reading(diagnostics, monitor)

    assert selected is diagnostics
    assert waiting is False
    assert monitor.take_nowait() is None


def test_unified_log_monitor_close_cancels_stream_and_closes_service():
    class FakeOsTrace:
        def __init__(self):
            self.connected = 0
            self.closed = 0
            self.stream_flags = None

        async def connect(self):
            self.connected += 1

        async def syslog(self, *, pid, stream_flags):
            assert pid == -1
            self.stream_flags = stream_flags
            while True:
                await asyncio.sleep(3600)
                if False:  # pragma: no cover - makes this an async generator
                    yield None

        async def close(self):
            self.closed += 1

    async def scenario():
        service = FakeOsTrace()
        monitor = ps._BatteryTemperatureSyslogMonitor(object(), service=service)
        await monitor.start()
        assert monitor.healthy
        await monitor.close()
        return service, monitor

    service, monitor = asyncio.run(scenario())

    assert service.connected == 1
    assert service.closed == 1
    from pymobiledevice3.services.os_trace import OsActivityStreamFlag

    assert service.stream_flags & int(OsActivityStreamFlag.HISTORICAL) == 0
    assert monitor.healthy is False


class _Closeable:
    def __init__(self, name: str):
        self.name = name
        self.closed = 0

    async def close(self):
        self.closed += 1


def test_guard_preparation_tries_usbmux_dvt_then_rsd(monkeypatch):
    usb = _Closeable("usb")
    rsd = _Closeable("rsd")
    dvt = _Closeable("dvt")
    diag = _Closeable("diag")
    calls: list[tuple[str, str]] = []

    async def connect_usb(_udid):
        calls.append(("connect", "usb"))
        return usb

    async def connect_rsd(*_args):
        calls.append(("connect", "rsd"))
        return rsd

    async def open_dvt(provider):
        calls.append(("dvt", provider.name))
        if provider is usb:
            raise RuntimeError("iOS 17 requires RSD")
        return dvt

    async def open_diag(provider):
        calls.append(("diag", provider.name))
        return diag

    async def no_syslog(_provider):
        raise RuntimeError("not available in this unit test")

    monkeypatch.setattr(ps, "_connect_usbmux", connect_usb)
    monkeypatch.setattr(ps, "_connect_rsd", connect_rsd)
    monkeypatch.setattr(ps, "_open_dvt", open_dvt)
    monkeypatch.setattr(ps, "_open_diagnostics", open_diag)
    monkeypatch.setattr(ps, "_open_temperature_syslog_monitor", no_syslog)

    resources = asyncio.run(ps._prepare_guard_services("UDID", None, None))
    assert resources["dvt"] is dvt
    assert resources["diagnostics"] is diag
    assert resources["dvt_transport"] == "rsd"
    assert resources["diagnostics_transport"] == "rsd"
    assert calls == [
        ("connect", "usb"),
        ("dvt", "usb"),
        ("connect", "rsd"),
        ("dvt", "rsd"),
        ("diag", "rsd"),
    ]

    asyncio.run(ps._close_guard_services(resources))
    assert (usb.closed, rsd.closed, dvt.closed, diag.closed) == (1, 1, 1, 1)


def test_guard_preparation_falls_back_to_rsd_for_diagnostics(monkeypatch):
    usb = _Closeable("usb")
    rsd = _Closeable("rsd")
    dvt = _Closeable("dvt")
    diag = _Closeable("diag")
    calls: list[tuple[str, str]] = []

    async def connect_usb(_udid):
        return usb

    async def connect_rsd(*_args):
        calls.append(("connect", "rsd"))
        return rsd

    async def open_dvt(provider):
        return dvt

    async def open_diag(provider):
        calls.append(("diag", provider.name))
        if provider is usb:
            raise RuntimeError("no diagnostics shim")
        return diag

    async def no_syslog(_provider):
        raise RuntimeError("not available in this unit test")

    monkeypatch.setattr(ps, "_connect_usbmux", connect_usb)
    monkeypatch.setattr(ps, "_connect_rsd", connect_rsd)
    monkeypatch.setattr(ps, "_open_dvt", open_dvt)
    monkeypatch.setattr(ps, "_open_diagnostics", open_diag)
    monkeypatch.setattr(ps, "_open_temperature_syslog_monitor", no_syslog)

    resources = asyncio.run(ps._prepare_guard_services("UDID", None, None))
    assert resources["dvt_transport"] == "usbmux"
    assert resources["diagnostics_transport"] == "rsd"
    assert calls == [("diag", "usb"), ("connect", "rsd"), ("diag", "rsd")]
    asyncio.run(ps._close_guard_services(resources))


def test_guard_cleanup_deduplicates_shared_providers():
    shared = _Closeable("shared")
    resources = {
        "process_control": None,
        "diagnostics": shared,
        "dvt": _Closeable("dvt"),
        "rsd": shared,
        "usbmux": shared,
    }

    asyncio.run(ps._close_guard_services(resources))

    assert shared.closed == 1
    assert resources["dvt"].closed == 1


def _guard_args(**overrides):
    values = {
        "udid": "UDID",
        "bundle_id": "com.example.game",
        "threshold_c": 40.0,
        "required_hits": 1,
        "temperature_timeout_sec": 0.01,
        "interval_ms": 50,
        "tunneld": None,
        "rsd": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_first_reading_deadline_is_disabled_after_valid_below_threshold(monkeypatch):
    dvt = _Closeable("dvt")
    diag = _Closeable("diag")
    resources = {
        "usbmux": None,
        "rsd": None,
        "dvt": dvt,
        "diagnostics": diag,
        "dvt_transport": "rsd",
        "diagnostics_transport": "rsd",
    }
    readings = [
        (30.0, "IOPMPowerSource", {"temp_c": 30.0}),
        (40.0, "IOPMPowerSource", {"temp_c": 40.0}),
    ]
    emitted: list[dict] = []
    launched: list[tuple[object, str]] = []

    async def prepare(*_args):
        return resources

    async def query(*_args, **_kwargs):
        return readings.pop(0)

    async def launch(provider, bundle_id):
        launched.append((provider, bundle_id))
        return _Closeable("control"), 321

    async def sysmon(provider, pid, _interval, _state):
        assert provider is dvt
        assert pid == 321

    monkeypatch.setattr(ps, "_validate_guard_pmd3_version", lambda: "9.13.0")
    monkeypatch.setattr(ps, "_prepare_guard_services", prepare)
    monkeypatch.setattr(ps, "_query_battery_temperature", query)
    monkeypatch.setattr(ps, "_launch_guard_target", launch)
    monkeypatch.setattr(ps, "_sysmon_loop", sysmon)
    monkeypatch.setattr(ps, "_fps_enabled", lambda: False)
    monkeypatch.setattr(ps, "_emit", emitted.append)

    assert asyncio.run(ps._run_guard(_guard_args())) == 0
    assert launched == [(dvt, "com.example.game")]
    assert any(event.get("event") == "triggered" for event in emitted)
    assert not any(event.get("code") == "temperature_unavailable" for event in emitted)
    assert diag.closed == 1
    assert dvt.closed == 1


def test_guard_high_missing_high_resets_hits_and_does_not_trigger(monkeypatch):
    dvt = _Closeable("dvt")
    diag = _Closeable("diag")
    resources = {
        "usbmux": None,
        "rsd": None,
        "dvt": dvt,
        "diagnostics": diag,
        "dvt_transport": "rsd",
        "diagnostics_transport": "rsd",
    }
    readings = [
        (40.0, "IOPMPowerSource", {"temp_c": 40.0}),
        None,
        (40.0, "IOPMPowerSource", {"temp_c": 40.0}),
    ]
    emitted: list[dict] = []
    launched: list[bool] = []

    async def prepare(*_args):
        return resources

    async def query(*_args, **_kwargs):
        if readings:
            return readings.pop(0)
        raise asyncio.CancelledError

    async def launch(*_args):
        launched.append(True)
        return _Closeable("control"), 123

    monkeypatch.setattr(ps, "_validate_guard_pmd3_version", lambda: "9.13.0")
    monkeypatch.setattr(ps, "_prepare_guard_services", prepare)
    monkeypatch.setattr(ps, "_query_battery_temperature", query)
    monkeypatch.setattr(ps, "_launch_guard_target", launch)
    monkeypatch.setattr(ps, "_emit", emitted.append)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(ps._run_guard(_guard_args(required_hits=2)))

    samples = [event for event in emitted if event.get("event") == "temperature"]
    assert [event["hits"] for event in samples] == [1, 1]
    assert len(
        [event for event in emitted if event.get("event") == "temperature_reset"]
    ) == 1
    assert launched == []
    assert diag.closed == 1
    assert dvt.closed == 1


def test_guard_syslog_high_idle_high_uses_two_unique_events(monkeypatch):
    class FakeTemperatureSyslog(_Closeable):
        def __init__(self):
            super().__init__("temperature-syslog")
            self.readings = deque(
                [
                    (40.0, "syslog:dasd", {"temp_c": 40.0}),
                    None,
                    (40.1, "syslog:dasd", {"temp_c": 40.1}),
                ]
            )

        @property
        def healthy(self):
            return True

        def take_nowait(self):
            return self.readings.popleft() if self.readings else None

        def discard_pending(self):
            self.readings.clear()

    dvt = _Closeable("dvt")
    diag = _Closeable("diag")
    temperature_syslog = FakeTemperatureSyslog()
    resources = {
        "usbmux": None,
        "rsd": None,
        "dvt": dvt,
        "diagnostics": diag,
        "temperature_syslog": temperature_syslog,
        "dvt_transport": "rsd",
        "diagnostics_transport": "rsd",
        "temperature_syslog_transport": "usbmux",
    }
    emitted: list[dict] = []
    launched: list[bool] = []

    async def prepare(*_args):
        return resources

    async def no_diagnostics_temperature(*_args, probe_status, **_kwargs):
        probe_status.update({"attempted": 4, "responses": 4, "failures": []})
        return None

    async def launch(*_args):
        launched.append(True)
        return _Closeable("control"), 654

    async def sysmon(*_args):
        return None

    monkeypatch.setattr(ps, "_validate_guard_pmd3_version", lambda: "9.13.0")
    monkeypatch.setattr(ps, "_prepare_guard_services", prepare)
    monkeypatch.setattr(
        ps, "_query_battery_temperature", no_diagnostics_temperature
    )
    monkeypatch.setattr(ps, "_launch_guard_target", launch)
    monkeypatch.setattr(ps, "_sysmon_loop", sysmon)
    monkeypatch.setattr(ps, "_fps_enabled", lambda: False)
    monkeypatch.setattr(ps, "_emit", emitted.append)

    result = asyncio.run(
        ps._run_guard(
            _guard_args(
                required_hits=2,
                temperature_timeout_sec=1.0,
                interval_ms=1,
            )
        )
    )

    assert result == 0
    samples = [event for event in emitted if event.get("event") == "temperature"]
    assert [event["hits"] for event in samples] == [1, 2]
    assert not any(event.get("event") == "temperature_reset" for event in emitted)
    assert launched == [True]
    assert temperature_syslog.closed == 1


def test_guard_stale_syslog_expires_diagnostics_hit_and_releases_reservation(
    monkeypatch,
):
    class StaleTemperatureSyslog(_Closeable):
        def __init__(self):
            super().__init__("temperature-syslog")
            self.readings = deque()

        @property
        def healthy(self):
            return True

        @property
        def stale(self):
            return not self.readings

        def take_nowait(self):
            return self.readings.popleft() if self.readings else None

        def discard_pending(self):
            self.readings.clear()

    dvt = _Closeable("dvt")
    diag = _Closeable("diag")
    temperature_syslog = StaleTemperatureSyslog()
    resources = {
        "usbmux": None,
        "rsd": None,
        "dvt": dvt,
        "diagnostics": diag,
        "temperature_syslog": temperature_syslog,
        "dvt_transport": "rsd",
        "diagnostics_transport": "rsd",
        "temperature_syslog_transport": "usbmux",
    }
    emitted: list[dict] = []
    launched: list[bool] = []
    probes = 0

    async def prepare(*_args):
        return resources

    async def no_diagnostics_temperature(*_args, probe_status, **_kwargs):
        nonlocal probes
        probes += 1
        probe_status.update({"attempted": 4, "responses": 4, "failures": []})
        if probes == 1:
            return 40.0, "IOPMPowerSource", {"temp_c": 40.0}
        return None

    async def launch(*_args):
        launched.append(True)
        return _Closeable("control"), 654

    monkeypatch.setattr(ps, "_validate_guard_pmd3_version", lambda: "9.13.0")
    monkeypatch.setattr(ps, "_prepare_guard_services", prepare)
    monkeypatch.setattr(
        ps, "_query_battery_temperature", no_diagnostics_temperature
    )
    monkeypatch.setattr(ps, "_launch_guard_target", launch)
    monkeypatch.setattr(ps, "_emit", emitted.append)

    result = asyncio.run(
        ps._run_guard(
            _guard_args(
                required_hits=2,
                temperature_timeout_sec=1.0,
                interval_ms=1,
            )
        )
    )

    assert result == 3
    assert launched == []
    resets = [event for event in emitted if event.get("event") == "temperature_reset"]
    assert [event["reason"] for event in resets] == [
        "source_stalled",
        "source_stalled",
        "source_stalled",
    ]
    error = next(event for event in emitted if event.get("event") == "error")
    assert error["code"] == "temperature_unavailable"
    assert temperature_syslog.closed == 1
    assert diag.closed == 1
    assert dvt.closed == 1


def test_guard_high_high_triggers_once(monkeypatch):
    dvt = _Closeable("dvt")
    diag = _Closeable("diag")
    resources = {
        "usbmux": None,
        "rsd": None,
        "dvt": dvt,
        "diagnostics": diag,
        "dvt_transport": "rsd",
        "diagnostics_transport": "rsd",
    }
    readings = [
        (40.0, "IOPMPowerSource", {"temp_c": 40.0}),
        (40.1, "IOPMPowerSource", {"temp_c": 40.1}),
    ]
    emitted: list[dict] = []
    launched: list[tuple[object, str]] = []

    async def prepare(*_args):
        return resources

    async def query(*_args, **_kwargs):
        return readings.pop(0)

    async def launch(provider, bundle_id):
        launched.append((provider, bundle_id))
        return _Closeable("control"), 456

    async def sysmon(_provider, _pid, _interval, _state):
        return None

    monkeypatch.setattr(ps, "_validate_guard_pmd3_version", lambda: "9.13.0")
    monkeypatch.setattr(ps, "_prepare_guard_services", prepare)
    monkeypatch.setattr(ps, "_query_battery_temperature", query)
    monkeypatch.setattr(ps, "_launch_guard_target", launch)
    monkeypatch.setattr(ps, "_sysmon_loop", sysmon)
    monkeypatch.setattr(ps, "_fps_enabled", lambda: False)
    monkeypatch.setattr(ps, "_emit", emitted.append)

    assert asyncio.run(ps._run_guard(_guard_args(required_hits=2))) == 0

    samples = [event for event in emitted if event.get("event") == "temperature"]
    assert [event["hits"] for event in samples] == [1, 2]
    assert launched == [(dvt, "com.example.game")]
    assert len([event for event in emitted if event.get("event") == "triggered"]) == 1


def test_temperature_preview_never_launches_on_high_readings(monkeypatch):
    dvt = _Closeable("dvt")
    diag = _Closeable("diag")
    resources = {
        "usbmux": None,
        "rsd": None,
        "dvt": dvt,
        "diagnostics": diag,
        "dvt_transport": "rsd",
        "diagnostics_transport": "rsd",
    }
    readings = [
        (40.0, "IOPMPowerSource", {"temp_c": 40.0}),
        (41.0, "IOPMPowerSource", {"temp_c": 41.0}),
    ]
    emitted: list[dict] = []

    async def prepare(*_args):
        return resources

    async def query(*_args, **_kwargs):
        if readings:
            return readings.pop(0)
        raise RuntimeError("preview test disconnect")

    async def launch(*_args):
        raise AssertionError("temperature preview must never launch an app")

    monkeypatch.setattr(ps, "_validate_guard_pmd3_version", lambda: "9.13.0")
    monkeypatch.setattr(ps, "_prepare_guard_services", prepare)
    monkeypatch.setattr(ps, "_query_battery_temperature", query)
    monkeypatch.setattr(ps, "_launch_guard_target", launch)
    monkeypatch.setattr(ps, "_emit", emitted.append)

    result = asyncio.run(
        ps._run_guard(
            _guard_args(
                threshold_c=5.0,
                required_hits=1,
                temperature_preview=True,
            )
        )
    )

    assert result == 6
    samples = [event for event in emitted if event.get("event") == "temperature"]
    assert [event["hits"] for event in samples] == [0, 0]
    assert not any(event.get("event") in {"triggering", "triggered"} for event in emitted)


def test_guard_valid_then_disconnect_errors_and_releases_services(monkeypatch):
    dvt = _Closeable("dvt")
    diag = _Closeable("diag")
    resources = {
        "usbmux": None,
        "rsd": None,
        "dvt": dvt,
        "diagnostics": diag,
        "dvt_transport": "rsd",
        "diagnostics_transport": "rsd",
    }
    emitted: list[dict] = []
    probes = 0
    launched: list[bool] = []

    async def prepare(*_args):
        return resources

    async def query(*_args, probe_status, **_kwargs):
        nonlocal probes
        probes += 1
        if probes == 1:
            probe_status.update({"attempted": 1, "responses": 1, "failures": []})
            return 30.0, "IOPMPowerSource", {"temp_c": 30.0}
        probe_status.update(
            {
                "attempted": 4,
                "responses": 0,
                "failures": ["GasGauge: ConnectionError: phone gone"],
            }
        )
        return None

    async def launch(*_args):
        launched.append(True)
        return _Closeable("control"), 999

    monkeypatch.setattr(ps, "_validate_guard_pmd3_version", lambda: "9.13.0")
    monkeypatch.setattr(ps, "_prepare_guard_services", prepare)
    monkeypatch.setattr(ps, "_query_battery_temperature", query)
    monkeypatch.setattr(ps, "_launch_guard_target", launch)
    monkeypatch.setattr(ps, "_emit", emitted.append)

    result = asyncio.run(ps._run_guard(_guard_args(required_hits=2)))

    assert result == 6
    assert launched == []
    resets = [event for event in emitted if event.get("event") == "temperature_reset"]
    assert [event["reason"] for event in resets] == [
        "probe_failed",
        "probe_failed",
        "probe_failed",
    ]
    error = next(event for event in emitted if event.get("event") == "error")
    assert error["code"] == "device_disconnected"
    assert "phone gone" in error["error"]
    assert diag.closed == 1
    assert dvt.closed == 1


def test_guard_slow_smartbattery_does_not_starve_valid_gasgauge(monkeypatch):
    class LadderDiagnostics(_Closeable):
        def __init__(self):
            super().__init__("diag")
            self.iopm_calls = 0
            self.pack_calls = 0
            self.smart_calls = 0
            self.gas_calls = 0

        async def get_battery(self):
            self.iopm_calls += 1
            if self.iopm_calls == 1:
                return {"Temperature": 3000}
            return {}

        async def ioregistry(self, *, ioclass):
            assert ioclass in ("AppleSmartBatteryPack", "AppleSmartBattery")
            if ioclass == "AppleSmartBatteryPack":
                self.pack_calls += 1
                return {}
            self.smart_calls += 1
            await asyncio.sleep(1.0)
            return {}

        async def info(self, name):
            assert name == "GasGauge"
            self.gas_calls += 1
            return {"Temperature": 4000}

    dvt = _Closeable("dvt")
    diag = LadderDiagnostics()
    resources = {
        "usbmux": None,
        "rsd": None,
        "dvt": dvt,
        "diagnostics": diag,
        "dvt_transport": "rsd",
        "diagnostics_transport": "rsd",
    }
    emitted: list[dict] = []
    launched: list[bool] = []

    async def prepare(*_args):
        return resources

    async def launch(*_args):
        launched.append(True)
        return _Closeable("control"), 2468

    async def sysmon(*_args):
        return None

    monkeypatch.setattr(ps, "_validate_guard_pmd3_version", lambda: "9.13.0")
    monkeypatch.setattr(ps, "_prepare_guard_services", prepare)
    monkeypatch.setattr(ps, "_launch_guard_target", launch)
    monkeypatch.setattr(ps, "_sysmon_loop", sysmon)
    monkeypatch.setattr(ps, "_fps_enabled", lambda: False)
    monkeypatch.setattr(ps, "_emit", emitted.append)
    monkeypatch.setattr(ps, "_GUARD_POST_VALID_PROBE_TIMEOUT_SEC", 0.06)
    monkeypatch.setattr(ps, "_GUARD_PROBE_COMPLETION_GRACE_SEC", 0.02)

    result = asyncio.run(ps._run_guard(_guard_args(required_hits=2)))

    assert result == 0
    assert diag.pack_calls == 2
    assert diag.smart_calls == 2
    assert diag.gas_calls == 2
    gas_samples = [
        event
        for event in emitted
        if event.get("event") == "temperature" and event.get("source") == "GasGauge"
    ]
    assert [event["hits"] for event in gas_samples] == [1, 2]
    assert launched == [True]
    assert not any(event.get("code") == "device_disconnected" for event in emitted)


def test_guard_temperature_unavailable_cleans_services(monkeypatch):
    dvt = _Closeable("dvt")
    diag = _Closeable("diag")
    resources = {
        "usbmux": None,
        "rsd": None,
        "dvt": dvt,
        "diagnostics": diag,
        "dvt_transport": "rsd",
        "diagnostics_transport": "rsd",
    }
    emitted: list[dict] = []

    async def prepare(*_args):
        return resources

    async def no_temperature(*_args, **_kwargs):
        return None

    monkeypatch.setattr(ps, "_validate_guard_pmd3_version", lambda: "9.13.0")
    monkeypatch.setattr(ps, "_prepare_guard_services", prepare)
    monkeypatch.setattr(ps, "_query_battery_temperature", no_temperature)
    monkeypatch.setattr(ps, "_emit", emitted.append)

    assert asyncio.run(ps._run_guard(_guard_args())) == 3
    assert any(event.get("code") == "temperature_unavailable" for event in emitted)
    assert diag.closed == 1
    assert dvt.closed == 1


def test_process_control_attaches_existing_pid_without_launch(monkeypatch):
    captured = {}
    calls = []

    class FakeProcessControl:
        def __init__(self, dvt):
            captured["dvt"] = dvt
            self.lookups = 0

        async def connect(self):
            calls.append("connect")

        async def process_identifier_for_bundle_identifier(self, bundle_id):
            self.lookups += 1
            calls.append(f"lookup-{self.lookups}")
            captured["lookup_bundle_id"] = bundle_id
            return 777

        async def launch(self, bundle_id, **kwargs):
            raise AssertionError(f"existing app must not launch: {bundle_id} {kwargs}")

    module_name = "pymobiledevice3.services.dvt.instruments.process_control"
    for name in (
        "pymobiledevice3",
        "pymobiledevice3.services",
        "pymobiledevice3.services.dvt",
        "pymobiledevice3.services.dvt.instruments",
    ):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    module = types.ModuleType(module_name)
    module.ProcessControl = FakeProcessControl
    monkeypatch.setitem(sys.modules, module_name, module)

    dvt = object()
    control, pid = asyncio.run(ps._launch_guard_target(dvt, "com.example.game"))

    assert isinstance(control, FakeProcessControl)
    assert pid == 777
    assert calls == ["connect", "lookup-1", "lookup-2"]
    assert captured == {
        "dvt": dvt,
        "lookup_bundle_id": "com.example.game",
    }


def test_process_control_attaches_latest_pid_when_existing_app_restarts(monkeypatch):
    calls = []

    class FakeProcessControl:
        def __init__(self, _dvt):
            self.lookups = 0

        async def connect(self):
            calls.append("connect")

        async def process_identifier_for_bundle_identifier(self, _bundle_id):
            self.lookups += 1
            calls.append(f"lookup-{self.lookups}")
            return 777 if self.lookups == 1 else 778

        async def launch(self, *_args, **_kwargs):
            raise AssertionError("restarted app must attach without launch")

    module_name = "pymobiledevice3.services.dvt.instruments.process_control"
    for name in (
        "pymobiledevice3",
        "pymobiledevice3.services",
        "pymobiledevice3.services.dvt",
        "pymobiledevice3.services.dvt.instruments",
    ):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    module = types.ModuleType(module_name)
    module.ProcessControl = FakeProcessControl
    monkeypatch.setitem(sys.modules, module_name, module)

    _control, pid = asyncio.run(ps._launch_guard_target(object(), "com.example.game"))

    assert pid == 778
    assert calls == ["connect", "lookup-1", "lookup-2"]


def test_process_control_launches_when_existing_pid_disappears_on_recheck(monkeypatch):
    calls = []

    class FakeProcessControl:
        def __init__(self, _dvt):
            self.lookups = 0

        async def connect(self):
            calls.append("connect")

        async def process_identifier_for_bundle_identifier(self, _bundle_id):
            self.lookups += 1
            calls.append(f"lookup-{self.lookups}")
            return 777 if self.lookups == 1 else 0

        async def launch(self, _bundle_id, **kwargs):
            calls.append("launch")
            assert kwargs == {"kill_existing": False}
            return 779

    module_name = "pymobiledevice3.services.dvt.instruments.process_control"
    for name in (
        "pymobiledevice3",
        "pymobiledevice3.services",
        "pymobiledevice3.services.dvt",
        "pymobiledevice3.services.dvt.instruments",
    ):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    module = types.ModuleType(module_name)
    module.ProcessControl = FakeProcessControl
    monkeypatch.setitem(sys.modules, module_name, module)

    _control, pid = asyncio.run(ps._launch_guard_target(object(), "com.example.game"))

    assert pid == 779
    assert calls == ["connect", "lookup-1", "lookup-2", "launch"]


def test_process_control_launches_missing_target_without_killing_existing(monkeypatch):
    captured = {}
    calls = []

    class FakeProcessControl:
        def __init__(self, dvt):
            captured["dvt"] = dvt

        async def connect(self):
            calls.append("connect")

        async def process_identifier_for_bundle_identifier(self, bundle_id):
            calls.append("lookup")
            captured["lookup_bundle_id"] = bundle_id
            return 0

        async def launch(self, bundle_id, **kwargs):
            calls.append("launch")
            captured["launch_bundle_id"] = bundle_id
            captured["launch_kwargs"] = kwargs
            return 778

    module_name = "pymobiledevice3.services.dvt.instruments.process_control"
    for name in (
        "pymobiledevice3",
        "pymobiledevice3.services",
        "pymobiledevice3.services.dvt",
        "pymobiledevice3.services.dvt.instruments",
    ):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    module = types.ModuleType(module_name)
    module.ProcessControl = FakeProcessControl
    monkeypatch.setitem(sys.modules, module_name, module)

    dvt = object()
    _control, pid = asyncio.run(ps._launch_guard_target(dvt, "com.example.game"))

    assert pid == 778
    assert calls == ["connect", "lookup", "launch"]
    assert captured == {
        "dvt": dvt,
        "lookup_bundle_id": "com.example.game",
        "launch_bundle_id": "com.example.game",
        "launch_kwargs": {"kill_existing": False},
    }


def test_process_control_lookup_error_never_launches_and_closes_control(monkeypatch):
    calls = []

    class FakeProcessControl:
        def __init__(self, _dvt):
            pass

        async def connect(self):
            calls.append("connect")

        async def process_identifier_for_bundle_identifier(self, _bundle_id):
            calls.append("lookup")
            raise RuntimeError("DVT lookup failed")

        async def launch(self, *_args, **_kwargs):
            calls.append("launch")
            raise AssertionError("lookup failure must not launch the target")

        async def close(self):
            calls.append("close")

    module_name = "pymobiledevice3.services.dvt.instruments.process_control"
    for name in (
        "pymobiledevice3",
        "pymobiledevice3.services",
        "pymobiledevice3.services.dvt",
        "pymobiledevice3.services.dvt.instruments",
    ):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    module = types.ModuleType(module_name)
    module.ProcessControl = FakeProcessControl
    monkeypatch.setitem(sys.modules, module_name, module)

    with pytest.raises(RuntimeError, match="DVT lookup failed"):
        asyncio.run(ps._launch_guard_target(object(), "com.example.game"))

    assert calls == ["connect", "lookup", "close"]


def test_process_control_second_lookup_error_never_launches_and_closes_control(monkeypatch):
    calls = []

    class FakeProcessControl:
        def __init__(self, _dvt):
            self.lookups = 0

        async def connect(self):
            calls.append("connect")

        async def process_identifier_for_bundle_identifier(self, _bundle_id):
            self.lookups += 1
            calls.append(f"lookup-{self.lookups}")
            if self.lookups == 1:
                return 777
            raise RuntimeError("second DVT lookup failed")

        async def launch(self, *_args, **_kwargs):
            calls.append("launch")
            raise AssertionError("second lookup failure must not launch the target")

        async def close(self):
            calls.append("close")

    module_name = "pymobiledevice3.services.dvt.instruments.process_control"
    for name in (
        "pymobiledevice3",
        "pymobiledevice3.services",
        "pymobiledevice3.services.dvt",
        "pymobiledevice3.services.dvt.instruments",
    ):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    module = types.ModuleType(module_name)
    module.ProcessControl = FakeProcessControl
    monkeypatch.setitem(sys.modules, module_name, module)

    with pytest.raises(RuntimeError, match="second DVT lookup failed"):
        asyncio.run(ps._launch_guard_target(object(), "com.example.game"))

    assert calls == ["connect", "lookup-1", "lookup-2", "close"]


def test_process_control_attaches_when_launch_races_an_external_start(monkeypatch):
    calls = []

    class FakeProcessControl:
        def __init__(self, _dvt):
            self.lookups = 0

        async def connect(self):
            calls.append("connect")

        async def process_identifier_for_bundle_identifier(self, _bundle_id):
            self.lookups += 1
            calls.append(f"lookup-{self.lookups}")
            return 0 if self.lookups == 1 else 779

        async def launch(self, *_args, **kwargs):
            calls.append("launch")
            assert kwargs == {"kill_existing": False}
            raise RuntimeError("already launched by user")

    module_name = "pymobiledevice3.services.dvt.instruments.process_control"
    for name in (
        "pymobiledevice3",
        "pymobiledevice3.services",
        "pymobiledevice3.services.dvt",
        "pymobiledevice3.services.dvt.instruments",
    ):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    module = types.ModuleType(module_name)
    module.ProcessControl = FakeProcessControl
    monkeypatch.setitem(sys.modules, module_name, module)

    _control, pid = asyncio.run(ps._launch_guard_target(object(), "com.example.game"))

    assert pid == 779
    assert calls == ["connect", "lookup-1", "launch", "lookup-2"]


@pytest.mark.parametrize(
    "failure_mode",
    ["connect", "launch", "cancelled", "invalid_pid", "zero_pid"],
)
def test_process_control_launch_failure_closes_control(monkeypatch, failure_mode):
    calls = []

    class FakeProcessControl:
        def __init__(self, _dvt):
            pass

        async def connect(self):
            calls.append("connect")
            if failure_mode == "connect":
                raise RuntimeError("connect failed")

        async def process_identifier_for_bundle_identifier(self, _bundle_id):
            calls.append("lookup")
            return 0

        async def launch(self, _bundle_id, **_kwargs):
            calls.append("launch")
            if failure_mode == "launch":
                raise RuntimeError("launch failed")
            if failure_mode == "cancelled":
                raise asyncio.CancelledError
            if failure_mode == "invalid_pid":
                return "not-a-pid"
            if failure_mode == "zero_pid":
                return 0
            return 777

        async def close(self):
            calls.append("close")

    module_name = "pymobiledevice3.services.dvt.instruments.process_control"
    for name in (
        "pymobiledevice3",
        "pymobiledevice3.services",
        "pymobiledevice3.services.dvt",
        "pymobiledevice3.services.dvt.instruments",
    ):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    module = types.ModuleType(module_name)
    module.ProcessControl = FakeProcessControl
    monkeypatch.setitem(sys.modules, module_name, module)

    expected = asyncio.CancelledError if failure_mode == "cancelled" else RuntimeError
    with pytest.raises(expected):
        asyncio.run(ps._launch_guard_target(object(), "com.example.game"))

    assert calls.count("close") == 1
    assert calls[-1] == "close"
    if failure_mode == "connect":
        assert "launch" not in calls


def test_process_control_launch_supports_sync_legacy_methods(monkeypatch):
    calls = []

    class LegacyProcessControl:
        def __init__(self, _dvt):
            pass

        def connect(self):
            calls.append("connect")

        def process_identifier_for_bundle_identifier(self, _bundle_id):
            calls.append("lookup")
            return 0

        def launch(self, _bundle_id, **_kwargs):
            calls.append("launch")
            return 888

    module_name = "pymobiledevice3.services.dvt.instruments.process_control"
    for name in (
        "pymobiledevice3",
        "pymobiledevice3.services",
        "pymobiledevice3.services.dvt",
        "pymobiledevice3.services.dvt.instruments",
    ):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    module = types.ModuleType(module_name)
    module.ProcessControl = LegacyProcessControl
    monkeypatch.setitem(sys.modules, module_name, module)

    _control, pid = asyncio.run(
        ps._launch_guard_target(object(), "com.example.game")
    )

    assert pid == 888
    assert calls == ["connect", "lookup", "launch"]


class _FakeProcess:
    def __init__(self, stdout=()):
        self.stdout = iter(stdout)
        self.stderr = iter(())
        self.terminated = False
        self.killed = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout):
        return 0

    def kill(self):
        self.killed = True


def test_guard_reader_routes_status_temperature_trigger_and_metrics():
    lines = [
        json.dumps({"event": "guard_ready"}) + "\n",
        json.dumps(
            {
                "event": "temperature",
                "temperature_c": 40.0,
                "sampled_at": "2026-07-20T00:00:00+00:00",
                "source": "IOPMPowerSource",
                "hits": 2,
            }
        )
        + "\n",
        json.dumps({"event": "triggered", "pid": 123}) + "\n",
        json.dumps({"pid": 123, "cpu": 5.0}) + "\n",
    ]
    reader = pmd3.TemperatureGuardStreamReader(
        "UDID", "com.example.game", 40.0
    )
    reader._proc = _FakeProcess(lines)

    reader._read_stdout()

    assert reader.wait_prepared(0)
    assert reader.next_temperature(0)["temperature_c"] == 40.0
    assert reader.wait_triggered(0) == 123
    assert reader.drain() == [{"pid": 123, "cpu": 5.0}]
    reader.close()
    assert reader._proc.terminated is True


def test_guard_reader_monitor_preserves_high_missing_high_reset_order():
    lines = [
        json.dumps({"event": "guard_ready"}) + "\n",
        json.dumps(
            {
                "event": "temperature",
                "temperature_c": 40.0,
                "sampled_at": "2026-07-20T00:00:00+00:00",
                "source": "IOPMPowerSource",
                "hits": 1,
            }
        )
        + "\n",
        json.dumps(
            {
                "event": "temperature_reset",
                "sampled_at": "2026-07-20T00:00:01+00:00",
                "reason": "unavailable",
                "hits": 0,
            }
        )
        + "\n",
        json.dumps(
            {
                "event": "temperature",
                "temperature_c": 40.1,
                "sampled_at": "2026-07-20T00:00:02+00:00",
                "source": "IOPMPowerSource",
                "hits": 1,
            }
        )
        + "\n",
    ]
    reader = pmd3.TemperatureGuardStreamReader(
        "UDID", "com.example.game", 40.0
    )
    reader._proc = _FakeProcess(lines)
    reader._read_stdout()
    device = DeviceInfo(serial="UDID", platform=Platform.IOS)
    package = PackageInfo(bundle_id="com.example.game")
    monitor = ib.IOSBatteryTemperatureMonitor("UDID", device, package, reader)

    first = monitor.sample()
    reset = monitor.sample()
    third = monitor.sample()

    assert first is not None and first.temperature_c == 40.0
    assert reset is None
    assert third is not None and third.temperature_c == 40.1
    monitor.close()


def test_reader_close_kills_process_when_terminate_does_not_finish():
    class StuckProcess(_FakeProcess):
        def wait(self, timeout):
            raise subprocess.TimeoutExpired("guard", timeout)

    reader = pmd3.TemperatureGuardStreamReader(
        "UDID", "com.example.game", 40.0
    )
    reader._proc = StuckProcess()

    reader.close()

    assert reader._proc.terminated is True
    assert reader._proc.killed is True


def test_backend_promotes_exact_guard_reader_without_respawn(monkeypatch):
    class FakeGuardReader:
        first_reading_timeout_sec = 45.0
        error = None

        def __init__(self, *_args, **_kwargs):
            self.closed = 0

        def start(self):
            pass

        def wait_prepared(self, _timeout):
            return True

        def next_temperature(self, _timeout):
            return {
                "temperature_c": 40.0,
                "sampled_at": "2026-07-20T00:00:00+00:00",
                "source": "IOPMPowerSource",
            }

        def wait_triggered(self, _timeout):
            return 4321

        def failed(self):
            return False

        def close(self):
            self.closed += 1

        def wait_ready(self, _timeout):
            return True

        def drain(self):
            return []

    fake_reader = FakeGuardReader()
    monkeypatch.setattr(
        pmd3, "TemperatureGuardStreamReader", lambda *_args, **_kwargs: fake_reader
    )
    backend = ib.IOSBackend()
    device = DeviceInfo(serial="UDID", platform=Platform.IOS)
    package = PackageInfo(bundle_id="com.example.game")
    monkeypatch.setattr(backend, "_resolve_device", lambda _serial: device)
    monkeypatch.setattr(backend, "_enrich_package", lambda _serial, pkg: pkg)

    monitor = backend.create_temperature_monitor(
        "UDID", package, threshold_c=40.0, required_hits=2
    )
    reading = monitor.sample()
    assert reading == BatteryTemperatureReading(
        temperature_c=40.0,
        sampled_at="2026-07-20T00:00:00+00:00",
        source="IOPMPowerSource",
    )

    session = backend.cold_start_and_open_streaming("UDID", package)

    assert session._reader is fake_reader
    assert session.pid == 4321
    assert backend._temperature_monitors == {}
    session.close()
    assert fake_reader.closed == 1


def test_backend_guard_preparation_stop_event_closes_reader(monkeypatch):
    stop_event = threading.Event()

    class WaitingReader:
        error = None

        def __init__(self, *_args, **_kwargs):
            self.closed = 0

        def start(self):
            pass

        def wait_prepared(self, _timeout):
            stop_event.set()
            return False

        def failed(self):
            return False

        def close(self):
            self.closed += 1

    reader = WaitingReader()
    monkeypatch.setattr(
        pmd3, "TemperatureGuardStreamReader", lambda *_args, **_kwargs: reader
    )
    backend = ib.IOSBackend()
    device = DeviceInfo(serial="UDID", platform=Platform.IOS)
    package = PackageInfo(bundle_id="com.example.game")
    monkeypatch.setattr(backend, "_resolve_device", lambda _serial: device)
    monkeypatch.setattr(backend, "_enrich_package", lambda _serial, pkg: pkg)

    with pytest.raises(pmd3.Pymobiledevice3Error, match="cancelled"):
        backend.create_temperature_monitor(
            "UDID",
            package,
            threshold_c=40.0,
            required_hits=2,
            stop_event=stop_event,
        )

    assert reader.closed == 1
    assert backend._temperature_monitors == {}
