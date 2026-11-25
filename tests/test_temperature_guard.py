from __future__ import annotations

import math
import threading
import time
from datetime import datetime, timezone

import pytest

from mpi import api
from mpi.android import backend as android_backend
from mpi.ios import pmd3
from mpi.schema import (
    BatteryTemperatureReading,
    DeviceInfo,
    MetricsSnapshot,
    PackageInfo,
    Platform,
)


def _reading(value: float, source: str = "test:battery") -> BatteryTemperatureReading:
    return BatteryTemperatureReading(
        temperature_c=value,
        sampled_at=datetime.now(timezone.utc).isoformat(),
        source=source,
    )


class _Monitor:
    def __init__(self, values: list[object]) -> None:
        self.values = list(values)
        self.last = values[-1] if values else None
        self.closed = False
        self.samples = 0

    def sample(self):
        self.samples += 1
        if self.closed:
            return None
        if self.values:
            value = self.values.pop(0)
        else:
            value = self.last
        if isinstance(value, BaseException):
            raise value
        return value

    def close(self) -> None:
        self.closed = True


class _Session:
    def __init__(self, device: DeviceInfo, package: PackageInfo) -> None:
        self.device = device
        self.package = package
        self.closed = False

    def sample(self, duration_sec: float) -> MetricsSnapshot:
        time.sleep(0.001)
        return MetricsSnapshot(
            device=self.device,
            package=self.package,
            captured_at=datetime.now(timezone.utc).isoformat(),
            duration_sec=duration_sec,
        )

    def close(self) -> None:
        self.closed = True


class _GuardBackend:
    def __init__(self) -> None:
        self.devices = {
            "USB": DeviceInfo(
                serial="USB",
                platform=Platform.ANDROID,
                hw_serial="HW-ONE",
            ),
            "10.0.0.2:5555": DeviceInfo(
                serial="10.0.0.2:5555",
                platform=Platform.ANDROID,
                connection_type="wireless",
                hw_serial="HW-ONE",
            ),
            "OTHER": DeviceInfo(
                serial="OTHER",
                platform=Platform.ANDROID,
                hw_serial="HW-TWO",
            ),
        }
        self.monitor_values: list[object] = [_reading(30.0)]
        self.monitors: list[_Monitor] = []
        self.cold_calls = 0
        self.sessions: list[_Session] = []
        self.fail_open = False
        self.prepare_entered: threading.Event | None = None
        self.prepare_release: threading.Event | None = None
        self.open_entered: threading.Event | None = None
        self.open_release: threading.Event | None = None

    def physical_device_id(self, serial: str) -> str:
        return self.devices[serial].hw_serial or serial

    def prepare_capture(self, serial: str, package: PackageInfo):
        return self.devices[serial], PackageInfo(
            bundle_id=package.bundle_id,
            label="Guard Game",
            uid="10001",
        )

    def create_temperature_monitor(self, serial: str, **kwargs):
        assert serial in self.devices
        if self.prepare_entered is not None:
            self.prepare_entered.set()
        if self.prepare_release is not None:
            stop_event = kwargs.get("stop_event")
            while not self.prepare_release.wait(0.001):
                if stop_event is not None and stop_event.is_set():
                    raise RuntimeError("temperature_guard_cancelled")
        monitor = _Monitor(self.monitor_values)
        self.monitors.append(monitor)
        return monitor

    def create_temperature_preview_monitor(self, serial: str, **kwargs):
        return self.create_temperature_monitor(serial, **kwargs)

    def cold_start_and_open_streaming(
        self,
        serial: str,
        package: PackageInfo,
        *,
        stop_event: threading.Event | None = None,
    ):
        del stop_event
        self.cold_calls += 1
        if self.open_entered is not None:
            self.open_entered.set()
        if self.open_release is not None:
            assert self.open_release.wait(2)
        if self.fail_open:
            raise RuntimeError("capture transport failed")
        session = _Session(self.devices[serial], package)
        self.sessions.append(session)
        return session

    def open_streaming(self, serial: str, package: PackageInfo):
        session = _Session(self.devices[serial], package)
        self.sessions.append(session)
        return session


def _wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.002)
    raise AssertionError("timed out waiting for asynchronous guard state")


@pytest.fixture
def guard_env(monkeypatch):
    backend = _GuardBackend()
    emitted: list[dict] = []
    monkeypatch.setattr(api, "_android_backend", backend)
    monkeypatch.setattr(api, "_emit", lambda payload: emitted.append(payload))
    monkeypatch.setattr(api, "TEMPERATURE_GUARD_SAMPLE_INTERVAL_SEC", 0.001)

    with api._captures_lock:
        api._active_temperature_previews.clear()
        api._active_temperature_guards.clear()
        api._active_captures.clear()
        api._reserved_serials.clear()

    yield backend, emitted

    with api._captures_lock:
        previews = list(api._active_temperature_previews.values())
        guards = list(api._active_temperature_guards.values())
        captures = list(api._active_captures.values())
    for context in previews:
        context.stop.set()
        if context.monitor is not None:
            api._close_session(context.monitor)
    for context in guards:
        context.stop.set()
        if context.monitor is not None:
            api._close_session(context.monitor)
    for context in captures:
        context.stop.set()
        api._close_session(context.session)
    time.sleep(0.03)
    with api._captures_lock:
        api._active_temperature_previews.clear()
        api._active_temperature_guards.clear()
        api._active_captures.clear()
        api._reserved_serials.clear()


