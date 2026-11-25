"""Live-capture handler tests for mpi.api.

Uses a fake AndroidBackend so the tests don't require a real device or adb.
"""

from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone

import pytest

from mpi import api
from mpi.android import adb, companion
from mpi.android import backend as android_backend
from mpi.schema import (
    BatteryStats,
    CpuStats,
    DeviceInfo,
    FrameStats,
    GpuStats,
    MemStats,
    MetricsSnapshot,
    NetworkStats,
    PackageInfo,
    Platform,
    ThermalStats,
)


@pytest.mark.parametrize("run_adb", [adb.run, adb.run_bytes])
def test_capture_adb_commands_leave_json_input_to_the_engine(monkeypatch, run_adb):
    def invoke(command, **kwargs):
        assert kwargs["stdin"] == subprocess.DEVNULL
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", invoke)
    run_adb(["shell", "cat"], serial="fake")


def _batched_android_getprop() -> str:
    values = {
        "ro.product.model": "FakePhone",
        "ro.build.version.release": "16",
        "ro.build.version.sdk": "36",
        "ro.product.device": "fake",
        "ro.product.board": "board",
        "ro.build.display.id": "BUILD.TEST1234",
        "ro.serialno": "HW123",
    }
    return "\n".join(f"[{key}]: [{value}]" for key, value in values.items())


class _FakeStreamingSession:
    def __init__(self, device: DeviceInfo, package: PackageInfo, fps_seq: list[float]):
        self.device = device
        self.package = package
        self._fps_seq = fps_seq
        self._i = 0

    def sample(self, duration_sec: float) -> MetricsSnapshot:
        # Tiny sleep to mimic a real capture; tests stop the pump quickly.
        time.sleep(0.005)
        fps = self._fps_seq[self._i % len(self._fps_seq)]
        self._i += 1
        return MetricsSnapshot(
            device=self.device,
            package=self.package,
            captured_at=datetime.now(timezone.utc).isoformat(),
            duration_sec=duration_sec,
            frames=FrameStats(fps_estimate=fps),
        )


class _FakeBackend:
    def __init__(self):
        self.devices = [
            DeviceInfo(
                serial="FAKE-SERIAL-1",
                platform=Platform.ANDROID,
                model="FakePhone",
                os_version="Android 16 (SDK 36)",
            )
        ]
        self.packages = {
            "FAKE-SERIAL-1": [
                PackageInfo(bundle_id="com.example.alpha", label="Alpha"),
                PackageInfo(bundle_id="com.example.beta", label="Beta"),
            ]
        }
        self.fps_sequences: dict[str, list[float]] = {}
        self.terminate_calls: list[tuple[str, str]] = []

    def list_devices(self):
        return list(self.devices)

    def prepare_wireless(self, serial: str, port: int = 5555):
        device = DeviceInfo(
            serial=f"192.168.0.10:{port}",
            platform=Platform.ANDROID,
            model="FakePhone",
            os_version="Android 16 (SDK 36)",
            connection_type="wireless",
        )
        return {"serial": device.serial, "ip": "192.168.0.10", "port": port, "device": device}

    def list_packages(self, serial: str, user_only: bool = True):
        del user_only
        return list(self.packages.get(serial, []))

    def open_streaming(self, serial: str, package: PackageInfo):
        device = next((d for d in self.devices if d.serial == serial), self.devices[0])
        seq = self.fps_sequences.get(package.bundle_id, [60.0, 59.5, 58.0])
        return _FakeStreamingSession(device, package, seq)

    def terminate_app(self, serial: str, bundle_id: str) -> None:
        self.terminate_calls.append((serial, bundle_id))


class _FakeIOSBackend(_FakeBackend):
    def __init__(self):
        self.devices = [
            DeviceInfo(
                serial="IOS-UDID-1",
                platform=Platform.IOS,
                model="Fake iPhone",
                os_version="iOS 18.2",
            )
        ]
        self.packages = {
            "IOS-UDID-1": [
                PackageInfo(bundle_id="com.example.iosgame", label="iOS Game"),
            ]
        }
        self.fps_sequences: dict[str, list[float]] = {}

    def prepare_wireless(self, serial: str):
        device = DeviceInfo(
            serial=serial,
            platform=Platform.IOS,
            model="Fake iPhone",
            os_version="iOS 18.2",
            connection_type="wireless",
        )
        return {"serial": serial, "device": device, "message": "already wireless"}


@pytest.fixture
def fake_backend(monkeypatch):
    backend = _FakeBackend()
    monkeypatch.setattr(api, "_android_backend", backend)

    def no_companion(*args, **kwargs):
        raise companion.CompanionError("no companion on fake device")

    monkeypatch.setattr(companion, "open_overlay_session", no_companion)
    yield backend
    # ensure no captures linger between tests
    with api._captures_lock:
        for ctx in list(api._active_captures.values()):
            ctx.stop.set()
        api._active_captures.clear()


@pytest.fixture
def fake_ios_backend(monkeypatch):
    backend = _FakeIOSBackend()
    monkeypatch.setattr(api, "_ios_backend", backend)
    yield backend
    with api._captures_lock:
        for ctx in list(api._active_captures.values()):
            ctx.stop.set()
        api._active_captures.clear()


@pytest.fixture
def captured_emits(monkeypatch):
    """Capture every payload that api._emit writes (instead of sending to stdout)."""
    emitted: list[dict] = []
    monkeypatch.setattr(api, "_emit", lambda payload: emitted.append(payload))
    return emitted