def _create_guard(serial: str = "USB", **overrides):
    payload = {
        "platform": "android",
        "serial": serial,
        "package": "com.example.game",
        "threshold_c": 40.0,
        "required_hits": 2,
        "cold_start": True,
        "duration_sec": 0.01,
    }
    payload.update(overrides)
    return api._handle_create_temperature_guard(payload)


def test_guard_carries_validated_auto_stop_to_promoted_capture(guard_env):
    created = api._handle_create_temperature_guard({
        "serial": "USB", "package": "com.example.game", "auto_stop_after_sec": 300,
    })
    with api._captures_lock:
        context = api._active_temperature_guards[created["guard_id"]]
    assert created["auto_stop_after_sec"] == 300.0
    assert context.config.duration_sec == 0.5
    assert context.config.auto_stop_after_sec == 300.0

    for invalid in (True, 0, float("nan"), 3601):
        with pytest.raises(ValueError):
            _create_guard(auto_stop_after_sec=invalid)


def _create_preview(serial: str = "USB"):
    return api._handle_create_temperature_preview(
        {
            "platform": "android",
            "serial": serial,
            "package": "com.example.game",
        }
    )


def test_preview_is_two_phase_read_only_and_releases_before_guard(guard_env):
    backend, emitted = guard_env
    backend.monitor_values = [_reading(40.0)]
    created = _create_preview()

    assert created["state"] == "preparing"
    assert backend.monitors == []
    with pytest.raises(ValueError):
        _create_guard()

    result = api._handle_activate_temperature_preview(
        {"preview_id": created["preview_id"]}
    )
    assert result["status"] == "preparing"
    _wait_for(
        lambda: any(e["event"] == "temperature_preview_sample" for e in emitted)
    )
    sample = next(e for e in emitted if e["event"] == "temperature_preview_sample")
    assert sample["preview_id"] == created["preview_id"]
    assert sample["temperature_c"] == 40.0
    assert backend.cold_calls == 0

    stopped = api._handle_stop_temperature_preview(
        {"preview_id": created["preview_id"]}
    )
    assert stopped["status"] == "stopping"
    _wait_for(
        lambda: any(e["event"] == "temperature_preview_ended" for e in emitted)
    )
    assert backend.monitors[0].closed
    assert (
        api._handle_stop_temperature_preview({"preview_id": created["preview_id"]})[
            "status"
        ]
        == "not_running"
    )

    guard = _create_guard()
    api._handle_stop_temperature_guard({"guard_id": guard["guard_id"]})


def test_preview_stop_before_activate_emits_end_and_never_opens_monitor(guard_env):
    backend, emitted = guard_env
    created = _create_preview()

    api._handle_stop_temperature_preview({"preview_id": created["preview_id"]})

    assert backend.monitors == []
    ended = [e for e in emitted if e["event"] == "temperature_preview_ended"]
    assert len(ended) == 1
    assert ended[0]["preview_id"] == created["preview_id"]
    retry = _create_preview()
    api._handle_stop_temperature_preview({"preview_id": retry["preview_id"]})


def test_preview_and_capture_share_physical_device_reservation(guard_env):
    _backend, _emitted = guard_env
    preview = _create_preview("USB")
    with pytest.raises(ValueError):
        api._handle_start_capture(
            {
                "platform": "android",
                "serial": "10.0.0.2:5555",
                "package": "com.example.game",
                "duration_sec": 0.01,
            }
        )
    api._handle_stop_temperature_preview({"preview_id": preview["preview_id"]})

    capture = api._handle_start_capture(
        {
            "platform": "android",
            "serial": "USB",
            "package": "com.example.game",
            "duration_sec": 0.01,
        }
    )
    with pytest.raises(ValueError):
        _create_preview("10.0.0.2:5555")
    api._handle_stop_capture({"capture_id": capture["capture_id"]})


def test_preview_cancel_during_monitor_preparation_has_no_stale_sample(guard_env):
    backend, emitted = guard_env
    backend.prepare_entered = threading.Event()
    backend.prepare_release = threading.Event()
    created = _create_preview()
    api._handle_activate_temperature_preview({"preview_id": created["preview_id"]})
    assert backend.prepare_entered.wait(2)

    api._handle_stop_temperature_preview({"preview_id": created["preview_id"]})
    backend.prepare_release.set()

    _wait_for(
        lambda: any(e["event"] == "temperature_preview_ended" for e in emitted)
    )
    assert not any(e["event"] == "temperature_preview_sample" for e in emitted)
    retry = _create_guard()
    api._handle_stop_temperature_guard({"guard_id": retry["guard_id"]})


def test_preview_stop_keeps_reservation_until_blocking_sample_worker_exits(guard_env):
    backend, emitted = guard_env
    sample_entered = threading.Event()
    close_unblocked_sample = threading.Event()
    allow_sample_return = threading.Event()

    class BlockingSampleMonitor:
        def __init__(self):
            self.close_calls = 0

        def sample(self):
            sample_entered.set()
            assert close_unblocked_sample.wait(2)
            assert allow_sample_return.wait(2)
            return _reading(30.0)

        def close(self):
            self.close_calls += 1
            close_unblocked_sample.set()

    monitor = BlockingSampleMonitor()
    backend.create_temperature_preview_monitor = (
        lambda _serial, **_kwargs: monitor
    )
    created = _create_preview()
    api._handle_activate_temperature_preview({"preview_id": created["preview_id"]})
    assert sample_entered.wait(2)

    api._handle_stop_temperature_preview({"preview_id": created["preview_id"]})

    with api._captures_lock:
        context = api._active_temperature_previews[created["preview_id"]]
        assert context.thread is not None and context.thread.is_alive()
    assert not any(e["event"] == "temperature_preview_ended" for e in emitted)
    with pytest.raises(ValueError):
        _create_guard()

    allow_sample_return.set()
    _wait_for(
        lambda: any(e["event"] == "temperature_preview_ended" for e in emitted)
    )
    context.thread.join(timeout=2)
    assert not context.thread.is_alive()
    assert monitor.close_calls == 1

    retry = _create_guard()
    api._handle_stop_temperature_guard({"guard_id": retry["guard_id"]})


def test_guard_requires_two_consecutive_threshold_hits_and_triggers_once(guard_env):
    backend, emitted = guard_env
    backend.monitor_values = [
        _reading(39.9),
        _reading(40.0),
        _reading(39.9),
        _reading(40.0),
        _reading(40.0),
        _reading(42.0),
    ]
    created = _create_guard()

    first = api._handle_activate_temperature_guard({"guard_id": created["guard_id"]})
    second = api._handle_activate_temperature_guard({"guard_id": created["guard_id"]})
    assert first["status"] == "preparing"
    assert second["status"] in {"preparing", "waiting", "triggering", "running"}

    _wait_for(lambda: any(e["event"] == "temperature_guard_started" for e in emitted))
    samples = [e for e in emitted if e["event"] == "temperature_guard_sample"]
    assert [e["consecutive_hits"] for e in samples[:5]] == [0, 1, 0, 1, 2]
    assert backend.cold_calls == 1
    assert len(backend.monitors) == 1
    assert len([e for e in emitted if e["event"] == "temperature_guard_started"]) == 1
    started = next(e for e in emitted if e["event"] == "temperature_guard_started")
    with api._captures_lock:
        assert started["session_id"] == api._active_captures[started["capture_id"]].mobile_session_id

    api._handle_stop_temperature_guard({"guard_id": created["guard_id"]})
    _wait_for(lambda: any(e["event"] == "temperature_guard_ended" for e in emitted))
    assert backend.sessions[0].closed


def test_guard_missing_poll_resets_hits_without_emitting_ui_sample(guard_env):
    backend, emitted = guard_env
    backend.monitor_values = [
        _reading(40.0),
        None,
        _reading(40.0),
        _reading(40.1),
    ]
    created = _create_guard()
    api._handle_activate_temperature_guard({"guard_id": created["guard_id"]})

    _wait_for(lambda: any(e["event"] == "temperature_guard_started" for e in emitted))

    samples = [e for e in emitted if e["event"] == "temperature_guard_sample"]
    assert [e["temperature_c"] for e in samples[:3]] == [40.0, 40.0, 40.1]
    assert [e["consecutive_hits"] for e in samples[:3]] == [1, 1, 2]
    assert backend.cold_calls == 1

    api._handle_stop_temperature_guard({"guard_id": created["guard_id"]})
    _wait_for(lambda: any(e["event"] == "temperature_guard_ended" for e in emitted))


def test_guard_rejects_nan_infinity_zero_and_out_of_range_samples(guard_env):
    backend, emitted = guard_env
    backend.monitor_values = [
        _reading(math.nan),
        _reading(math.inf),
        _reading(0.0),
        _reading(4.9),
        _reading(60.1),
        _reading(40.0),
        _reading(40.0),
    ]
    created = _create_guard()
    api._handle_activate_temperature_guard({"guard_id": created["guard_id"]})

    _wait_for(lambda: any(e["event"] == "temperature_guard_started" for e in emitted))
    samples = [e for e in emitted if e["event"] == "temperature_guard_sample"]
    assert [e["temperature_c"] for e in samples] == [40.0, 40.0]
    api._handle_stop_temperature_guard({"guard_id": created["guard_id"]})


def test_guard_cancel_while_waiting_never_launches_app(guard_env):
    backend, emitted = guard_env
    backend.monitor_values = [_reading(20.0)]
    created = _create_guard()
    api._handle_activate_temperature_guard({"guard_id": created["guard_id"]})
    _wait_for(lambda: any(e["event"] == "temperature_guard_sample" for e in emitted))

    result = api._handle_stop_temperature_guard({"guard_id": created["guard_id"]})

    assert result["status"] == "stopping"
    _wait_for(lambda: any(e["event"] == "temperature_guard_ended" for e in emitted))
    assert backend.cold_calls == 0
    assert backend.monitors[0].closed