def test_list_devices_returns_fake_device(fake_backend):
    result = api._handle_list_devices({})
    assert "devices" in result
    assert len(result["devices"]) == 1
    assert result["devices"][0].serial == "FAKE-SERIAL-1"


def test_list_devices_routes_to_ios_backend(fake_ios_backend):
    result = api._handle_list_devices({"platform": "ios"})
    assert result["platform"] == "ios"
    assert len(result["devices"]) == 1
    assert result["devices"][0].serial == "IOS-UDID-1"
    assert result["devices"][0].platform == Platform.IOS


def test_list_devices_all_overlaps_platforms_and_preserves_errors(monkeypatch):
    barrier = threading.Barrier(2, timeout=2)

    class Backend:
        def __init__(self, device=None, error=None):
            self.device = device
            self.error = error

        def list_devices(self):
            barrier.wait()
            if self.error:
                raise self.error
            return [self.device]

    android = DeviceInfo(serial="A", platform=Platform.ANDROID)
    monkeypatch.setattr(api, "_android_backend", Backend(device=android))
    monkeypatch.setattr(api, "_ios_backend", Backend(error=RuntimeError("ios down")))

    result = api._handle_list_devices({"platform": "all"})

    assert result["devices"] == [android]
    assert result["errors"] == {"ios": "RuntimeError: ios down"}


def test_list_packages_filters_by_serial(fake_backend):
    result = api._handle_list_packages({"serial": "FAKE-SERIAL-1"})
    assert result["serial"] == "FAKE-SERIAL-1"
    assert len(result["packages"]) == 2
    assert result["packages"][0].bundle_id == "com.example.alpha"


def test_list_packages_routes_to_ios_backend(fake_ios_backend):
    result = api._handle_list_packages({"platform": "ios", "serial": "IOS-UDID-1"})
    assert result["platform"] == "ios"
    assert result["serial"] == "IOS-UDID-1"
    assert result["packages"][0].bundle_id == "com.example.iosgame"


def test_list_packages_requires_serial(fake_backend):
    with pytest.raises(ValueError):
        api._handle_list_packages({})


def test_prepare_wireless_returns_wireless_device(fake_backend):
    result = api._handle_prepare_wireless({"serial": "FAKE-SERIAL-1", "port": 5555})
    assert result["serial"] == "192.168.0.10:5555"
    assert result["device"].connection_type == "wireless"


def test_prepare_wireless_routes_to_ios_backend(fake_ios_backend):
    result = api._handle_prepare_wireless({"platform": "ios", "serial": "IOS-UDID-1"})
    assert result["device"].platform == Platform.IOS
    assert result["device"].connection_type == "wireless"


def test_start_capture_returns_id_and_records_active(fake_backend, captured_emits):
    result = api._handle_start_capture(
        {"serial": "FAKE-SERIAL-1", "package": "com.example.alpha"}
    )
    assert "capture_id" in result
    assert result["device"].serial == "FAKE-SERIAL-1"
    assert result["duration_sec"] == 0.5
    assert result["package"].bundle_id == "com.example.alpha"
    cid = result["capture_id"]
    with api._captures_lock:
        assert cid in api._active_captures
        assert result["session_id"] == api._active_captures[cid].mobile_session_id
        assert result["session_id"]

    api._handle_stop_capture({"capture_id": cid})
    # wait for pump thread to wind down
    time.sleep(0.3)
    with api._captures_lock:
        assert cid not in api._active_captures


def test_start_capture_routes_to_ios_streaming_backend(fake_ios_backend, captured_emits):
    result = api._handle_start_capture(
        {
            "platform": "ios",
            "serial": "IOS-UDID-1",
            "package": "com.example.iosgame",
        }
    )
    assert result["platform"] == "ios"
    assert result["duration_sec"] == 1.0
    assert result["device"].platform == Platform.IOS
    cid = result["capture_id"]

    time.sleep(0.1)
    api._handle_stop_capture({"capture_id": cid})
    time.sleep(0.1)

    snapshots = [e for e in captured_emits if e.get("event") == "snapshot" and e.get("capture_id") == cid]
    assert snapshots
    assert all(s["data"].device.platform == Platform.IOS for s in snapshots)


def test_start_capture_emits_snapshot_events(fake_backend, captured_emits):
    fake_backend.fps_sequences["com.example.alpha"] = [60.0, 59.0, 58.0, 57.0]
    result = api._handle_start_capture(
        {"serial": "FAKE-SERIAL-1", "package": "com.example.alpha", "duration_sec": 0.02}
    )
    cid = result["capture_id"]

    # Let the pump emit a few samples
    time.sleep(0.2)
    api._handle_stop_capture({"capture_id": cid})
    time.sleep(0.2)

    snapshots = [e for e in captured_emits if e.get("event") == "snapshot" and e.get("capture_id") == cid]
    ended = [e for e in captured_emits if e.get("event") == "capture_ended" and e.get("capture_id") == cid]

    assert len(snapshots) >= 2, f"expected multiple snapshots, got {len(snapshots)}"
    assert all(s["data"].frames.fps_estimate is not None for s in snapshots)
    assert len(ended) == 1


def test_stop_capture_unknown_id_returns_not_running(fake_backend):
    result = api._handle_stop_capture({"capture_id": "nope"})
    assert result["status"] == "not_running"