def test_guard_cancel_interrupts_monitor_preparation_and_releases_device(guard_env):
    backend, emitted = guard_env
    backend.prepare_entered = threading.Event()
    backend.prepare_release = threading.Event()
    created = _create_guard()
    api._handle_activate_temperature_guard({"guard_id": created["guard_id"]})
    assert backend.prepare_entered.wait(2)

    api._handle_stop_temperature_guard({"guard_id": created["guard_id"]})

    _wait_for(lambda: any(e["event"] == "temperature_guard_ended" for e in emitted))
    assert not backend.monitors
    retry = _create_guard()
    api._handle_stop_temperature_guard({"guard_id": retry["guard_id"]})


def test_usb_and_wifi_transports_compete_for_one_physical_reservation(guard_env):
    _backend, _emitted = guard_env
    first = _create_guard("USB")

    with pytest.raises(ValueError, match="같은 물리 기기"):
        _create_guard("10.0.0.2:5555")

    api._handle_stop_temperature_guard({"guard_id": first["guard_id"]})
    second = _create_guard("10.0.0.2:5555")
    assert second["device"].hw_serial == "HW-ONE"
    api._handle_stop_temperature_guard({"guard_id": second["guard_id"]})


def test_guard_and_manual_capture_share_the_physical_reservation(guard_env):
    _backend, _emitted = guard_env
    guard = _create_guard("USB")

    with pytest.raises(ValueError, match="같은 물리 기기"):
        api._handle_start_capture(
            {
                "platform": "android",
                "serial": "10.0.0.2:5555",
                "package": "com.example.game",
                "duration_sec": 0.01,
            }
        )

    api._handle_stop_temperature_guard({"guard_id": guard["guard_id"]})
    capture = api._handle_start_capture(
        {
            "platform": "android",
            "serial": "USB",
            "package": "com.example.game",
            "duration_sec": 0.01,
        }
    )
    with pytest.raises(ValueError, match="같은 물리 기기"):
        _create_guard("10.0.0.2:5555")
    api._handle_stop_capture({"capture_id": capture["capture_id"]})


def test_trigger_failure_releases_reservation_for_retry(guard_env):
    backend, emitted = guard_env
    backend.monitor_values = [_reading(40.0), _reading(40.0)]
    backend.fail_open = True
    created = _create_guard()
    api._handle_activate_temperature_guard({"guard_id": created["guard_id"]})

    _wait_for(lambda: any(e["event"] == "temperature_guard_error" for e in emitted))
    _wait_for(lambda: any(e["event"] == "temperature_guard_ended" for e in emitted))
    error = next(e for e in emitted if e["event"] == "temperature_guard_error")
    assert error["code"] == "capture_start_failed"

    backend.fail_open = False
    retry = _create_guard()
    api._handle_stop_temperature_guard({"guard_id": retry["guard_id"]})


def test_guard_disconnect_error_is_stable_and_releases_reservation(guard_env):
    backend, emitted = guard_env
    backend.monitor_values = [
        RuntimeError("device_disconnected: battery diagnostics unavailable")
    ]
    created = _create_guard()
    api._handle_activate_temperature_guard({"guard_id": created["guard_id"]})

    _wait_for(lambda: any(e["event"] == "temperature_guard_error" for e in emitted))
    _wait_for(lambda: any(e["event"] == "temperature_guard_ended" for e in emitted))

    error = next(e for e in emitted if e["event"] == "temperature_guard_error")
    assert error["code"] == "device_disconnected"
    retry = _create_guard()
    api._handle_stop_temperature_guard({"guard_id": retry["guard_id"]})


def test_guard_error_is_emitted_only_after_monitor_cleanup_and_unlock(guard_env):
    backend, emitted = guard_env
    close_entered = threading.Event()
    allow_close = threading.Event()

    class BlockingCloseMonitor(_Monitor):
        def close(self) -> None:
            close_entered.set()
            assert allow_close.wait(1)
            super().close()

    monitor = BlockingCloseMonitor([RuntimeError("device_disconnected")])
    backend.create_temperature_monitor = lambda _serial, **_kwargs: monitor
    created = _create_guard()
    api._handle_activate_temperature_guard({"guard_id": created["guard_id"]})
    assert close_entered.wait(1)

    assert not any(e["event"] == "temperature_guard_error" for e in emitted)
    with pytest.raises(ValueError) as busy:
        _create_guard()
    assert "serial=USB" in str(busy.value)

    allow_close.set()
    _wait_for(lambda: any(e["event"] == "temperature_guard_error" for e in emitted))
    assert monitor.closed
    retry = _create_guard()
    api._handle_stop_temperature_guard({"guard_id": retry["guard_id"]})


def test_activation_thread_failure_releases_reservation(guard_env, monkeypatch):
    del guard_env
    created = _create_guard()
    monkeypatch.setattr(
        api.threading.Thread,
        "start",
        lambda _thread: (_ for _ in ()).throw(RuntimeError("thread unavailable")),
    )

    with pytest.raises(RuntimeError, match="thread unavailable"):
        api._handle_activate_temperature_guard({"guard_id": created["guard_id"]})

    retry = _create_guard()
    api._handle_stop_temperature_guard({"guard_id": retry["guard_id"]})


def test_capture_thread_failure_closes_session_and_releases_guard(
    guard_env, monkeypatch
):
    backend, emitted = guard_env
    backend.monitor_values = [_reading(40.0), _reading(40.0)]
    original_start = api.threading.Thread.start

    def start(thread):
        if thread.name.startswith("capture-"):
            raise RuntimeError("capture thread unavailable")
        return original_start(thread)

    monkeypatch.setattr(api.threading.Thread, "start", start)
    created = _create_guard()
    api._handle_activate_temperature_guard({"guard_id": created["guard_id"]})

    _wait_for(lambda: any(e["event"] == "temperature_guard_error" for e in emitted))
    assert backend.sessions[0].closed
    retry = _create_guard()
    api._handle_stop_temperature_guard({"guard_id": retry["guard_id"]})


def test_cancel_during_capture_creation_closes_late_session(guard_env):
    backend, emitted = guard_env
    backend.monitor_values = [_reading(40.0), _reading(40.0)]
    backend.open_entered = threading.Event()
    backend.open_release = threading.Event()
    created = _create_guard()
    api._handle_activate_temperature_guard({"guard_id": created["guard_id"]})
    assert backend.open_entered.wait(2)

    api._handle_stop_temperature_guard({"guard_id": created["guard_id"]})
    backend.open_release.set()

    _wait_for(lambda: any(e["event"] == "temperature_guard_ended" for e in emitted))
    assert backend.sessions and backend.sessions[0].closed
    assert not any(e["event"] == "temperature_guard_started" for e in emitted)


def test_no_first_valid_temperature_reports_stable_error(guard_env, monkeypatch):
    backend, emitted = guard_env
    backend.monitor_values = [None]
    monkeypatch.setattr(api, "TEMPERATURE_GUARD_FIRST_READING_TIMEOUT_SEC", 0.002)
    created = _create_guard()
    api._handle_activate_temperature_guard({"guard_id": created["guard_id"]})

    _wait_for(lambda: any(e["event"] == "temperature_guard_error" for e in emitted))
    error = next(e for e in emitted if e["event"] == "temperature_guard_error")
    assert error["code"] == "temperature_unavailable"
    assert backend.cold_calls == 0


def test_guard_input_validation(guard_env):
    del guard_env
    for value in (math.nan, math.inf, 0.0, 4.9, 60.1):
        with pytest.raises(ValueError, match="threshold_c"):
            _create_guard(threshold_c=value)
    with pytest.raises(ValueError, match="required_hits"):
        _create_guard(required_hits=0)
    with pytest.raises(ValueError, match="cold_start"):
        _create_guard(cold_start="yes")


def test_guard_api_default_threshold_is_25_c(guard_env):
    del guard_env
    created = api._handle_create_temperature_guard(
        {
            "platform": "android",
            "serial": "USB",
            "package": "com.example.game",
            "required_hits": 2,
            "cold_start": True,
            "duration_sec": 0.01,
        }
    )
    with api._captures_lock:
        context = api._active_temperature_guards[created["guard_id"]]
        assert context.config.threshold_c == 25.0
    api._handle_stop_temperature_guard({"guard_id": created["guard_id"]})


def test_shutdown_joins_guard_preparing_a_local_monitor(guard_env, monkeypatch):
    backend, _emitted = guard_env
    backend.prepare_entered = threading.Event()
    backend.prepare_release = threading.Event()
    monkeypatch.setattr(api, "SHUTDOWN_JOIN_TIMEOUT_SEC", 1.0)
    created = _create_guard()
    api._handle_activate_temperature_guard({"guard_id": created["guard_id"]})
    assert backend.prepare_entered.wait(1)

    with api._captures_lock:
        context = api._active_temperature_guards[created["guard_id"]]
        worker = context.thread

    api._shutdown_captures()

    assert worker is not None and not worker.is_alive()
    with api._captures_lock:
        assert created["guard_id"] not in api._active_temperature_guards
        assert context.device_key not in api._reserved_serials


def test_capture_ended_is_emitted_after_registry_cleanup(guard_env, monkeypatch):
    backend, emitted = guard_env
    session = _Session(backend.devices["USB"], PackageInfo(bundle_id="com.example.game"))
    stop = threading.Event()
    stop.set()
    capture_id = "terminal-order"
    context = api._CaptureContext(
        thread=threading.current_thread(),
        stop=stop,
        session=session,
        duration_sec=0.01,
        serial="USB",
        device_key="android:HW-ONE",
    )
    with api._captures_lock:
        api._active_captures[capture_id] = context

    def emit(payload):
        if payload.get("event") == "capture_ended":
            with api._captures_lock:
                assert capture_id not in api._active_captures
        emitted.append(payload)

    monkeypatch.setattr(api, "_emit", emit)
    api._capture_pump(capture_id, session, stop, 0.01)

    assert session.closed
    assert emitted[-1]["event"] == "capture_ended"