@pytest.mark.parametrize(("read_time", "backoff"), [(0.125, 0.375), (0.5, 0), (1.0, 0)])
def test_capture_error_retries_on_next_sample_interval(monkeypatch, captured_emits, read_time, backoff):
    now = [10.0]
    waits = []
    stop = threading.Event()
    monkeypatch.setattr(api.time, "monotonic", lambda: now[0])

    class FailingSession:
        def sample(self, duration_sec):
            now[0] += read_time
            raise OSError("unavailable")

    def wait_and_stop(timeout):
        waits.append(timeout)
        stop.set()

    monkeypatch.setattr(stop, "wait", wait_and_stop)
    api._capture_pump("test-retry", FailingSession(), stop, 0.5)
    assert waits == [backoff]
    assert [event["event"] for event in captured_emits] == ["capture_error", "capture_ended"]
    assert captured_emits[0]["recoverable"] is True


def test_start_capture_validates_inputs(fake_backend):
    with pytest.raises(ValueError):
        api._handle_start_capture({"serial": "x"})  # missing package
    with pytest.raises(ValueError):
        api._handle_start_capture({"package": "y"})  # missing serial
    with pytest.raises(ValueError):
        api._handle_start_capture(
            {"serial": "x", "package": "y", "duration_sec": 0}
        )
    for interval in (-1, True, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            api._handle_start_capture(
                {"serial": "x", "package": "y", "duration_sec": interval}
            )
    for value in (True, "2", 0, -1, float("nan"), float("inf"), 3601):
        with pytest.raises(ValueError):
            api._handle_start_capture(
                {"serial": "x", "package": "y", "auto_stop_after_sec": value}
            )


def test_manual_stop_and_auto_stop_off_never_terminate_target(
    fake_backend, captured_emits
):
    result = api._handle_start_capture(
        {"serial": "FAKE-SERIAL-1", "package": "com.example.alpha", "duration_sec": 0.01}
    )
    api._handle_stop_capture({"capture_id": result["capture_id"]})
    time.sleep(0.1)

    assert fake_backend.terminate_calls == []
    assert any(
        event.get("event") == "capture_ended"
        and event.get("capture_id") == result["capture_id"]
        for event in captured_emits
    )


def test_auto_stop_terminates_once_and_marks_ended(
    fake_backend, captured_emits, monkeypatch
):
    monkeypatch.setattr(api, "AUTO_STOP_MIN_SEC", 0.01)
    result = api._handle_start_capture(
        {
            "serial": "FAKE-SERIAL-1",
            "package": "com.example.alpha",
            "duration_sec": 0.005,
            "auto_stop_after_sec": 0.03,
        }
    )
    capture_id = result["capture_id"]
    time.sleep(0.2)

    assert fake_backend.terminate_calls == [("FAKE-SERIAL-1", "com.example.alpha")]
    ended = [
        event for event in captured_emits
        if event.get("event") == "capture_ended" and event.get("capture_id") == capture_id
    ]
    assert len(ended) == 1
    assert ended[0]["reason"] == "auto_stop"
    assert ended[0]["elapsed_sec"] == pytest.approx(0.03)


def test_auto_stop_manual_race_does_not_duplicate_termination(
    fake_backend, captured_emits, monkeypatch
):
    monkeypatch.setattr(api, "AUTO_STOP_MIN_SEC", 0.01)
    entered_termination = threading.Event()
    release_termination = threading.Event()

    def blocking_terminate(serial: str, bundle_id: str) -> None:
        fake_backend.terminate_calls.append((serial, bundle_id))
        entered_termination.set()
        release_termination.wait(1.0)

    fake_backend.terminate_app = blocking_terminate
    result = api._handle_start_capture(
        {
            "serial": "FAKE-SERIAL-1",
            "package": "com.example.alpha",
            "duration_sec": 0.005,
            "auto_stop_after_sec": 0.03,
        }
    )
    assert entered_termination.wait(1.0)
    assert api._handle_stop_capture({"capture_id": result["capture_id"]})["status"] == "stopping"
    release_termination.set()
    time.sleep(0.1)

    assert fake_backend.terminate_calls == [("FAKE-SERIAL-1", "com.example.alpha")]
    assert len([
        event for event in captured_emits
        if event.get("event") == "capture_ended" and event.get("capture_id") == result["capture_id"]
    ]) == 1


def test_auto_stop_termination_failure_still_ends_capture(
    fake_backend, captured_emits, monkeypatch
):
    monkeypatch.setattr(api, "AUTO_STOP_MIN_SEC", 0.01)

    def failed_terminate(serial: str, bundle_id: str) -> None:
        raise RuntimeError("device disconnected")

    fake_backend.terminate_app = failed_terminate
    result = api._handle_start_capture(
        {
            "serial": "FAKE-SERIAL-1",
            "package": "com.example.alpha",
            "duration_sec": 0.005,
            "auto_stop_after_sec": 0.03,
        }
    )
    time.sleep(0.2)
    ended = [
        event for event in captured_emits
        if event.get("event") == "capture_ended" and event.get("capture_id") == result["capture_id"]
    ]
    assert len(ended) == 1
    assert "RuntimeError: device disconnected" == ended[0]["target_app_termination_error"]


def test_auto_stop_is_not_armed_until_a_snapshot_is_emitted(
    fake_backend, captured_emits, monkeypatch
):
    monkeypatch.setattr(api, "AUTO_STOP_MIN_SEC", 0.01)
    released = threading.Event()

    class NoSnapshotSession(_FakeStreamingSession):
        def sample(self, duration_sec: float):
            released.wait(1.0)
            return super().sample(duration_sec)

        def close(self) -> None:
            released.set()

    def open_no_snapshot(serial: str, package: PackageInfo):
        return NoSnapshotSession(fake_backend.devices[0], package, [60.0])

    fake_backend.open_streaming = open_no_snapshot
    result = api._handle_start_capture(
        {
            "serial": "FAKE-SERIAL-1",
            "package": "com.example.alpha",
            "auto_stop_after_sec": 0.03,
        }
    )
    time.sleep(0.1)
    assert fake_backend.terminate_calls == []
    api._handle_stop_capture({"capture_id": result["capture_id"]})
    time.sleep(0.1)
    assert not [
        event for event in captured_emits
        if event.get("event") == "snapshot" and event.get("capture_id") == result["capture_id"]
    ]


def test_app_exit_after_auto_stop_does_not_emit_capture_error(
    fake_backend, captured_emits, monkeypatch
):
    monkeypatch.setattr(api, "AUTO_STOP_MIN_SEC", 0.01)

    class ExitAfterCloseSession(_FakeStreamingSession):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.closed = False

        def sample(self, duration_sec: float):
            if self._i:
                time.sleep(0.05)
                if self.closed:
                    raise api.AppExitedError("app-exited after automatic termination")
            return super().sample(duration_sec)

        def close(self) -> None:
            self.closed = True

    def open_exit_after_close(serial: str, package: PackageInfo):
        return ExitAfterCloseSession(fake_backend.devices[0], package, [60.0])

    fake_backend.open_streaming = open_exit_after_close
    result = api._handle_start_capture(
        {
            "serial": "FAKE-SERIAL-1",
            "package": "com.example.alpha",
            "auto_stop_after_sec": 0.03,
        }
    )
    time.sleep(0.2)
    assert not [
        event for event in captured_emits
        if event.get("event") == "capture_error" and event.get("capture_id") == result["capture_id"]
    ]


def test_android_backend_classifies_usb_and_wireless_devices(monkeypatch):
    run_calls = []
    shell_calls = []

    def fake_run(args, serial=None, timeout=30.0):
      del timeout
      run_calls.append((args, serial))
      if args == ["devices", "-l"]:
          return "\n".join([
              "List of devices attached",
              "USB123 device product:test model:FakePhone",
              "192.168.0.10:5555 device product:test model:FakePhone",
          ])
      raise AssertionError(f"unexpected adb.run args: {args}")

    def fake_shell(command, serial=None, timeout=30.0):
      del timeout
      shell_calls.append((command, serial))
      if command == "getprop":
          return _batched_android_getprop()
      if command.startswith("settings get global device_name"):
          return "QA Phone\n__MPI_DEVICE_NAME_SPLIT__\nBluetooth Phone"
      raise AssertionError(f"unexpected adb.shell command: {command}")

    monkeypatch.setattr(android_backend.adb, "run", fake_run)
    monkeypatch.setattr(android_backend.adb, "shell", fake_shell)

    backend = android_backend.AndroidBackend()
    devices = backend.list_devices()
    assert [d.connection_type for d in devices] == ["usb", "wireless"]
    assert devices[0].model == "FakePhone"
    assert devices[0].display_name == "QA Phone"
    assert devices[1].hw_serial == "HW123"
    assert backend.physical_device_id("USB123") == "HW123"
    assert backend.physical_device_id("192.168.0.10:5555") == "HW123"

    # Polling repeats only the cheap authoritative transport list. Static props
    # remain one batched shell command per connected transport, and start-time
    # resolution can use the same cache without another scan.
    backend.list_devices()
    assert backend._resolve_device("USB123") is devices[0]
    assert [call for call in shell_calls if call[0] == "getprop"] == [
        ("getprop", "USB123"),
        ("getprop", "192.168.0.10:5555"),
    ]
    assert [call for call in run_calls if call[0] == ["devices", "-l"]] == [
        (["devices", "-l"], None),
        (["devices", "-l"], None),
    ]


@pytest.mark.parametrize(
    ("names_output", "market_name", "expected"),
    [
        ("null\n__MPI_DEVICE_NAME_SPLIT__\nBluetooth Phone", "", "Bluetooth Phone"),
        ("null\n__MPI_DEVICE_NAME_SPLIT__\nnull", "Market Phone", "Market Phone"),
        ("null\n__MPI_DEVICE_NAME_SPLIT__\nunknown", "", "FakePhone"),
    ],
)
def test_android_display_name_fallback_order(
    monkeypatch, names_output, market_name, expected
):
    monkeypatch.setattr(
        android_backend.adb,
        "run",
        lambda args, serial=None, timeout=30.0: (
            "List of devices attached\nUSB123 device product:test model:FakePhone"
        ),
    )

    def fake_shell(command, serial=None, timeout=30.0):
        if command == "getprop":
            base = _batched_android_getprop()
            if market_name:
                base += f"\n[ro.product.marketname]: [{market_name}]"
            return base
        if command.startswith("settings get global device_name"):
            return names_output
        raise AssertionError(f"unexpected adb.shell command: {command}")

    monkeypatch.setattr(android_backend.adb, "shell", fake_shell)

    [device] = android_backend.AndroidBackend().list_devices()

    assert device.display_name == expected
    assert device.model == "FakePhone"


def test_android_static_startup_probes_overlap_and_cache(monkeypatch):
    backend = android_backend.AndroidBackend()
    barrier = threading.Barrier(5, timeout=2)
    calls: list[str] = []

    def probe(name, value):
        def run(serial):
            assert serial == "S1"
            calls.append(name)
            barrier.wait()
            return value

        return run

    monkeypatch.setattr(backend, "_read_clock_ticks", probe("hz", 100))
    monkeypatch.setattr(backend, "_read_mem_total", probe("mem", 8192))
    monkeypatch.setattr(backend, "_read_cpu_metadata", probe("cpu", (8, 12345.0)))
    monkeypatch.setattr(backend, "_discover_thermal_zones", probe("thermal", [("cpu:x", 1)]))
    monkeypatch.setattr(backend, "_detect_max_refresh_hz", probe("fps", 120.0))

    first = backend._load_static_metadata("S1")
    second = backend._load_static_metadata("S1")

    assert first == second == (100, 8192, 8, [("cpu:x", 1)], 12345.0, 120.0)
    assert sorted(calls) == ["cpu", "fps", "hz", "mem", "thermal"]


def test_android_package_inventory_invalidates_changed_detail_cache(monkeypatch):
    backend = android_backend.AndroidBackend()
    key = ("S1", "com.example.game")
    backend._package_details_cache[key] = PackageInfo(
        bundle_id=key[1],
        version_name="1.0",
        version_code="1",
        apk_path="/data/app/old/base.apk",
        uid="10123",
    )

    monkeypatch.setattr(
        android_backend.adb,
        "shell",
        lambda command, serial=None: "package:/data/app/new/base.apk=com.example.game",
    )

    packages = backend.list_packages("S1")

    assert packages[0].apk_path == "/data/app/new/base.apk"
    assert key not in backend._package_details_cache


def test_android_capture_enrichment_upgrades_label_from_persistent_cache(monkeypatch):
    backend = android_backend.AndroidBackend()
    key = ("S1", "com.example.game")
    backend._package_details_cache[key] = PackageInfo(
        bundle_id=key[1],
        label=None,
        version_name="1.0",
        apk_path="/data/app/base.apk",
    )
    monkeypatch.setattr(android_backend, "_label_cache_read", lambda bundle_id: "Cached Game")

    package = backend._enrich_package("S1", PackageInfo(bundle_id=key[1]))

    assert package.label == "Cached Game"
    assert backend._package_details_cache[key].label == "Cached Game"


def test_android_backend_prepare_wireless_runs_tcpip_and_connect(monkeypatch):
    calls: list[tuple[list[str], str | None]] = []

    def fake_run(args, serial=None, timeout=30.0):
        del timeout
        calls.append((args, serial))
        if args == ["tcpip", "5555"]:
            return "restarting in TCP mode port: 5555"
        if args == ["connect", "192.168.0.10:5555"]:
            return "connected to 192.168.0.10:5555"
        if args == ["devices", "-l"]:
            return "\n".join([
                "List of devices attached",
                "USB123 device product:test model:FakePhone",
                "192.168.0.10:5555 device product:test model:FakePhone",
            ])
        raise AssertionError(f"unexpected adb.run args: {args}")

    def fake_shell(command, serial=None, timeout=30.0):
        del serial, timeout
        if command == "ip -f inet addr show wlan0":
            return "3: wlan0: <UP>\n    inet 192.168.0.10/24 brd 192.168.0.255 scope global wlan0"
        if command == "getprop":
            return _batched_android_getprop()
        if command.startswith("settings get global device_name"):
            return "null\n__MPI_DEVICE_NAME_SPLIT__\nBluetooth Phone"
        raise AssertionError(f"unexpected adb.shell command: {command}")

    monkeypatch.setattr(android_backend.adb, "run", fake_run)
    monkeypatch.setattr(android_backend.adb, "shell", fake_shell)

    result = android_backend.AndroidBackend().prepare_wireless("USB123")
    assert result["serial"] == "192.168.0.10:5555"
    assert result["device"].connection_type == "wireless"
    assert (["tcpip", "5555"], "USB123") in calls
    assert (["connect", "192.168.0.10:5555"], None) in calls


def test_android_backend_prepare_wireless_skips_unreachable_translation_ip(monkeypatch):
    def fake_run(args, serial=None, timeout=30.0):
        del serial, timeout
        if args == ["tcpip", "5555"]:
            return "restarting in TCP mode port: 5555"
        if args == ["connect", "192.168.0.10:5555"]:
            return "connected to 192.168.0.10:5555"
        if args == ["devices", "-l"]:
            return "\n".join([
                "List of devices attached",
                "USB123 device product:test model:FakePhone",
                "192.168.0.10:5555 device product:test model:FakePhone",
            ])
        raise AssertionError(f"unexpected adb.run args: {args}")

    def fake_shell(command, serial=None, timeout=30.0):
        del serial, timeout
        if command == "ip -f inet addr show wlan0":
            return "3: wlan0: <UP>\n    inet 192.0.0.2/32 scope global wlan0"
        if command == "ifconfig wlan0":
            return ""
        if command == "getprop dhcp.wlan0.ipaddress":
            return "192.168.0.10"
        if command == "getprop":
            return _batched_android_getprop()
        if command.startswith("settings get global device_name"):
            return "null\n__MPI_DEVICE_NAME_SPLIT__\nBluetooth Phone"
        raise AssertionError(f"unexpected adb.shell command: {command}")

    monkeypatch.setattr(android_backend.adb, "run", fake_run)
    monkeypatch.setattr(android_backend.adb, "shell", fake_shell)

    result = android_backend.AndroidBackend().prepare_wireless("USB123")
    assert result["serial"] == "192.168.0.10:5555"


def test_android_backend_prepare_wireless_rejects_failed_connect_output(monkeypatch):
    def fake_run(args, serial=None, timeout=30.0):
        del serial, timeout
        if args == ["tcpip", "5555"]:
            return "restarting in TCP mode port: 5555"
        if args == ["connect", "192.168.0.10:5555"]:
            return "failed to connect to 192.168.0.10:5555"
        raise AssertionError(f"unexpected adb.run args: {args}")

    def fake_shell(command, serial=None, timeout=30.0):
        del serial, timeout
        if command == "ip -f inet addr show wlan0":
            return "3: wlan0: <UP>\n    inet 192.168.0.10/24 brd 192.168.0.255 scope global wlan0"
        if command in ("ifconfig wlan0", "getprop dhcp.wlan0.ipaddress"):
            return ""
        raise AssertionError(f"unexpected adb.shell command: {command}")

    monkeypatch.setattr(android_backend.adb, "run", fake_run)
    monkeypatch.setattr(android_backend.adb, "shell", fake_shell)

    with pytest.raises(android_backend.adb.AdbError, match="wireless adb connect failed"):
        android_backend.AndroidBackend().prepare_wireless("USB123")


def test_android_backend_prepare_wireless_does_not_return_unlisted_target(monkeypatch):
    def fake_run(args, serial=None, timeout=30.0):
        del serial, timeout
        if args == ["tcpip", "5555"]:
            return "restarting in TCP mode port: 5555"
        if args == ["connect", "192.168.0.10:5555"]:
            return "connected to 192.168.0.10:5555"
        if args == ["devices", "-l"]:
            return "\n".join([
                "List of devices attached",
                "USB123 device product:test model:FakePhone",
            ])
        raise AssertionError(f"unexpected adb.run args: {args}")

    def fake_shell(command, serial=None, timeout=30.0):
        del serial, timeout
        if command == "ip -f inet addr show wlan0":
            return "3: wlan0: <UP>\n    inet 192.168.0.10/24 brd 192.168.0.255 scope global wlan0"
        if command in ("ifconfig wlan0", "getprop dhcp.wlan0.ipaddress"):
            return ""
        if command == "getprop":
            return _batched_android_getprop()
        raise AssertionError(f"unexpected adb.shell command: {command}")

    monotonic_values = iter([0.0, 11.0])

    monkeypatch.setattr(android_backend.adb, "run", fake_run)
    monkeypatch.setattr(android_backend.adb, "shell", fake_shell)
    monkeypatch.setattr(android_backend.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(android_backend.time, "sleep", lambda _seconds: None)

    with pytest.raises(android_backend.adb.AdbError, match="did not produce an online device"):
        android_backend.AndroidBackend().prepare_wireless("USB123")


# --- on-device HUD toggle (set_capture_overlay) ---------------------------
#
# The renderer fires this fire-and-forget on every toggle with no debounce, onto
# the worker pool, so overlapping calls for one capture are the normal case, not
# a corner case. An opened session that is dropped instead of closed keeps its
# socket, its daemon pusher thread and its `adb forward` rule for the life of the
# sidecar, so these tests assert on the forward rules adb was asked to remove.


def _serve_overlay(server_side: socket.socket) -> None:
    """Answer overlay_config/overlay_update lines until the client goes away."""
    reader = server_side.makefile("r", encoding="utf-8", newline="\n")
    try:
        for line in reader:
            request = json.loads(line)
            args = request.get("args") or {}
            data = {"enabled": bool(args.get("enabled"))}
            server_side.sendall((json.dumps({"ok": True, "data": data}) + "\n").encode("utf-8"))
    except (OSError, ValueError):
        pass
    finally:
        try:
            server_side.close()
        except OSError:
            pass


def _install_overlay_opener(monkeypatch, opened, removed, delay=0.0, before_return=None):
    """Replace companion.open_overlay_session with a real session on a socketpair.

    Real sessions (not stubs) so close() exercises the whole teardown: disable on
    the wire, socket, then the forward rule the leak is measured by."""
    ports = iter(range(41234, 41334))

    monkeypatch.setattr(
        adb, "forward_remove", lambda local, serial=None: removed.append(local)
    )

    def fake_open(serial, package="", metrics=None, allow_cold_start=True, **kwargs):
        assert allow_cold_start is False  # never `am start` over a running capture
        time.sleep(delay)  # adb forward + connect + configure is not instant
        if before_return is not None:
            before_return()
        client_side, server_side = socket.socketpair()
        threading.Thread(target=_serve_overlay, args=(server_side,), daemon=True).start()
        session = companion.OverlaySession(
            serial, client_side, f"tcp:{next(ports)}", package=package, metrics=metrics,
            enabled=kwargs.get("enabled", True), session_id=kwargs.get("session_id"),
            label=kwargs.get("label"),
        )
        opened.append(session)
        session.configure()  # the real opener sends the initial config too
        return session

    monkeypatch.setattr(companion, "open_overlay_session", fake_open)


def _register_overlay_capture(capture_id="overlay-capture"):
    """Register a capture context without a pump: these tests drive the toggle
    handler, not the measurement loop."""
    device = DeviceInfo(
        serial="FAKE-SERIAL-1",
        platform=Platform.ANDROID,
        model="FakePhone",
        os_version="Android 16 (SDK 36)",
    )
    package = PackageInfo(bundle_id="com.example.alpha", label="Alpha")
    ctx = api._CaptureContext(
        thread=threading.Thread(target=lambda: None),
        stop=threading.Event(),
        session=_FakeStreamingSession(device, package, [60.0]),
        duration_sec=0.5,
        serial="FAKE-SERIAL-1",
        platform="android",
    )
    with api._captures_lock:
        api._active_captures[capture_id] = ctx
    return capture_id, ctx


def _drop_overlay_capture(capture_id):
    with api._captures_lock:
        ctx = api._active_captures.pop(capture_id, None)
        overlay = ctx.overlay if ctx is not None else None
        if ctx is not None:
            ctx.overlay = None
    api._close_overlay(overlay)


def test_overlay_cpu_projects_whole_device_total_not_app_cpu():
    snap = MetricsSnapshot(
        device=DeviceInfo(serial="S1", platform=Platform.ANDROID, model="Fake", os_version="16"),
        package=PackageInfo(bundle_id="com.example.game"),
        captured_at=datetime.now(timezone.utc).isoformat(),
        duration_sec=0.5,
        cpu=CpuStats(cpu_percent=280.0, cpu_norm=42.0, total_percent=17.5),
    )

    assert api._overlay_values(snap)["cpu"] == 17.5


def test_mobile_dashboard_projects_units_and_cpu_scales():
    snap = MetricsSnapshot(
        device=DeviceInfo(serial="S1", platform=Platform.ANDROID, model="Fake", os_version="16"),
        package=PackageInfo(bundle_id="com.example.game"),
        captured_at=datetime.now(timezone.utc).isoformat(),
        duration_sec=0.5,
        frames=FrameStats(total_frames=200, janky_frames=7, fps_estimate=58.5, jank_percent=3.5),
        cpu=CpuStats(cpu_percent=280.0, cpu_norm=42.0, total_percent=17.5),
        gpu=GpuStats(busy_percent=38.0),
        memory=MemStats(pss_total_kb=153600, rss_total_kb=204800, sys_used_kb=3072000),
        battery=BatteryStats(level_percent=80, current_ma=-250.0, voltage_mv=4000.0),
        thermal=ThermalStats(zones_c={"battery": 32.5, "cpu_max": 60.0}),
        network=NetworkStats(rx_kbps=125.0, tx_kbps=8.0),
    )
    assert api._overlay_values(snap, 12.5) == {
        "elapsed": 12.5, "fps": 58.5, "cpu": 17.5, "cpu_app": 280.0,
        "gpu": 38.0, "temp": 32.5, "memory": 150.0, "memory_system": 3000.0,
        "battery": 80.0, "power": 1000.0, "jank": 3.5, "rx": 125.0, "tx": 8.0,
    }


def test_mobile_dashboard_memory_fallback_and_missing_values():
    snap = MetricsSnapshot(
        device=DeviceInfo(serial="S1", platform=Platform.ANDROID, model="Fake", os_version="16"),
        package=PackageInfo(bundle_id="com.example.game"),
        captured_at=datetime.now(timezone.utc).isoformat(),
        duration_sec=0.5,
        memory=MemStats(pss_total_kb=0, rss_total_kb=102400),
        gpu=GpuStats(busy_percent=float("nan")),
        thermal=ThermalStats(zones_c={"cpu_max": 60.0}),
    )
    values = api._overlay_values(snap)
    assert values["memory"] == 100.0
    assert all(values[key] is None for key in ("memory_system", "power", "battery", "gpu", "temp", "rx", "tx", "jank"))
    snap.memory.rss_total_kb = 0
    assert api._overlay_values(snap)["memory"] is None


@pytest.mark.parametrize("total_frames,jank_percent,expected", [(0, 0.0, None), (120, 0.0, 0.0), (120, 5.0, 5.0)])
def test_mobile_dashboard_jank_requires_measured_frames(total_frames, jank_percent, expected):
    snap = MetricsSnapshot(
        device=DeviceInfo(serial="S1", platform=Platform.ANDROID, model="Fake", os_version="16"),
        package=PackageInfo(bundle_id="com.example.game"),
        captured_at=datetime.now(timezone.utc).isoformat(),
        duration_sec=0.5,
        frames=FrameStats(total_frames=total_frames, jank_percent=jank_percent),
    )
    assert api._overlay_values(snap)["jank"] == expected


@pytest.mark.parametrize("overlay_config", [None, {"enabled": False}, {"enabled": True}])
def test_android_capture_streams_mobile_dashboard_regardless_of_hud(
    fake_backend, captured_emits, monkeypatch, overlay_config,
):
    opened, updates = [], []
    closed = threading.Event()
    sampled = threading.Event()

    class MobileSession:
        def update(self, values):
            updates.append(values)
            sampled.set()

        def close(self):
            closed.set()

    def fake_open(serial, **kwargs):
        assert kwargs["allow_cold_start"] is bool(overlay_config and overlay_config["enabled"])
        opened.append(kwargs)
        return MobileSession()

    monkeypatch.setattr(companion, "open_overlay_session", fake_open)
    payload = {"serial": "FAKE-SERIAL-1", "package": "com.example.alpha", "duration_sec": 0.5}
    if overlay_config is not None:
        payload["overlay"] = overlay_config
    result = api._handle_start_capture(payload)
    capture_id = result["capture_id"]
    try:
        assert sampled.wait(3)
        with api._captures_lock:
            context = api._active_captures[capture_id]
        assert opened[0]["session_id"] == context.mobile_session_id
        assert opened[0]["enabled"] is bool(overlay_config and overlay_config["enabled"])
        assert updates[0]["fps"] == 60.0
        assert updates[0]["elapsed"] == 0.0
    finally:
        api._handle_stop_capture({"capture_id": capture_id})
    assert closed.wait(3)


def test_ios_capture_does_not_open_mobile_dashboard(monkeypatch):
    monkeypatch.setattr(
        companion, "open_overlay_session",
        lambda *a, **k: pytest.fail("iOS has no Android companion"),
    )
    assert api._open_overlay_session("ios", "S1", "com.example.game", None) is None


def test_mobile_dashboard_connection_failure_leaves_capture_running(
    fake_backend, captured_emits, monkeypatch,
):
    sampled = threading.Event()
    monkeypatch.setattr(api, "_emit", lambda payload: sampled.set() if payload.get("event") == "snapshot" else None)
    result = api._handle_start_capture({"serial": "FAKE-SERIAL-1", "package": "com.example.alpha"})
    try:
        assert sampled.wait(3)
    finally:
        api._handle_stop_capture({"capture_id": result["capture_id"]})


def test_overlay_elapsed_starts_at_first_real_sample(monkeypatch):
    capture_id, ctx = _register_overlay_capture("overlay-elapsed")
    updates = []

    class FakeOverlay:
        def update(self, values):
            updates.append(values)

        def close(self):
            return None

    ctx.overlay = FakeOverlay()
    clock = iter((100.0, 105.25))
    monkeypatch.setattr(api.time, "monotonic", lambda: next(clock))
    snap = MetricsSnapshot(
        device=DeviceInfo(
            serial="S1",
            platform=Platform.ANDROID,
            model="Fake",
            os_version="16",
        ),
        package=PackageInfo(bundle_id="com.example.game"),
        captured_at=datetime.now(timezone.utc).isoformat(),
        duration_sec=0.5,
    )

    try:
        api._push_overlay_sample(capture_id, snap)
        api._push_overlay_sample(capture_id, snap)
    finally:
        _drop_overlay_capture(capture_id)

    assert updates[0]["elapsed"] == 0.0
    assert updates[1]["elapsed"] == pytest.approx(5.25)


def test_concurrent_overlay_toggles_open_exactly_one_session(monkeypatch):
    capture_id, ctx = _register_overlay_capture()
    opened: list = []
    removed: list = []
    _install_overlay_opener(monkeypatch, opened, removed, delay=0.15)
    results: list = []
    errors: list = []

    def toggle(metrics):
        try:
            results.append(
                api._handle_set_capture_overlay(
                    {"capture_id": capture_id, "enabled": True, "metrics": metrics}
                )
            )
        except Exception as exc:  # surfaced below so the assert names the failure
            errors.append(exc)

    try:
        threads = [
            threading.Thread(target=toggle, args=(m,)) for m in (["fps", "cpu"], ["fps"])
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        assert not any(t.is_alive() for t in threads)
        assert not errors, errors

        # The second toggle must reconfigure the published session instead of
        # opening its own; a second session would be dropped on the floor with
        # its forward rule still installed.
        assert len(opened) == 1
        assert ctx.overlay is opened[0]
        assert opened[0].session_id == ctx.mobile_session_id
        assert opened[0].alive
        assert removed == []
        assert [r["enabled"] for r in results] == [True, True]
    finally:
        _drop_overlay_capture(capture_id)


def test_overlay_toggle_closes_session_when_capture_ends_mid_connect(monkeypatch):
    # The pump epilogue reads and clears the overlay slot, then pops the capture;
    # a session published after that would never be torn down again, so the
    # opener has to close what it can no longer hand over.
    capture_id, ctx = _register_overlay_capture("overlay-capture-ending")
    opened: list = []
    removed: list = []
    _install_overlay_opener(
        monkeypatch,
        opened,
        removed,
        before_return=lambda: api._active_captures.pop(capture_id, None),
    )

    try:
        with pytest.raises(ValueError, match="unknown capture_id"):
            api._handle_set_capture_overlay(
                {"capture_id": capture_id, "enabled": True, "metrics": ["fps"]}
            )

        assert len(opened) == 1
        assert not opened[0].alive
        assert removed == [opened[0]._forward_rule]  # forward rule handed back
        assert ctx.overlay is None
    finally:
        _drop_overlay_capture(capture_id)


def test_overlay_toggle_does_not_reenable_a_closed_session(monkeypatch):
    # A toggle landing as the capture ends must not reach a session the epilogue
    # is already tearing down, and must not open a replacement for a capture that
    # is on its way out.
    capture_id, ctx = _register_overlay_capture("overlay-capture-closed")
    opened: list = []
    removed: list = []
    _install_overlay_opener(monkeypatch, opened, removed)

    try:
        api._handle_set_capture_overlay(
            {"capture_id": capture_id, "enabled": True, "metrics": ["fps"]}
        )
        session = ctx.overlay
        assert session is not None
        session.close()  # what the pump epilogue does on its way out
        opened.clear()

        result = api._handle_set_capture_overlay(
            {"capture_id": capture_id, "enabled": True, "metrics": ["fps"]}
        )

        assert opened == []  # no reconnect for a capture that is ending
        assert result["enabled"] is False
        assert result["reason"] == "companion_unavailable"
        assert session.enabled is False
        assert session.hud_enabled is False
    finally:
        _drop_overlay_capture(capture_id)