def test_ios_guard_preflight_uses_pmd3_on_macos_without_changing_xcrun(
    monkeypatch,
):
    calls: list[str] = []
    monkeypatch.setattr(api.sys, "platform", "darwin")
    monkeypatch.setattr(
        pmd3,
        "guard_preflight",
        lambda: calls.append("guard_preflight")
        or {"available": True, "version": "9.13"},
    )
    monkeypatch.setattr(
        pmd3,
        "install_cli",
        lambda: calls.append("install") or {"available": True, "version": "9.13"},
    )

    assert api._handle_ios_preflight({})["tool"] == "xcrun"
    guard_check = api._handle_ios_temperature_guard_preflight({})
    guard_install = api._handle_ios_temperature_guard_install_tool({})

    assert guard_check["tool"] == guard_install["tool"] == "pymobiledevice3"
    assert calls == ["guard_preflight", "install", "guard_preflight"]


def test_android_temperature_monitor_prefers_dumpsys_then_exact_sysfs(monkeypatch):
    calls: list[str] = []

    def fake_shell(command: str, serial=None, timeout=30.0):
        del serial, timeout
        calls.append(command)
        if command.startswith("for i in"):
            return "3:battery_charger\n7:battery\n9:BATTERY\n"
        if command == "dumpsys battery":
            return "Current Battery Service state:\n  level: 90\n"
        if command == "cat /sys/class/thermal/thermal_zone7/temp":
            return "31500\n"
        raise AssertionError(command)

    monkeypatch.setattr(android_backend.adb, "shell", fake_shell)
    backend = android_backend.AndroidBackend()
    monitor = android_backend.AndroidBatteryTemperatureMonitor(backend, "SERIAL")

    sample = monitor.sample()

    assert sample is not None
    assert sample.temperature_c == 31.5
    assert sample.source == "thermal:battery"
    assert "cat /sys/class/thermal/thermal_zone3/temp" not in calls


def test_live_capture_adds_dumpsys_temperature_only_without_sysfs_battery():
    zones = {"cpu_max": 45.0}
    android_backend._add_dumpsys_battery_thermal_fallback(
        zones, "temperature: 402\n"
    )
    assert zones["battery"] == 40.2

    sysfs_zones = {"battery": 39.0}
    android_backend._add_dumpsys_battery_thermal_fallback(
        sysfs_zones, "temperature: 402\n"
    )
    assert sysfs_zones == {"battery": 39.0}


def test_android_cold_launch_order_confirms_pid_and_foreground(monkeypatch):
    backend = android_backend.AndroidBackend()
    device = DeviceInfo(serial="SERIAL", platform=Platform.ANDROID)
    package = PackageInfo(bundle_id="com.example.game", uid="10001")
    session = _Session(device, package)
    calls: list[str] = []

    monkeypatch.setattr(backend, "prepare_capture", lambda *_args: (device, package))
    monkeypatch.setattr(backend, "open_streaming", lambda *_args: session)
    ps_queries = 0

    def fake_shell(command: str, serial=None, timeout=30.0):
        nonlocal ps_queries
        del serial, timeout
        calls.append(command)
        if command.startswith("cmd package resolve-activity"):
            return "com.example.game/.MainActivity\n"
        if command.startswith("am start -W"):
            return "Status: ok\nActivity: com.example.game/.MainActivity\n"
        if command.startswith("ps -A"):
            ps_queries += 1
            return "" if ps_queries <= 2 else "  PID   UID NAME\n 1234 10001 com.example.game\n"
        if command.startswith("dumpsys activity activities"):
            return "mResumedActivity: com.example.game/.MainActivity"
        raise AssertionError(command)

    monkeypatch.setattr(android_backend.adb, "shell", fake_shell)

    assert backend.cold_start_and_open_streaming("SERIAL", package) is session
    resolve_idx = next(i for i, c in enumerate(calls) if c.startswith("cmd package"))
    start_idx = next(i for i, c in enumerate(calls) if c.startswith("am start -W"))
    ps_indices = [i for i, c in enumerate(calls) if c.startswith("ps -A")]
    focus_idx = next(i for i, c in enumerate(calls) if c.startswith("dumpsys activity"))
    assert not any(c.startswith("am force-stop") for c in calls)
    assert (
        ps_indices[0]
        < resolve_idx
        < ps_indices[1]
        < start_idx
        < ps_indices[2]
        < focus_idx
    )


def test_android_exact_target_with_shared_uid_sibling_attaches_without_mutation(monkeypatch):
    backend = android_backend.AndroidBackend()
    device = DeviceInfo(serial="SERIAL", platform=Platform.ANDROID)
    package = PackageInfo(bundle_id="com.example.game", uid="10001")
    session = _Session(device, package)
    calls: list[str] = []

    monkeypatch.setattr(backend, "prepare_capture", lambda *_args: (device, package))
    monkeypatch.setattr(backend, "open_streaming", lambda *_args: session)

    def fake_shell(command: str, serial=None, timeout=30.0):
        del serial, timeout
        calls.append(command)
        if command.startswith("ps -A"):
            return (
                "  PID   UID NAME\n"
                " 400 10001 com.sibling.game\n"
                " 1234 10001 com.example.game\n"
            )
        raise AssertionError(command)

    monkeypatch.setattr(android_backend.adb, "shell", fake_shell)

    assert backend.cold_start_and_open_streaming("SERIAL", package) is session
    assert calls == ["ps -A -o PID,UID,NAME"]


def test_android_shared_uid_sibling_only_never_launches(monkeypatch):
    backend = android_backend.AndroidBackend()
    device = DeviceInfo(serial="SERIAL", platform=Platform.ANDROID)
    package = PackageInfo(bundle_id="com.example.game", uid="10001")
    calls: list[str] = []

    monkeypatch.setattr(backend, "prepare_capture", lambda *_args: (device, package))

    def fake_shell(command: str, serial=None, timeout=30.0):
        del serial, timeout
        calls.append(command)
        if command.startswith("ps -A"):
            return "  PID   UID NAME\n 400 10001 com.sibling.game\n"
        raise AssertionError(command)

    monkeypatch.setattr(android_backend.adb, "shell", fake_shell)

    with pytest.raises(
        android_backend.adb.AdbError,
        match="has no target-owned process",
    ):
        backend.cold_start_and_open_streaming("SERIAL", package)
    assert calls == ["ps -A -o PID,UID,NAME"]


def test_android_ambiguous_custom_process_never_launches(monkeypatch):
    backend = android_backend.AndroidBackend()
    device = DeviceInfo(serial="SERIAL", platform=Platform.ANDROID)
    package = PackageInfo(bundle_id="com.example.game", uid="10001")
    calls: list[str] = []

    monkeypatch.setattr(backend, "prepare_capture", lambda *_args: (device, package))

    def fake_shell(command: str, serial=None, timeout=30.0):
        del serial, timeout
        calls.append(command)
        if command.startswith("ps -A"):
            return "  PID   UID NAME\n 5024 10001 android.process.acore\n"
        raise AssertionError(command)

    monkeypatch.setattr(android_backend.adb, "shell", fake_shell)

    with pytest.raises(
        android_backend.adb.AdbError,
        match="has no target-owned process",
    ):
        backend.cold_start_and_open_streaming("SERIAL", package)
    assert calls == ["ps -A -o PID,UID,NAME"]


def test_android_pre_launch_race_attaches_without_foreground_mutation(monkeypatch):
    backend = android_backend.AndroidBackend()
    device = DeviceInfo(serial="SERIAL", platform=Platform.ANDROID)
    package = PackageInfo(bundle_id="com.example.game", uid="10001")
    session = _Session(device, package)
    calls: list[str] = []
    ps_queries = 0

    monkeypatch.setattr(backend, "prepare_capture", lambda *_args: (device, package))
    monkeypatch.setattr(backend, "open_streaming", lambda *_args: session)

    def fake_shell(command: str, serial=None, timeout=30.0):
        nonlocal ps_queries
        del serial, timeout
        calls.append(command)
        if command.startswith("ps -A"):
            ps_queries += 1
            return (
                ""
                if ps_queries == 1
                else "  PID   UID NAME\n 5678 10001 com.example.game\n"
            )
        if command.startswith("cmd package resolve-activity"):
            return "com.example.game/.MainActivity\n"
        raise AssertionError(command)

    monkeypatch.setattr(android_backend.adb, "shell", fake_shell)

    assert backend.cold_start_and_open_streaming("SERIAL", package) is session
    assert calls == [
        "ps -A -o PID,UID,NAME",
        "cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.LAUNCHER com.example.game",
        "ps -A -o PID,UID,NAME",
    ]


def test_android_target_pid_query_failure_does_not_launch(monkeypatch):
    backend = android_backend.AndroidBackend()
    device = DeviceInfo(serial="SERIAL", platform=Platform.ANDROID)
    package = PackageInfo(bundle_id="com.example.game")
    calls: list[str] = []

    monkeypatch.setattr(backend, "prepare_capture", lambda *_args: (device, package))

    def fake_shell(command: str, serial=None, timeout=30.0):
        del serial, timeout
        calls.append(command)
        if command.startswith("pidof"):
            raise android_backend.adb.AdbError("device offline")
        raise AssertionError(command)

    monkeypatch.setattr(android_backend.adb, "shell", fake_shell)

    with pytest.raises(
        android_backend.adb.AdbError,
        match="could not check the app process state",
    ):
        backend.cold_start_and_open_streaming("SERIAL", package)
    assert calls == ["pidof com.example.game || true"]


def test_android_target_uid_query_failure_does_not_launch(monkeypatch):
    backend = android_backend.AndroidBackend()
    device = DeviceInfo(serial="SERIAL", platform=Platform.ANDROID)
    package = PackageInfo(bundle_id="com.example.game", uid="10001")
    calls: list[str] = []

    monkeypatch.setattr(backend, "prepare_capture", lambda *_args: (device, package))

    def fake_shell(command: str, serial=None, timeout=30.0):
        del serial, timeout
        calls.append(command)
        if command.startswith("ps -A"):
            raise android_backend.adb.AdbError("device offline")
        raise AssertionError(command)

    monkeypatch.setattr(android_backend.adb, "shell", fake_shell)

    with pytest.raises(
        android_backend.adb.AdbError,
        match="could not check the app process state",
    ):
        backend.cold_start_and_open_streaming("SERIAL", package)
    assert calls == ["ps -A -o PID,UID,NAME"]


def test_android_target_without_usable_uid_and_no_pid_does_not_launch(monkeypatch):
    backend = android_backend.AndroidBackend()
    device = DeviceInfo(serial="SERIAL", platform=Platform.ANDROID)
    package = PackageInfo(bundle_id="com.example.game", uid="not-a-uid")
    calls: list[str] = []

    monkeypatch.setattr(backend, "prepare_capture", lambda *_args: (device, package))

    def fake_shell(command: str, serial=None, timeout=30.0):
        del serial, timeout
        calls.append(command)
        if command.startswith("pidof"):
            return ""
        raise AssertionError(command)

    monkeypatch.setattr(android_backend.adb, "shell", fake_shell)

    with pytest.raises(
        android_backend.adb.AdbError,
        match="could not determine target process state",
    ):
        backend.cold_start_and_open_streaming("SERIAL", package)
    assert calls == ["pidof com.example.game || true"]


def test_android_clone_pidof_does_not_block_selected_profile_launch(monkeypatch):
    backend = android_backend.AndroidBackend()
    device = DeviceInfo(serial="SERIAL", platform=Platform.ANDROID)
    package = PackageInfo(bundle_id="com.example.game", uid="10001")
    session = _Session(device, package)
    calls: list[str] = []

    monkeypatch.setattr(backend, "prepare_capture", lambda *_args: (device, package))
    monkeypatch.setattr(backend, "open_streaming", lambda *_args: session)

    def fake_shell(command: str, serial=None, timeout=30.0):
        del serial, timeout
        calls.append(command)
        if command.startswith("pidof"):
            return "5383\n"  # Secure Folder clone: uid 15010001
        if command.startswith("ps -A"):
            if any(c.startswith("am start -W") for c in calls):
                return "  PID   UID NAME\n 1234 10001 com.example.game\n"
            return "  PID   UID NAME\n 5383 15010001 com.example.game\n"
        if command.startswith("cmd package resolve-activity"):
            return "com.example.game/.MainActivity\n"
        if command.startswith("am start -W"):
            return "Status: ok\n"
        if command.startswith("dumpsys activity"):
            return "mResumedActivity: com.example.game/.MainActivity"
        raise AssertionError(command)

    monkeypatch.setattr(android_backend.adb, "shell", fake_shell)

    assert backend.cold_start_and_open_streaming("SERIAL", package) is session
    assert any(c.startswith("am start -W") for c in calls)
    assert not any(c.startswith("pidof") for c in calls)


def test_android_post_launch_cancellation_never_force_stops_app(monkeypatch):
    backend = android_backend.AndroidBackend()
    device = DeviceInfo(serial="SERIAL", platform=Platform.ANDROID)
    package = PackageInfo(bundle_id="com.example.game", uid="10001")
    stop = threading.Event()
    calls: list[str] = []

    monkeypatch.setattr(backend, "prepare_capture", lambda *_args: (device, package))

    def fake_shell(command: str, serial=None, timeout=30.0):
        del serial, timeout
        calls.append(command)
        if command.startswith("am force-stop"):
            return ""
        if command.startswith("cmd package resolve-activity"):
            return "com.example.game/.MainActivity\n"
        if command.startswith("am start -W"):
            return "Status: ok\n"
        if command.startswith("ps -A"):
            if any(c.startswith("am start -W") for c in calls):
                stop.set()
            return ""
        if command.startswith("dumpsys activity"):
            return ""
        raise AssertionError(command)

    monkeypatch.setattr(android_backend.adb, "shell", fake_shell)

    with pytest.raises(RuntimeError, match="temperature_guard_cancelled"):
        backend.cold_start_and_open_streaming(
            "SERIAL", package, stop_event=stop
        )
    assert not any(c.startswith("am force-stop") for c in calls)


def test_android_post_launch_error_never_force_stops_app(monkeypatch):
    backend = android_backend.AndroidBackend()
    device = DeviceInfo(serial="SERIAL", platform=Platform.ANDROID)
    package = PackageInfo(bundle_id="com.example.game", uid="10001")
    calls: list[str] = []

    monkeypatch.setattr(backend, "prepare_capture", lambda *_args: (device, package))

    def fail_open(*_args):
        raise RuntimeError("stream setup failed")

    monkeypatch.setattr(backend, "open_streaming", fail_open)

    def fake_shell(command: str, serial=None, timeout=30.0):
        del serial, timeout
        calls.append(command)
        if command.startswith("am force-stop"):
            return ""
        if command.startswith("cmd package resolve-activity"):
            return "com.example.game/.MainActivity\n"
        if command.startswith("am start -W"):
            return "Status: ok\n"
        if command.startswith("ps -A"):
            return (
                "  PID   UID NAME\n 1234 10001 com.example.game\n"
                if any(c.startswith("am start -W") for c in calls)
                else ""
            )
        if command.startswith("dumpsys activity"):
            return "mResumedActivity: com.example.game/.MainActivity"
        raise AssertionError(command)

    monkeypatch.setattr(android_backend.adb, "shell", fake_shell)

    with pytest.raises(RuntimeError, match="stream setup failed"):
        backend.cold_start_and_open_streaming("SERIAL", package)
    assert any(c.startswith("am start -W") for c in calls)
    assert not any(c.startswith("am force-stop") for c in calls)
