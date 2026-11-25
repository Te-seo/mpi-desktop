from __future__ import annotations

import json
import os
import threading

import pytest

from mpi.ios.parsers import (
    enrich_pymobiledevice3_device,
    find_pymobiledevice3_process,
    lookup_pymobiledevice3_pid,
    parse_activity_monitor,
    parse_core_animation,
    parse_devicectl_apps,
    parse_devicectl_devices,
    parse_pymobiledevice3_apps,
    parse_pymobiledevice3_devices,
    parse_pymobiledevice3_process_snapshot,
)
from mpi.ios import backend as ios_backend
from mpi.ios import pmd3
from mpi.schema import DeviceInfo, PackageInfo, Platform


@pytest.fixture(autouse=True)
def _reset_pmd3_device_metadata_cache():
    pmd3._clear_device_metadata_cache()
    yield
    pmd3._clear_device_metadata_cache()


def test_parse_devicectl_devices_filters_simulators_and_unavailable_devices():
    payload = {
        "result": {
            "devices": [
                {
                    "identifier": "UDID-1",
                    "connectionProperties": {
                        "tunnelState": "connected",
                        "transportType": "USB",
                    },
                    "deviceProperties": {
                        "name": "QA iPhone",
                        "osVersionNumber": "18.2",
                    },
                    "hardwareProperties": {"deviceType": "iPhone15,2"},
                },
                {
                    "identifier": "SIM-1",
                    "deviceProperties": {"name": "iPhone 15 Simulator"},
                    "hardwareProperties": {
                        "deviceType": "com.apple.CoreSimulator.SimDeviceType.iPhone-15"
                    },
                },
                {
                    "identifier": "OFFLINE-1",
                    "connectionProperties": {"tunnelState": "unavailable"},
                    "deviceProperties": {"name": "Offline iPhone"},
                },
            ]
        }
    }

    devices = parse_devicectl_devices(payload)

    assert len(devices) == 1
    assert devices[0].serial == "UDID-1"
    assert devices[0].platform.value == "ios"
    assert devices[0].display_name == "QA iPhone"
    assert devices[0].model == "iPhone 14 Pro"
    assert devices[0].model_identifier == "iPhone15,2"
    assert devices[0].os_version == "iOS 18.2"


def test_devicectl_product_version_wins_over_os_and_build_versions():
    payload = {
        "result": {
            "devices": [
                {
                    "identifier": "UDID-1",
                    "connectionProperties": {
                        "tunnelState": "connected",
                        "transportType": "USB",
                    },
                    "deviceProperties": {
                        "name": "QA iPhone",
                        "osVersion": "27.0",
                        "ProductVersion": "26.2.1",
                        "BuildVersion": "23C71",
                    },
                    "hardwareProperties": {"productType": "iPhone17,1"},
                }
            ]
        }
    }

    [device] = parse_devicectl_devices(payload)

    assert device.os_version == "iOS 26.2.1"
    assert device.display_name == "QA iPhone"
    assert device.model == "iPhone 16 Pro"


def test_parse_pymobiledevice3_devices_keeps_usb_and_wireless_entries():
    payload = [
        {
            "DeviceID": 1,
            "Properties": {
                "SerialNumber": "UDID-1",
                "ConnectionType": "USB",
                "ProductType": "iPhone15,2",
            },
        },
        {
            "DeviceID": 2,
            "Properties": {
                "SerialNumber": "UDID-1",
                "ConnectionType": "Network",
                "ProductType": "iPhone15,2",
            },
        },
    ]

    devices = parse_pymobiledevice3_devices(payload)

    assert [device.connection_type for device in devices] == ["usb", "wireless"]
    assert all(device.platform is Platform.IOS for device in devices)


def test_enrich_pymobiledevice3_device_adds_lockdown_info():
    device = DeviceInfo(serial="UDID-1", platform=Platform.IOS)

    enriched = enrich_pymobiledevice3_device(
        device,
        {
            "DeviceName": "QA iPhone",
            "ProductType": "iPhone15,2",
            "ProductVersion": "18.4",
        },
    )

    assert enriched.display_name == "QA iPhone"
    assert enriched.model == "iPhone 14 Pro"
    assert enriched.os_version == "iOS 18.4"


@pytest.mark.parametrize("product_version", ["17.7.2", "26.2.1", "27.0"])
def test_ios_market_version_prefers_product_version_and_preserves_all_components(
    product_version,
):
    device = DeviceInfo(serial="UDID-1", platform=Platform.IOS)

    enriched = enrich_pymobiledevice3_device(
        device,
        {
            "DeviceName": "QA iPhone",
            "ProductType": "iPhone17,1",
            "BuildVersion": "23C71",
            "ProductVersion": product_version,
        },
    )

    assert enriched.os_version == f"iOS {product_version}"
    assert "23C71" not in enriched.os_version


def test_parse_devicectl_apps_filters_system_apps_and_sorts_by_bundle_id():
    payload = {
        "result": {
            "apps": [
                {
                    "bundleIdentifier": "com.example.zeta",
                    "displayName": "Zeta",
                    "bundleShortVersionString": "1.2.3",
                    "bundleVersion": "45",
                    "installationType": "User",
                },
                {
                    "bundleIdentifier": "com.apple.Preferences",
                    "displayName": "Settings",
                    "installationType": "System",
                },
                {
                    "bundleIdentifier": "com.example.alpha",
                    "displayName": "Alpha",
                    "installationType": "User",
                },
            ]
        }
    }

    apps = parse_devicectl_apps(payload, user_only=True)

    assert [app.bundle_id for app in apps] == [
        "com.example.alpha",
        "com.example.zeta",
    ]
    assert apps[1].label == "Zeta"
    assert apps[1].version_name == "1.2.3"
    assert apps[1].version_code == "45"


def test_parse_pymobiledevice3_apps_accepts_bundle_mapping():
    payload = {
        "com.example.zeta": {
            "CFBundleDisplayName": "Zeta",
            "CFBundleShortVersionString": "1.2.3",
            "CFBundleVersion": "45",
            "ApplicationType": "User",
        },
        "com.apple.Preferences": {
            "CFBundleDisplayName": "Settings",
            "ApplicationType": "System",
        },
    }

    apps = parse_pymobiledevice3_apps(payload, user_only=True)

    assert [app.bundle_id for app in apps] == ["com.example.zeta"]
    assert apps[0].label == "Zeta"
    assert apps[0].version_name == "1.2.3"


def test_parse_pymobiledevice3_process_snapshot_reads_cpu_memory_and_fps():
    payload = [
        {
            "pid": 111,
            "name": "Other",
            "cpuUsage": "90%",
            "physFootprint": "900 MB",
        },
        {
            "pid": 222,
            "bundleIdentifier": "com.example.game",
            "cpuUsage": "12.5%",
            "physFootprint": "256 MB",
            "fps": 59.7,
        },
    ]

    parsed = parse_pymobiledevice3_process_snapshot(
        payload,
        pid=222,
        bundle_id="com.example.game",
    )

    assert parsed["cpu"].pid == 222
    assert parsed["cpu"].cpu_percent == 12.5
    assert parsed["memory"].rss_total_kb == 256 * 1024
    assert parsed["frames"].fps_estimate == 59.7
    process = find_pymobiledevice3_process(payload, bundle_id="com.example.game")
    assert lookup_pymobiledevice3_pid(process or {}) == 222


def test_parse_pymobiledevice3_process_snapshot_caps_extreme_fps():
    payload = [
        {
            "pid": 333,
            "bundleIdentifier": "com.example.game",
            "fps": 999,
        },
    ]

    parsed = parse_pymobiledevice3_process_snapshot(
        payload,
        pid=333,
        bundle_id="com.example.game",
    )

    # An impossible 999 fps reading is bounded by the absolute sanity ceiling
    # (240), not the old 120 hard cap — so a real 120/144 reading would pass
    # through untouched while genuine garbage is still rejected.
    assert parsed["frames"].fps_estimate == 240


def test_parse_activity_monitor_averages_last_five_samples_for_pid():
    xml = """
    <trace>
      <row><pid>123</pid><cpuUsage>1%</cpuUsage><residentMemory>100 MB</residentMemory></row>
      <row><pid>123</pid><cpuUsage>2%</cpuUsage><residentMemory>101 MB</residentMemory></row>
      <row><pid>999</pid><cpuUsage>99%</cpuUsage><residentMemory>999 MB</residentMemory></row>
      <row><pid>123</pid><cpuUsage>3%</cpuUsage><residentMemory>102 MB</residentMemory></row>
      <row><pid>123</pid><cpuUsage>4%</cpuUsage><residentMemory>103 MB</residentMemory></row>
      <row><pid>123</pid><cpuUsage>5%</cpuUsage><residentMemory>104 MB</residentMemory></row>
      <row><pid>123</pid><cpuUsage>6%</cpuUsage><residentMemory>105 MB</residentMemory></row>
    </trace>
    """

    cpu, memory = parse_activity_monitor(xml, pid=123)

    assert cpu.pid == 123
    assert cpu.cpu_percent == 4.0
    assert memory.rss_total_kb == 103 * 1024
    assert memory.pss_total_kb == memory.rss_total_kb


def test_parse_core_animation_computes_fps_jank_and_percentiles():
    xml = """
    <trace>
      <row><timestamp>0s</timestamp><frameDuration>16ms</frameDuration></row>
      <row><timestamp>0.0166667s</timestamp><frameDuration>18ms</frameDuration></row>
      <row><timestamp>0.0333334s</timestamp><frameDuration>8ms</frameDuration></row>
      <row><timestamp>0.05s</timestamp><frameDuration>33ms</frameDuration></row>
    </trace>
    """

    stats = parse_core_animation(xml, refresh_hz=60)

    assert stats.total_frames == 4
    assert stats.janky_frames == 2
    assert stats.jank_percent == 50.0
    assert stats.p50_ms == 16.0
    assert stats.p95_ms == 33.0
    assert stats.fps_estimate is not None
    assert 59.9 <= stats.fps_estimate <= 60.1


def test_parse_core_animation_burst_timestamps_fall_back_to_duration_fps():
    xml = """
    <trace>
      <row><timestamp>0s</timestamp><frameDuration>16ms</frameDuration></row>
      <row><timestamp>0.001s</timestamp><frameDuration>16ms</frameDuration></row>
      <row><timestamp>0.002s</timestamp><frameDuration>16ms</frameDuration></row>
    </trace>
    """

    stats = parse_core_animation(xml, refresh_hz=60)

    # The 1ms-apart burst timestamps imply an impossible ~1000 fps, so the
    # estimate falls back to the integer-ms duration buckets.  A 16 ms bucket
    # contains the exact 60 Hz cadence duration (16.667 ms), so restore it.
    assert stats.fps_estimate == pytest.approx(60.0)


def test_parse_core_animation_duration_fallback_uses_explicit_120hz_cadence():
    xml = """
    <trace>
      <row><timestamp>0s</timestamp><frameDuration>8ms</frameDuration></row>
      <row><timestamp>0.001s</timestamp><frameDuration>8ms</frameDuration></row>
      <row><timestamp>0.002s</timestamp><frameDuration>8ms</frameDuration></row>
    </trace>
    """

    stats = parse_core_animation(xml, refresh_hz=120)

    assert stats.fps_estimate == pytest.approx(120.0)


def test_parse_core_animation_duration_fallback_restores_60fps_on_120hz():
    xml = """
    <trace>
      <row><timestamp>0s</timestamp><frameDuration>16ms</frameDuration></row>
      <row><timestamp>0.001s</timestamp><frameDuration>16ms</frameDuration></row>
      <row><timestamp>0.002s</timestamp><frameDuration>16ms</frameDuration></row>
    </trace>
    """

    stats = parse_core_animation(xml, refresh_hz=120)

    # Each 16 ms bucket contains two 120 Hz periods, so the content cadence is 60.
    assert stats.fps_estimate == pytest.approx(60.0)


def test_parse_core_animation_duration_fallback_does_not_infer_unknown_cadence():
    xml = """
    <trace>
      <row><timestamp>0s</timestamp><frameDuration>8ms</frameDuration></row>
      <row><timestamp>0.001s</timestamp><frameDuration>8ms</frameDuration></row>
      <row><timestamp>0.002s</timestamp><frameDuration>8ms</frameDuration></row>
    </trace>
    """

    stats = parse_core_animation(xml, refresh_hz=60)

    # 8 ms is not a 60 Hz refresh-period multiple, so preserve the raw fallback.
    assert stats.fps_estimate == pytest.approx(125.0)


def test_parse_core_animation_duration_fallback_counts_dropped_refreshes():
    xml = """
    <trace>
      <row><timestamp>0s</timestamp><frameDuration>16ms</frameDuration></row>
      <row><timestamp>0.001s</timestamp><frameDuration>33ms</frameDuration></row>
      <row><timestamp>0.002s</timestamp><frameDuration>16ms</frameDuration></row>
    </trace>
    """

    stats = parse_core_animation(xml, refresh_hz=60)

    # The buckets represent one, two, and one 60 Hz refresh periods: 3 frames
    # over 4 refreshes = 45 fps, rather than the old 16 ms p50 estimate (62.5).
    assert stats.fps_estimate == pytest.approx(45.0)


def test_parse_core_animation_duration_fallback_preserves_mismatched_vrr_buckets():
    xml = """
    <trace>
      <row><timestamp>0s</timestamp><frameDuration>16ms</frameDuration></row>
      <row><timestamp>0.001s</timestamp><frameDuration>20ms</frameDuration></row>
      <row><timestamp>0.002s</timestamp><frameDuration>33ms</frameDuration></row>
    </trace>
    """

    stats = parse_core_animation(xml, refresh_hz=60)

    # 20 ms is not a 60 Hz period multiple, so cadence reconstruction is
    # abandoned for the whole VRR-like sample and the historical raw p50 wins.
    assert stats.fps_estimate == pytest.approx(50.0)


def test_parse_core_animation_high_resolution_timestamps_are_not_cadence_snapped():
    xml = """
    <trace>
      <row><timestamp>0s</timestamp><frameDuration>16ms</frameDuration></row>
      <row><timestamp>0.016s</timestamp><frameDuration>16ms</frameDuration></row>
      <row><timestamp>0.032s</timestamp><frameDuration>16ms</frameDuration></row>
    </trace>
    """

    stats = parse_core_animation(xml, refresh_hz=60)

    # A plausible high-resolution timestamp rate remains authoritative even
    # when the accompanying duration column is integer-ms quantized.
    assert stats.fps_estimate == pytest.approx(62.5)


def test_parse_core_animation_direct_fps_values_are_not_cadence_snapped():
    xml = """
    <trace>
      <row><framesPerSecond>63</framesPerSecond></row>
      <row><framesPerSecond>120</framesPerSecond></row>
      <row><framesPerSecond>144</framesPerSecond></row>
    </trace>
    """

    stats = parse_core_animation(xml, refresh_hz=60)

    assert stats.fps_estimate == pytest.approx(109.0)


def test_ios_backend_uses_pymobiledevice3_on_windows(monkeypatch):
    monkeypatch.setattr(ios_backend.sys, "platform", "win32")
    monkeypatch.setattr(
        ios_backend.pymobiledevice,
        "list_devices",
        lambda: [
            DeviceInfo(
                serial="UDID-1",
                platform=Platform.IOS,
                model="QA iPhone",
                connection_type="wireless",
            )
        ],
    )

    devices = ios_backend.IOSBackend().list_devices()

    assert devices[0].serial == "UDID-1"
    assert devices[0].connection_type == "wireless"


def test_ios_backend_prepare_wireless_uses_pymobiledevice3_on_windows(monkeypatch):
    wireless = DeviceInfo(
        serial="UDID-1",
        platform=Platform.IOS,
        connection_type="wireless",
    )
    monkeypatch.setattr(ios_backend.sys, "platform", "win32")
    monkeypatch.setattr(
        ios_backend.pymobiledevice,
        "prepare_wireless",
        lambda serial: {"serial": serial, "device": wireless},
    )

    result = ios_backend.IOSBackend().prepare_wireless("UDID-1")

    assert result["device"].platform == Platform.IOS
    assert result["device"].connection_type == "wireless"


def test_pymobiledevice3_list_devices_uses_simple_usb_and_network(monkeypatch):
    calls = []

    def fake_run_json(args, *, udid=None, timeout=60.0):
        calls.append((list(args), udid))
        if list(args) == ["usbmux", "list", "--usb", "--simple"]:
            return ["USB-UDID"]
        if list(args) == ["usbmux", "list", "--network", "--simple"]:
            return ["WIFI-UDID"]
        if list(args) == ["lockdown", "info"]:
            return {
                "DeviceName": f"iPhone {udid}",
                "ProductType": "iPhone15,2",
                "ProductVersion": "18.5",
            }
        if list(args[:2]) == ["diagnostics", "mg"]:
            return {}
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(pmd3, "run_json", fake_run_json)
    # Isolate from the real tunneld: list_devices() merges list_tunneld_devices(),
    # which reads http://127.0.0.1:49151 directly (not via run_json). On a machine
    # with a live iOS tunnel that injects an extra device and breaks this test,
    # which only exercises the usbmux --usb/--network merge.
    monkeypatch.setattr(pmd3, "list_tunneld_devices", lambda: [])

    devices = pmd3.list_devices()

    assert [(device.serial, device.connection_type) for device in devices] == [
        ("USB-UDID", "usb"),
        ("WIFI-UDID", "wireless"),
    ]
    assert devices[1].display_name == "iPhone WIFI-UDID"
    assert devices[1].model == "iPhone 14 Pro"
    assert (["lockdown", "info"], "USB-UDID") in calls
    assert (["lockdown", "info"], "WIFI-UDID") in calls


def test_pymobiledevice3_device_sources_overlap(monkeypatch):
    barrier = threading.Barrier(3, timeout=2)

    def fake_usbmux(filter_arg, connection_type):
        barrier.wait()
        serial = "USB-1" if filter_arg == "--usb" else "WIFI-1"
        return [DeviceInfo(serial=serial, platform=Platform.IOS, connection_type=connection_type)]

    def fake_tunneld():
        barrier.wait()
        return []

    monkeypatch.setattr(pmd3, "_list_usbmux_simple", fake_usbmux)
    monkeypatch.setattr(pmd3, "list_tunneld_devices", fake_tunneld)
    monkeypatch.setattr(
        pmd3,
        "run_json",
        lambda args, *, udid=None, timeout=60.0: {"DeviceName": udid},
    )

    devices = pmd3.list_devices()

    assert [(device.serial, device.connection_type) for device in devices] == [
        ("USB-1", "usb"),
        ("WIFI-1", "wireless"),
    ]


def test_pymobiledevice3_lockdown_enrichment_overlaps_and_caches(monkeypatch):
    barrier = threading.Barrier(2, timeout=2)
    lockdown_calls: list[str] = []

    def fake_usbmux(filter_arg, connection_type):
        serial = "USB-1" if filter_arg == "--usb" else "WIFI-1"
        return [DeviceInfo(serial=serial, platform=Platform.IOS, connection_type=connection_type)]

    def fake_info(args, *, udid=None, timeout=60.0):
        if list(args[:2]) == ["diagnostics", "mg"]:
            return {}
        assert list(args) == ["lockdown", "info"]
        lockdown_calls.append(str(udid))
        barrier.wait()
        return {
            "DeviceName": f"Phone {udid}",
            "ProductType": f"Hardware {udid}",
            "ProductVersion": "18.5",
        }

    monkeypatch.setattr(pmd3, "_list_usbmux_simple", fake_usbmux)
    monkeypatch.setattr(pmd3, "list_tunneld_devices", lambda: [])
    monkeypatch.setattr(pmd3, "run_json", fake_info)

    first = pmd3.list_devices()
    monkeypatch.setattr(
        pmd3,
        "run_json",
        lambda *a, **k: pytest.fail("cached lockdown metadata should be reused"),
    )
    second = pmd3.list_devices()

    assert sorted(lockdown_calls) == ["USB-1", "WIFI-1"]
    assert [device.display_name for device in first] == ["Phone USB-1", "Phone WIFI-1"]
    assert [device.model for device in first] == ["Hardware USB-1", "Hardware WIFI-1"]
    assert [device.display_name for device in second] == ["Phone USB-1", "Phone WIFI-1"]
    assert [device.model for device in second] == ["Hardware USB-1", "Hardware WIFI-1"]


def test_ios_backend_reuses_list_metadata_for_capture_open(monkeypatch):
    monkeypatch.setattr(ios_backend.sys, "platform", "win32")
    device = DeviceInfo(
        serial="UDID-1",
        platform=Platform.IOS,
        model="QA iPhone",
        os_version="iOS 18.5",
    )
    package = PackageInfo(
        bundle_id="com.example.game",
        label="Game",
        version_name="1.2.3",
    )
    backend = ios_backend.IOSBackend()
    monkeypatch.setattr(ios_backend.pymobiledevice, "list_devices", lambda: [device])
    monkeypatch.setattr(
        ios_backend.pymobiledevice,
        "list_packages",
        lambda serial, user_only=True: [package],
    )

    backend.list_devices()
    backend.list_packages("UDID-1")
    monkeypatch.setattr(
        ios_backend.pymobiledevice,
        "list_devices",
        lambda: pytest.fail("device inventory should be cached"),
    )
    monkeypatch.setattr(
        ios_backend.pymobiledevice,
        "list_packages",
        lambda *a, **k: pytest.fail("package inventory should be cached"),
    )

    resolved_device, resolved_package = backend.prepare_capture(
        "UDID-1", PackageInfo(bundle_id="com.example.game")
    )

    assert resolved_device is device
    assert resolved_package.label == "Game"
    assert resolved_package.version_name == "1.2.3"


def test_pymobiledevice3_list_packages_filters_at_cli(monkeypatch):
    calls = []

    def fake_run_json(args, *, udid=None, timeout=60.0):
        calls.append((list(args), udid))
        return {
            "com.example.game": {
                "CFBundleDisplayName": "Example",
                "ApplicationType": "User",
            }
        }

    monkeypatch.setattr(pmd3, "run_json", fake_run_json)

    apps = pmd3.list_packages("UDID-1", user_only=True)

    assert [app.bundle_id for app in apps] == ["com.example.game"]
    assert calls == [(["apps", "list", "--type", "User"], "UDID-1")]


def test_pymobiledevice3_prepare_wireless_enables_wifi_connections(monkeypatch):
    usb = [
        DeviceInfo(
            serial="UDID-1",
            platform=Platform.IOS,
            connection_type="usb",
        )
    ]
    wireless = [
        DeviceInfo(
            serial="UDID-1",
            platform=Platform.IOS,
            connection_type="wireless",
        )
    ]
    device_lists = [usb, wireless]
    calls = []

    def fake_list_devices():
        return device_lists.pop(0)

    def fake_run(args, *, udid=None, timeout=60.0):
        calls.append((list(args), udid))
        return ""

    monkeypatch.setattr(pmd3, "list_devices", fake_list_devices)
    monkeypatch.setattr(pmd3, "run", fake_run)

    result = pmd3.prepare_wireless("UDID-1", timeout=1.0)

    assert result["device"].connection_type == "wireless"
    assert calls == [(["lockdown", "wifi-connections", "--state", "on"], "UDID-1")]


def test_pymobiledevice3_set_wifi_connections_falls_back_to_positional(monkeypatch):
    calls = []

    def fake_run(args, *, udid=None, timeout=60.0):
        calls.append(list(args))
        if "--state" in args:
            raise pmd3.Pymobiledevice3Error(
                "pymobiledevice3 failed (pymobiledevice3 lockdown wifi-connections "
                "--state on): Error: No such option: --state"
            )
        return ""

    monkeypatch.setattr(pmd3, "run", fake_run)

    pmd3._set_wifi_connections("UDID-1", "on")

    assert calls == [
        ["lockdown", "wifi-connections", "--state", "on"],
        ["lockdown", "wifi-connections", "on"],
    ]


def test_pymobiledevice3_set_wifi_connections_does_not_retry_device_error(monkeypatch):
    calls = []

    def fake_run(args, *, udid=None, timeout=60.0):
        calls.append(list(args))
        raise pmd3.Pymobiledevice3Error(
            "pymobiledevice3 failed (pymobiledevice3 lockdown wifi-connections "
            "--state on): device not found"
        )

    monkeypatch.setattr(pmd3, "run", fake_run)

    with pytest.raises(pmd3.Pymobiledevice3Error):
        pmd3._set_wifi_connections("UDID-1", "on")

    # A genuine device failure must not waste a second attempt on the other shape.
    assert calls == [["lockdown", "wifi-connections", "--state", "on"]]


def test_pymobiledevice3_set_wifi_connections_reports_when_all_forms_rejected(monkeypatch):
    calls = []

    def fake_run(args, *, udid=None, timeout=60.0):
        calls.append(list(args))
        # Mimic pymobiledevice3 9.16 rejecting the positional form and a
        # hypothetical build also rejecting --state: every shape is a usage error.
        if "--state" in args:
            raise pmd3.Pymobiledevice3Error("Error: No such option: --state")
        raise pmd3.Pymobiledevice3Error("Got unexpected extra argument(s) (on)")

    monkeypatch.setattr(pmd3, "run", fake_run)

    with pytest.raises(pmd3.Pymobiledevice3Error, match="rejected every known"):
        pmd3._set_wifi_connections("UDID-1", "on")

    # Both known forms were attempted before giving up.
    assert calls == [
        ["lockdown", "wifi-connections", "--state", "on"],
        ["lockdown", "wifi-connections", "on"],
    ]


def test_pymobiledevice3_resolve_pid_uses_bundle_id_command(monkeypatch):
    calls = []

    def fake_run(args, *, udid=None, timeout=60.0):
        calls.append((list(args), udid))
        return "4321\n"

    monkeypatch.setattr(pmd3, "run", fake_run)

    pid = pmd3.resolve_or_launch_pid(
        "UDID-1",
        PackageInfo(bundle_id="com.example.game"),
    )

    assert pid == 4321
    assert calls == [
        (
            [
                "developer",
                "dvt",
                "process-id-for-bundle-id",
                "com.example.game",
            ],
            "UDID-1",
        )
    ]


def test_pymobiledevice3_terminate_pid_uses_dvt_kill_without_launch(monkeypatch):
    calls = []

    def fake_run(args, *, udid=None, timeout=60.0):
        calls.append((list(args), udid, timeout))
        return ""

    monkeypatch.setattr(pmd3, "run", fake_run)
    pmd3.terminate_pid("UDID-1", 4321)

    assert calls == [(["developer", "dvt", "kill", "4321"], "UDID-1", 45.0)]
    with pytest.raises(ValueError):
        pmd3.terminate_pid("UDID-1", 0)


def test_pymobiledevice3_lookup_running_pid_is_read_only(monkeypatch):
    calls = []

    def fake_run(args, *, udid=None, timeout=60.0):
        calls.append((list(args), udid, timeout))
        return "4321\n"

    monkeypatch.setattr(pmd3, "run", fake_run)

    assert pmd3.lookup_running_pid("UDID-1", "com.example.game") == 4321
    assert calls == [
        (
            ["developer", "dvt", "process-id-for-bundle-id", "com.example.game"],
            "UDID-1",
            45.0,
        )
    ]


def test_ios_pymobiledevice_terminate_revalidates_captured_pid(monkeypatch):
    backend = ios_backend.IOSBackend()
    package = PackageInfo(bundle_id="com.example.game")

    class Session:
        pid = 4321

    killed = []
    monkeypatch.setattr(backend, "_use_pymobiledevice", lambda: True)
    monkeypatch.setattr(pmd3, "terminate_pid", lambda serial, pid: killed.append((serial, pid)))

    monkeypatch.setattr(pmd3, "lookup_running_pid", lambda *_args: 9876)
    backend.terminate_app("UDID-1", package, session=Session())
    assert killed == []

    monkeypatch.setattr(pmd3, "lookup_running_pid", lambda *_args: 4321)
    backend.terminate_app("UDID-1", package, session=Session())
    assert killed == [("UDID-1", 4321)]


def test_ios_macos_terminate_looks_up_without_launch_then_kills(monkeypatch):
    backend = ios_backend.IOSBackend()
    calls = []
    monkeypatch.setattr(backend, "_use_pymobiledevice", lambda: False)
    monkeypatch.setattr(backend, "_require_macos", lambda: None)

    def fake_run_json(args, timeout=60.0):
        calls.append(("json", list(args), timeout))
        return {"result": {"processes": [{"bundleIdentifier": "com.example.game", "pid": 123}]}}

    def fake_run(args, timeout=60.0):
        calls.append(("run", list(args), timeout))
        return ""

    monkeypatch.setattr(ios_backend.xcrun, "run_json", fake_run_json)
    monkeypatch.setattr(ios_backend.xcrun, "run", fake_run)
    backend.terminate_app("UDID-1", PackageInfo(bundle_id="com.example.game"))

    assert calls == [
        (
            "json",
            [
                "devicectl", "device", "process", "list", "--device", "UDID-1",
                "--bundle-identifier", "com.example.game",
            ],
            45.0,
        ),
        (
            "run",
            [
                "devicectl", "device", "process", "terminate", "--device", "UDID-1",
                "--pid", "123", "--kill",
            ],
            60.0,
        ),
    ]


def test_ios_macos_terminate_rejects_unmatched_single_pid(monkeypatch):
    backend = ios_backend.IOSBackend()
    monkeypatch.setattr(backend, "_use_pymobiledevice", lambda: False)
    monkeypatch.setattr(backend, "_require_macos", lambda: None)
    monkeypatch.setattr(
        ios_backend.xcrun,
        "run_json",
        lambda *_args, **_kwargs: {
            "result": {
                "processes": [
                    {"bundleIdentifier": "com.example.other", "pid": 123}
                ]
            }
        },
    )
    terminate_calls = []
    monkeypatch.setattr(
        ios_backend.xcrun,
        "run",
        lambda args, **_kwargs: terminate_calls.append(args),
    )

    backend.terminate_app("UDID-1", PackageInfo(bundle_id="com.example.game"))

    assert terminate_calls == []


def test_pymobiledevice3_resolve_pid_raises_when_no_pid(monkeypatch):
    monkeypatch.setattr(pmd3, "run", lambda *args, **kwargs: "0\n")
    monkeypatch.setattr(pmd3, "run_json", lambda *args, **kwargs: [])

    try:
        pmd3.resolve_or_launch_pid(
            "UDID-1",
            PackageInfo(bundle_id="com.example.game"),
        )
    except pmd3.Pymobiledevice3Error as exc:
        assert "could not resolve running pid" in str(exc)
    else:
        raise AssertionError("expected Pymobiledevice3Error")


def test_pymobiledevice3_sample_process_filters_by_pid(monkeypatch):
    calls = []

    def fake_run_json(args, *, udid=None, timeout=60.0):
        calls.append((list(args), udid))
        return [
            {
                "pid": 4321,
                "bundleIdentifier": "com.example.game",
                "cpuUsage": "7.5%",
                "physFootprint": "128 MB",
            }
        ]

    monkeypatch.setattr(pmd3, "run_json", fake_run_json)

    parsed = pmd3.sample_process(
        "UDID-1",
        PackageInfo(bundle_id="com.example.game"),
        pid=4321,
        duration_sec=0,
    )

    assert parsed["cpu"].pid == 4321
    assert parsed["memory"].rss_total_kb == 128 * 1024
    # When the pid is known we pass `--filter pid=<pid>` so pymobiledevice3 only
    # resolves execName for that one process instead of issuing a per-process
    # `execnameForPid:` round-trip for every process in the snapshot (~500 of them,
    # which over a Wi-Fi tunnel blows the 90 s timeout).
    assert calls == [
        (
            [
                "developer",
                "dvt",
                "sysmon",
                "process",
                "single",
                "--filter",
                "pid=4321",
            ],
            "UDID-1",
        )
    ]


def test_pymobiledevice3_sample_process_raises_when_target_missing(monkeypatch):
    calls = []

    def fake_run_json(args, *, udid=None, timeout=60.0):
        calls.append((list(args), udid))
        return [
            {
                "pid": 9999,
                "bundleIdentifier": "com.example.other",
                "cpuUsage": "1.0%",
            }
        ]

    monkeypatch.setattr(pmd3, "run_json", fake_run_json)

    try:
        pmd3.sample_process(
            "UDID-1",
            PackageInfo(bundle_id="com.example.game"),
            pid=4321,
            duration_sec=0,
        )
    except pmd3.Pymobiledevice3Error as exc:
        assert "could not find sysmon process sample" in str(exc)
    else:
        raise AssertionError("expected Pymobiledevice3Error")

    assert calls == [
        (
            [
                "developer",
                "dvt",
                "sysmon",
                "process",
                "single",
                "--filter",
                "pid=4321",
            ],
            "UDID-1",
        )
    ]


def test_sample_sysmon_returns_empty_when_filter_finds_no_match(monkeypatch):
    # The pid-filtered single-shot raises "matching the given filters" when the
    # target process isn't in the snapshot. _sample_sysmon must turn that into an
    # empty result (so sample_process raises its own clear message) WITHOUT paying
    # for a second, full-snapshot fetch.
    calls = []

    def fake_run_json(args, *, udid=None, timeout=60.0):
        calls.append(list(args))
        raise pmd3.Pymobiledevice3Error(
            "pymobiledevice3 failed (...): Failed to find a process matching the given filters"
        )

    monkeypatch.setattr(pmd3, "run_json", fake_run_json)

    assert pmd3._sample_sysmon("UDID-1", timeout=10.0, pid=4321) == []
    # Only the filtered attempt ran; no fallback to the full snapshot.
    assert calls == [["developer", "dvt", "sysmon", "process", "single", "--filter", "pid=4321"]]


def test_sample_sysmon_falls_back_to_full_snapshot_on_usage_error(monkeypatch):
    # An older pymobiledevice3 that doesn't understand `--filter` must not break
    # capture: _sample_sysmon retries with the full (unfiltered) snapshot.
    calls = []

    def fake_run_json(args, *, udid=None, timeout=60.0):
        calls.append(list(args))
        if "--filter" in args:
            raise pmd3.Pymobiledevice3Error("Error: No such option: --filter")
        return [{"pid": 4321, "cpuUsage": "5%"}]

    monkeypatch.setattr(pmd3, "run_json", fake_run_json)

    out = pmd3._sample_sysmon("UDID-1", timeout=10.0, pid=4321)

    assert out == [{"pid": 4321, "cpuUsage": "5%"}]
    assert calls == [
        ["developer", "dvt", "sysmon", "process", "single", "--filter", "pid=4321"],
        ["developer", "dvt", "sysmon", "process", "single"],
    ]


def test_pymobiledevice3_frozen_env_removes_pyinstaller_path(monkeypatch, tmp_path):
    meipass = tmp_path / "_MEI"
    nested = meipass / "dlls"
    tools = tmp_path / "tools"
    nested.mkdir(parents=True)
    tools.mkdir()
    monkeypatch.setattr(pmd3.sys, "frozen", True, raising=False)
    monkeypatch.setattr(pmd3.sys, "_MEIPASS", str(meipass), raising=False)
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join([str(meipass), str(nested), str(tools)]),
    )

    env = pmd3._subprocess_env("UDID-1")

    assert env["PATH"] == str(tools)
    assert env["PYMOBILEDEVICE3_UDID"] == "UDID-1"


def test_pymobiledevice3_env_accepts_home_override(monkeypatch, tmp_path):
    home = tmp_path / "pmd3-home"
    monkeypatch.setenv("MPI_PYMOBILEDEVICE3_HOME", str(home))

    env = pmd3._subprocess_env(None)

    assert env["USERPROFILE"] == str(home)
    assert env["HOME"] == str(home)


def test_pymobiledevice3_env_maps_tunnel_alias(monkeypatch):
    monkeypatch.setenv("MPI_PYMOBILEDEVICE3_TUNNEL", "UDID:12345")
    monkeypatch.delenv("PYMOBILEDEVICE3_TUNNEL", raising=False)

    env = pmd3._subprocess_env(None)

    assert env["PYMOBILEDEVICE3_TUNNEL"] == "UDID:12345"


def test_pymobiledevice3_injects_rsd_for_dvt_positional_commands(monkeypatch):
    monkeypatch.setenv("MPI_PYMOBILEDEVICE3_RSD", "127.0.0.1:12345")

    args = pmd3._inject_rsd_args(
        ["developer", "dvt", "launch", "com.example.game"]
    )

    assert args == [
        "developer",
        "dvt",
        "launch",
        "--rsd",
        "127.0.0.1",
        "12345",
        "com.example.game",
    ]


def test_pymobiledevice3_injects_rsd_for_sysmon_options(monkeypatch):
    monkeypatch.setenv("MPI_PYMOBILEDEVICE3_RSD", "127.0.0.1 12345")

    args = pmd3._inject_rsd_args(
        [
            "developer",
            "dvt",
            "sysmon",
            "process",
            "single",
            "--filter",
            "pid=4321",
        ]
    )

    assert args == [
        "developer",
        "dvt",
        "sysmon",
        "process",
        "single",
        "--rsd",
        "127.0.0.1",
        "12345",
        "--filter",
        "pid=4321",
    ]


def test_pymobiledevice3_discover_rsd_reads_tunneld_payload(monkeypatch):
    payload = {
        "00008150-001608282178401C": [
            {
                "tunnel-address": "fd00::1",
                "tunnel-port": 55555,
                "interface": "utun7",
            }
        ]
    }

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(payload).encode("utf-8")

    monkeypatch.delenv("MPI_PYMOBILEDEVICE3_TUNNELD", raising=False)
    monkeypatch.setattr(
        pmd3.urllib.request, "urlopen", lambda *args, **kwargs: FakeResponse()
    )

    assert pmd3._discover_rsd("00008150-001608282178401C") == ("fd00::1", "55555")
    # No specific device requested: a lone active tunnel resolves unambiguously.
    assert pmd3._discover_rsd(None) == ("fd00::1", "55555")
    # A specific-but-absent UDID must NOT borrow another device's tunnel.
    assert pmd3._discover_rsd("OTHER-UDID") is None


def test_pymobiledevice3_discover_rsd_skips_stale_tunneld_entries(monkeypatch):
    payload = {
        "udid-1": [
            {"interface": "stale"},
            {"tunnel-address": "fd00::2", "tunnel-port": 55556},
        ]
    }

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(payload).encode("utf-8")

    monkeypatch.setattr(
        pmd3.urllib.request, "urlopen", lambda *args, **kwargs: FakeResponse()
    )

    assert pmd3._discover_rsd("UDID-1") == ("fd00::2", "55556")


def test_pymobiledevice3_discover_rsd_matches_casefolded_udid_keys(monkeypatch):
    payload = {
        "00008150-001608282178401C": [
            {"tunnel-address": "fd00::1", "tunnel-port": 55555}
        ],
        "alias": [{"TunnelAddress": "fd00::9", "TunnelPort": 55559}],
    }

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(payload).encode("utf-8")

    monkeypatch.delenv("MPI_PYMOBILEDEVICE3_TUNNELD", raising=False)
    monkeypatch.setattr(
        pmd3.urllib.request, "urlopen", lambda *args, **kwargs: FakeResponse()
    )

    assert pmd3._discover_rsd("alias") == ("fd00::9", "55559")


def test_pymobiledevice3_discover_rsd_parses_rsd_entry_spec(monkeypatch):
    payload = {
        "alias": {
            "rsd": "fd00::7 7777",
        }
    }

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(payload).encode("utf-8")

    monkeypatch.delenv("MPI_PYMOBILEDEVICE3_TUNNELD", raising=False)
    monkeypatch.setattr(
        pmd3.urllib.request, "urlopen", lambda *args, **kwargs: FakeResponse()
    )

    assert pmd3._discover_rsd("alias") == ("fd00::7", "7777")


def test_pymobiledevice3_discover_rsd_returns_none_when_tunneld_down(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(pmd3.urllib.request, "urlopen", boom)

    assert pmd3._discover_rsd("UDID-1") is None


def test_pymobiledevice3_auto_injects_discovered_rsd(monkeypatch):
    monkeypatch.delenv("MPI_PYMOBILEDEVICE3_RSD", raising=False)
    monkeypatch.setattr(pmd3, "_discover_rsd", lambda udid=None: ("fd00::1", "55555"))

    args = pmd3._inject_rsd_args(
        ["developer", "dvt", "launch", "com.example.game"], "UDID-1"
    )

    assert args == [
        "developer",
        "dvt",
        "launch",
        "--rsd",
        "fd00::1",
        "55555",
        "com.example.game",
    ]


def test_pymobiledevice3_auto_injects_discovered_rsd_for_kill(monkeypatch):
    monkeypatch.delenv("MPI_PYMOBILEDEVICE3_RSD", raising=False)
    monkeypatch.setattr(pmd3, "_discover_rsd", lambda udid=None: ("fd00::1", "55555"))

    args = pmd3._inject_rsd_args(
        ["developer", "dvt", "kill", "4321"], "UDID-1"
    )

    assert args == [
        "developer",
        "dvt",
        "kill",
        "--rsd",
        "fd00::1",
        "55555",
        "4321",
    ]


def test_pymobiledevice3_env_rsd_overrides_discovery(monkeypatch):
    monkeypatch.setenv("MPI_PYMOBILEDEVICE3_RSD", "127.0.0.1:12345")
    monkeypatch.setattr(
        pmd3, "_discover_rsd", lambda *a, **k: ("SHOULD", "NOT-BE-USED")
    )

    args = pmd3._inject_rsd_args(
        ["developer", "dvt", "launch", "com.example.game"], "UDID-1"
    )

    assert args[3:6] == ["--rsd", "127.0.0.1", "12345"]


def test_pymobiledevice3_no_rsd_discovery_for_non_dvt_commands(monkeypatch):
    monkeypatch.delenv("MPI_PYMOBILEDEVICE3_RSD", raising=False)
    calls = []

    def fake_discover(*args, **kwargs):
        calls.append(args)
        return ("fd00::1", "55555")

    monkeypatch.setattr(pmd3, "_discover_rsd", fake_discover)

    args = pmd3._inject_rsd_args(["usbmux", "list", "--usb"], "UDID-1")

    assert args == ["usbmux", "list", "--usb"]
    assert calls == []


def test_pymobiledevice3_run_does_not_inherit_stdin(monkeypatch):
    captured = {}

    class FakeProcess:
        returncode = 0

        def communicate(self, timeout=None):
            del timeout
            return "[]", ""

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["stdin"] = kwargs.get("stdin")
        return FakeProcess()

    monkeypatch.setattr(pmd3, "_command", lambda: ["pymobiledevice3"])
    monkeypatch.setattr(pmd3.subprocess, "Popen", fake_popen)

    output = pmd3.run(["usbmux", "list"], udid="UDID-1")

    assert output == "[]"
    assert captured["cmd"] == ["pymobiledevice3", "usbmux", "list"]
    assert captured["stdin"] is pmd3.subprocess.DEVNULL


def test_pymobiledevice3_run_treats_error_output_as_failure(monkeypatch):
    class FakeProcess:
        returncode = 0

        def communicate(self, timeout=None):
            del timeout
            return "\x1b[31mERROR\x1b[0m Device not found: UDID-1", ""

    monkeypatch.setattr(pmd3, "_command", lambda: ["pymobiledevice3"])
    monkeypatch.setattr(pmd3, "_discover_rsd", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        pmd3.subprocess,
        "Popen",
        lambda *args, **kwargs: FakeProcess(),
    )

    try:
        pmd3.run(["developer", "dvt", "process-id-for-bundle-id", "app"])
    except pmd3.Pymobiledevice3Error as exc:
        assert "Device not found" in str(exc)
        assert "require a pymobiledevice3 tunnel" in str(exc)
    else:
        raise AssertionError("expected Pymobiledevice3Error")


def test_pymobiledevice3_resolve_pid_preserves_tunnel_error(monkeypatch):
    def fail_run(*args, **kwargs):
        raise pmd3.Pymobiledevice3Error(
            "pymobiledevice3 failed (developer dvt process-id-for-bundle-id app): "
            "Device not found. DVT metrics on recent iOS versions require a "
            "pymobiledevice3 tunnel."
        )

    monkeypatch.setattr(pmd3, "run", fail_run)

    try:
        pmd3.resolve_or_launch_pid(
            "UDID-1",
            PackageInfo(bundle_id="com.example.game"),
        )
    except pmd3.Pymobiledevice3Error as exc:
        assert "pymobiledevice3 tunnel" in str(exc)
    else:
        raise AssertionError("expected Pymobiledevice3Error")


def test_pymobiledevice3_preflight_reports_available_cli(monkeypatch):
    monkeypatch.setattr(pmd3, "_command", lambda: ["pymobiledevice3"])
    monkeypatch.setattr(pmd3, "run", lambda *args, **kwargs: "9.13.0\n")

    result = pmd3.preflight()

    assert result == {
        "available": True,
        "command": ["pymobiledevice3"],
        "version": "9.13.0",
    }


@pytest.mark.parametrize("version", ["9.12.0", "10.0.0"])
def test_pymobiledevice3_guard_preflight_rejects_unsupported_version(
    monkeypatch, version
):
    monkeypatch.setattr(pmd3, "_command", lambda: ["pymobiledevice3"])
    monkeypatch.setattr(pmd3, "run", lambda *args, **kwargs: f"{version}\n")
    monkeypatch.setattr(pmd3, "_system_python", lambda: "python")

    result = pmd3.guard_preflight()

    assert result["available"] is False
    assert result["version"] == version
    assert result["installable"] is True
    assert "requires pymobiledevice3 >=9.13,<10" in result["error"]


def test_pymobiledevice3_preflight_reports_missing_cli(monkeypatch):
    def boom():
        raise pmd3.Pymobiledevice3Error("pymobiledevice3 executable not found")

    monkeypatch.setattr(pmd3, "_command", boom)
    monkeypatch.setattr(pmd3, "_system_python", lambda: "C:/Python314/python.exe")

    result = pmd3.preflight()

    assert result["available"] is False
    assert result["installable"] is True
    assert "not found" in result["error"]


def test_pymobiledevice3_preflight_tolerates_version_failure(monkeypatch):
    def fail_run(*args, **kwargs):
        raise pmd3.Pymobiledevice3Error("no version subcommand")

    monkeypatch.setattr(pmd3, "_command", lambda: ["pymobiledevice3"])
    monkeypatch.setattr(pmd3, "run", fail_run)

    result = pmd3.preflight()

    assert result["available"] is True
    assert result["version"] is None

    guard_result = pmd3.guard_preflight()
    assert guard_result["available"] is False
    assert guard_result["guard_supported"] is False
    assert "found unknown" in guard_result["error"]


def test_pymobiledevice3_install_cli_requires_interpreter(monkeypatch):
    monkeypatch.setattr(pmd3, "_system_python", lambda: None)

    try:
        pmd3.install_cli()
    except pmd3.Pymobiledevice3Error as exc:
        assert "no Python interpreter" in str(exc)
    else:
        raise AssertionError("expected Pymobiledevice3Error")


def test_pymobiledevice3_install_cli_runs_pip_and_repreflights(monkeypatch):
    captured = {}

    class FakeProcess:
        returncode = 0
        stdout = "Successfully installed pymobiledevice3"
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return FakeProcess()

    monkeypatch.setattr(pmd3, "_system_python", lambda: "python")
    monkeypatch.setattr(pmd3.subprocess, "run", fake_run)
    monkeypatch.setattr(
        pmd3,
        "preflight",
        lambda: {"available": True, "command": ["pymobiledevice3"], "version": "9.13.0"},
    )

    result = pmd3.install_cli()

    assert captured["cmd"] == [
        "python",
        "-m",
        "pip",
        "install",
        "--upgrade",
        "pymobiledevice3>=9.13,<10",
        "zeroconf",
    ]
    assert result["available"] is True


def test_pmd3_stream_module_imports_and_fps_enabled(monkeypatch):
    # Regression: pmd3_stream._fps_enabled() once raised NameError because the
    # module called os.environ without importing os. That crashed the streamer
    # right after it emitted "ready", forcing the slow single-shot fallback
    # (~30 s/sample, no FPS). Importing the module and exercising the helper
    # would have caught it.
    from mpi.ios import pmd3_stream

    monkeypatch.delenv("MPI_IOS_FPS", raising=False)
    assert pmd3_stream._fps_enabled() is True
    monkeypatch.setenv("MPI_IOS_FPS", "0")
    assert pmd3_stream._fps_enabled() is False
    monkeypatch.setenv("MPI_IOS_FPS", "1")
    assert pmd3_stream._fps_enabled() is True


def test_pmd3_stream_scan_fps_extracts_core_animation_fps():
    from mpi.ios import pmd3_stream

    # Graphics events arrive as the raw notification dict...
    assert pmd3_stream._scan_fps({"CoreAnimationFramesPerSecond": 59.7}) == 59.7
    # ...or as a (selector, [dict]) dispatch tuple.
    assert pmd3_stream._scan_fps(("sel", [{"CoreAnimationFramesPerSecond": 60}])) == 60
    # Out-of-range and unrelated values are ignored.
    assert pmd3_stream._scan_fps({"CoreAnimationFramesPerSecond": 99999}) is None
    assert pmd3_stream._scan_fps({"unrelated": 30}) is None


def test_pmd3_stream_scan_gpu_extracts_device_utilization():
    from mpi.ios import pmd3_stream

    # The Graphics instrument reports GPU load as "Device Utilization %"; _norm
    # strips the space and percent sign so the key matches.
    event = {
        "Device Utilization %": 93,
        "Renderer Utilization %": 90,
        "Tiler Utilization %": 88,
        "CoreAnimationFramesPerSecond": 58,
    }
    assert pmd3_stream._scan_gpu(event) == 93
    # Falls back to the renderer/tiler stage figure when the device figure is
    # absent (takes the hotter of the two).
    assert pmd3_stream._scan_gpu({"Renderer Utilization %": 70, "Tiler Utilization %": 80}) == 80
    # Out-of-range and unrelated events yield nothing.
    assert pmd3_stream._scan_gpu({"Device Utilization %": 5000}) is None
    assert pmd3_stream._scan_gpu({"unrelated": 42}) is None


def test_pmd3_stream_battery_fields_maps_diagnostics_payload():
    from mpi.ios import pmd3_stream

    info = {
        "CurrentCapacity": 54,
        "Amperage": -1260,
        "Voltage": 3805,
        "AppleRawBatteryVoltage": 3805,
        "ExternalConnected": False,
        "IsCharging": False,
    }
    fields = pmd3_stream._battery_fields(info)
    assert fields == {
        "level_percent": 54,
        "current_ma": -1260.0,
        "voltage_mv": 3805.0,
        "status": "Discharging",
    }

    # Charging / full states are derived from the diagnostics flags.
    assert pmd3_stream._battery_fields(
        {"CurrentCapacity": 80, "IsCharging": True, "ExternalConnected": True}
    )["status"] == "Charging"
    assert pmd3_stream._battery_fields(
        {"CurrentCapacity": 100, "FullyCharged": True, "ExternalConnected": True}
    )["status"] == "Full"

    # Voltage falls back to AppleRawBatteryVoltage; an all-empty payload yields
    # None so the streamer never emits a blank battery object.
    assert pmd3_stream._battery_fields(
        {"CurrentCapacity": 50, "AppleRawBatteryVoltage": 3700}
    )["voltage_mv"] == 3700.0
    assert pmd3_stream._battery_fields({}) is None
    assert pmd3_stream._battery_fields("nope") is None
    assert pmd3_stream._battery_enabled() is True

    # Live battery temperature: the top-level "Temperature" is centi-C on iOS
    # (3980 -> 39.8C) and is surfaced as temp_c.
    assert pmd3_stream._battery_fields(
        {"CurrentCapacity": 54, "Temperature": 3980}
    )["temp_c"] == 39.8
    # A boot-time/uninitialised 0 (or out-of-range reading) is suppressed so the
    # thermal card never shows a bogus 0C — temp_c is simply absent.
    assert "temp_c" not in pmd3_stream._battery_fields(
        {"CurrentCapacity": 54, "Temperature": 0}
    )
    # VirtualTemperature is the fallback when Temperature is absent.
    assert pmd3_stream._battery_fields(
        {"CurrentCapacity": 54, "VirtualTemperature": 2590}
    )["temp_c"] == 25.9
    # The nested AdapterDetails temp is the CHARGER temperature, not the
    # battery's, so it is ignored — only the top-level key counts.
    assert "temp_c" not in pmd3_stream._battery_fields(
        {"CurrentCapacity": 54, "AdapterDetails": {"Temperature": 3000}}
    )
    # A temperature-only payload still yields a reading (feeds the thermal zone).
    assert pmd3_stream._battery_fields({"Temperature": 3050})["temp_c"] == 30.5


def test_pmd3_stream_system_cpu_total_sums_processes():
    from mpi.ios import pmd3_stream

    # System total = sum of every process's per-core-summed cpuUsage (same unit
    # as the app value); None/negative/zero entries don't add, garbage is skipped.
    snapshot = [
        {"pid": 1, "cpuUsage": 80.0},
        {"pid": 2, "cpuUsage": 40.0},
        {"pid": 3, "cpuUsage": None},
        {"pid": 4, "cpuUsage": -1.0},
        "not-a-dict",
    ]
    assert pmd3_stream._system_cpu_total(snapshot) == 120.0
    # An all-idle (but non-empty) snapshot is a real 0, not "no data".
    assert pmd3_stream._system_cpu_total([{"pid": 1, "cpuUsage": 0.0}]) == 0.0
    # An empty snapshot yields None so an empty window doesn't report 0% busy.
    assert pmd3_stream._system_cpu_total([]) is None


@pytest.mark.parametrize(
    "snapshot",
    [
        [{"pid": 1}],
        [{"pid": 1, "cpuUsage": None}, {"pid": 2, "cpuUsage": "garbage"}],
        [{"pid": 1, "cpuUsage": float("nan")}, {"pid": 2, "cpuUsage": "NaN"}],
        [{"pid": 1, "cpuUsage": float("inf")}, {"pid": 2, "cpuUsage": -1.0}],
        ["not-a-dict"],
    ],
)
def test_pmd3_stream_system_cpu_total_requires_valid_cpu_usage(snapshot):
    from mpi.ios import pmd3_stream

    assert pmd3_stream._system_cpu_total(snapshot) is None


def test_pmd3_stream_system_cpu_total_keeps_valid_values_in_mixed_snapshot():
    from mpi.ios import pmd3_stream

    snapshot = [
        {"pid": 1, "cpuUsage": float("nan")},
        {"pid": 2, "cpuUsage": "invalid"},
        {"pid": 3, "cpuUsage": -1.0},
        {"pid": 4, "cpuUsage": 0.0},
        {"pid": 5, "cpuUsage": 12.5},
        {"pid": 6, "cpuUsage": "7.5%"},
    ]

    assert pmd3_stream._system_cpu_total(snapshot) == 20.0


def test_sysmon_stream_reader_failed_after_process_exit():
    # Regression: a streamer that dies mid-capture (after the first frame set
    # _ready) must report failed() so the session degrades to single-shot
    # instead of emitting empty 0-value snapshots forever.
    reader = pmd3.SysmonStreamReader("UDID-1", 4321)

    class FakeProc:
        def __init__(self, code):
            self._code = code

        def poll(self):
            return self._code

    reader._proc = FakeProc(None)  # still running
    reader._ready.set()  # first frame had arrived
    assert reader.failed() is False

    reader._proc = FakeProc(1)  # exited mid-capture
    assert reader.failed() is True


def test_prepare_wireless_windows_message_points_to_tunnel(monkeypatch):
    usb = [DeviceInfo(serial="UDID-1", platform=Platform.IOS, connection_type="usb")]
    monkeypatch.setattr(pmd3, "list_devices", lambda: list(usb))
    monkeypatch.setattr(pmd3, "_set_wifi_connections", lambda *a, **k: None)
    monkeypatch.setattr(pmd3.sys, "platform", "win32", raising=False)

    with pytest.raises(pmd3.Pymobiledevice3Error) as excinfo:
        pmd3.prepare_wireless("UDID-1", timeout=0.0)

    message = str(excinfo.value)
    # On Windows the device reconnects wirelessly only via the tunnel (after the
    # USB cable is removed), so the guidance must point at the tunnel and tell the
    # user to unplug USB — not the (futile) "stay on the same Wi-Fi" tip.
    assert "tunnel" in message.lower()
    assert "unplug" in message.lower()


def test_list_tunneld_devices_classifies_usb_and_wifi(monkeypatch):
    payload = {
        "USB-UDID": [
            {"tunnel-address": "fd00::1", "tunnel-port": 1, "interface": "usbmux-USB-UDID-USB"}
        ],
        # The Wi-Fi (mobdev2) tunnel is named "usbmux-<udid>-Network" — it ALSO
        # contains "usbmux", so it must be classified by the trailing transport
        # token, not by "usb" appearing anywhere (regression: it was mislabeled
        # USB and hidden from the wireless list).
        "NET-UDID": [
            {"tunnel-address": "fd00::2", "tunnel-port": 2, "interface": "usbmux-NET-UDID-Network"}
        ],
        "WIFI-UDID": [
            {"tunnel-address": "fd00::3", "tunnel-port": 3, "interface": "en0"}
        ],
    }

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(payload).encode("utf-8")

    monkeypatch.delenv("MPI_PYMOBILEDEVICE3_TUNNELD", raising=False)
    monkeypatch.setattr(
        pmd3.urllib.request, "urlopen", lambda *a, **k: FakeResponse()
    )

    devices = {d.serial: d.connection_type for d in pmd3.list_tunneld_devices()}
    # Only a "-USB" tunnel is USB; "-Network" and bare adapter names are wireless.
    assert devices == {"USB-UDID": "usb", "NET-UDID": "wireless", "WIFI-UDID": "wireless"}


def test_list_devices_merges_wifi_only_tunnel_device(monkeypatch):
    # usbmux sees only the USB device; tunneld additionally exposes a Wi-Fi tunnel
    # device that usbmux cannot see on Windows. The merge must surface it once,
    # without duplicating the USB device that appears in both sources.
    def fake_usbmux(filter_arg, connection_type):
        if filter_arg == "--usb":
            return [DeviceInfo(serial="USB-1", platform=Platform.IOS, connection_type="usb")]
        return []

    def boom(*a, **k):  # lockdown info enrichment -> keep device as-is
        raise pmd3.Pymobiledevice3Error("no lockdown over wifi")

    monkeypatch.setattr(pmd3, "_list_usbmux_simple", fake_usbmux)
    monkeypatch.setattr(
        pmd3,
        "list_tunneld_devices",
        lambda: [
            DeviceInfo(serial="USB-1", platform=Platform.IOS, connection_type="usb"),
            DeviceInfo(serial="WIFI-1", platform=Platform.IOS, connection_type="wireless"),
        ],
    )
    monkeypatch.setattr(pmd3, "run_json", boom)

    devices = pmd3.list_devices()
    pairs = {(d.serial, d.connection_type) for d in devices}
    assert ("USB-1", "usb") in pairs
    assert ("WIFI-1", "wireless") in pairs
    assert sum(1 for d in devices if d.serial == "USB-1") == 1


def test_prepare_wireless_survives_transient_list_error(monkeypatch):
    # A transient list_devices() failure mid-poll must not abort prepare_wireless
    # with a raw error; keep polling and succeed when the wireless device appears.
    wireless = DeviceInfo(serial="UDID-1", platform=Platform.IOS, connection_type="wireless")
    seq = [
        [DeviceInfo(serial="UDID-1", platform=Platform.IOS, connection_type="usb")],
        "boom",
        [wireless],
    ]

    def fake_list_devices():
        item = seq.pop(0)
        if item == "boom":
            raise pmd3.Pymobiledevice3Error("transient lockdown hiccup")
        return item

    monkeypatch.setattr(pmd3, "list_devices", fake_list_devices)
    monkeypatch.setattr(pmd3, "_set_wifi_connections", lambda *a, **k: None)

    result = pmd3.prepare_wireless("UDID-1", timeout=5.0)

    assert result["device"].connection_type == "wireless"


def test_pymobiledevice3_install_cli_surfaces_pip_failure(monkeypatch):
    class FakeProcess:
        returncode = 1
        stdout = ""
        stderr = "ERROR: No matching distribution found for pymobiledevice3"

    monkeypatch.setattr(pmd3, "_system_python", lambda: "python")
    monkeypatch.setattr(pmd3.subprocess, "run", lambda *a, **k: FakeProcess())

    try:
        pmd3.install_cli()
    except pmd3.Pymobiledevice3Error as exc:
        assert "No matching distribution" in str(exc)
    else:
        raise AssertionError("expected Pymobiledevice3Error")


# --- Persistent metrics streamer -----------------------------------------


class _FakePopen:
    """Minimal subprocess.Popen stand-in for SysmonStreamReader read loops."""

    def __init__(self, stdout_lines, stderr_lines=()):
        self.stdout = iter(stdout_lines)
        self.stderr = iter(stderr_lines)
        self._poll = None

    def poll(self):
        return self._poll

    def terminate(self):
        self._poll = -15

    def wait(self, timeout=None):
        return self._poll

    def kill(self):
        self._poll = -9


def test_streamer_reader_parses_ndjson_frames():
    reader = pmd3.SysmonStreamReader("UDID-1", 4321)
    reader._proc = _FakePopen(
        [
            '{"event": "ready", "pid": 4321}\n',
            '{"t": 1.0, "pid": 4321, "cpu": 10.0, "mem_bytes": 104857600, "fps": 60}\n',
            "this is not json\n",
            '{"t": 2.0, "pid": 4321, "cpu": 20.0, "mem_bytes": 209715200, "fps": 58}\n',
        ]
    )

    reader._read_stdout()

    assert reader.wait_ready(0) is True
    frames = reader.drain()
    assert [f["cpu"] for f in frames] == [10.0, 20.0]
    # drain clears the buffer
    assert reader.drain() == []
    assert reader.failed() is False


def test_streamer_reader_records_fatal_error():
    reader = pmd3.SysmonStreamReader("UDID-1", 4321)
    reader._proc = _FakePopen(['{"event": "fatal", "error": "no tunnel for device"}\n'])

    reader._read_stdout()

    assert reader.failed() is True
    assert "no tunnel" in (reader.error or "")


def test_streamer_reader_error_joins_stderr_tail():
    # Status breadcrumbs must not abort a live connection attempt. Once the
    # child actually exits, `.error` exposes the joined tail for diagnosis.
    reader = pmd3.SysmonStreamReader("UDID-1", 4321)
    reader._proc = _FakePopen(
        [],
        stderr_lines=[
            "connecting: udid=UDID-1 rsd=auto tunneld=default\n",
            "sysmon: instrument created\n",
            "sysmon: streaming started\n",
        ],
    )

    reader._drain_stderr()

    assert reader.error is None
    reader._proc._poll = 1

    err = reader.error or ""
    assert "sysmon: streaming started" in err
    assert "sysmon: instrument created" in err
    assert "\n" in err  # multiple lines joined, not just the last one


def test_sysmon_loop_aligns_output_frequency_with_interval(monkeypatch):
    # Primary wireless fix: pymobiledevice3 hardcodes Sysmontap's output frequency
    # ("ur") to 1 ms, flooding the device->host link ~1000x/s. Over a low-MTU Wi-Fi
    # tunnel that flood prevents the first frame from ever arriving. _sysmon_loop
    # must override "ur" to match the requested sample interval (~1 Hz).
    import asyncio

    from mpi.ios import pmd3_stream

    sysmontap_mod = pytest.importorskip(
        "pymobiledevice3.services.dvt.instruments.sysmontap"
    )

    captured: dict = {}

    class _FakeSysmontap:
        def __init__(self, interval_ms):
            # mirrors pymobiledevice3: "ur" hardcoded to the 1 ms minimum
            self._config = {"ur": 1, "sampleInterval": interval_ms * 1_000_000}
            captured["instance"] = self

        @classmethod
        async def create(cls, dvt, interval=500):
            return cls(interval)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def iter_processes(self):
            # first snapshot is skipped (uninitialised cpu); second is emitted
            yield [{"pid": 4321, "cpuUsage": 5.0, "physFootprint": 1024}]
            yield [{"pid": 4321, "cpuUsage": 7.0, "physFootprint": 2048}]

    monkeypatch.setattr(sysmontap_mod, "Sysmontap", _FakeSysmontap)
    emitted: list = []
    monkeypatch.setattr(pmd3_stream, "_emit", lambda payload: emitted.append(payload))
    monkeypatch.setattr(pmd3_stream, "_status", lambda *_a, **_k: None)

    state = {"fps": None, "clock": lambda: 1000.0}
    asyncio.run(pmd3_stream._sysmon_loop(object(), 4321, 1000, state))

    # The 1 ms flood is gone: output frequency now matches the 1 s sample interval.
    assert captured["instance"]._config["ur"] == 1000
    # A frame is still produced (the second snapshot, after the skipped first).
    assert emitted and emitted[-1]["pid"] == 4321
    assert emitted[-1].get("cpu") == 7.0


def test_build_streamer_command_includes_pid_tunneld_and_rsd(monkeypatch):
    monkeypatch.setattr(pmd3, "_streamer_python", lambda: "PYEXE")
    monkeypatch.setattr(pmd3, "_tunneld_address", lambda: ("127.0.0.1", 49151))
    monkeypatch.setattr(pmd3, "_rsd_args", lambda serial: ["--rsd", "fd00::1", "55555"])

    cmd = pmd3.build_streamer_command(
        "UDID-1", 4321, bundle_id="com.example.game", interval_ms=1000
    )

    assert cmd[0] == "PYEXE"
    assert cmd[1] == pmd3._streamer_script_path()
    assert cmd[cmd.index("--udid") + 1] == "UDID-1"
    assert cmd[cmd.index("--pid") + 1] == "4321"
    assert cmd[cmd.index("--tunneld") + 1] == "127.0.0.1:49151"
    rsd_at = cmd.index("--rsd")
    assert cmd[rsd_at + 1 : rsd_at + 3] == ["fd00::1", "55555"]
    assert cmd[cmd.index("--bundle-id") + 1] == "com.example.game"


def test_build_streamer_command_raises_without_interpreter(monkeypatch):
    monkeypatch.setattr(pmd3, "_streamer_python", lambda: None)

    with pytest.raises(pmd3.Pymobiledevice3Error):
        pmd3.build_streamer_command("UDID-1", 4321)


class _FakeStreamReader:
    def __init__(
        self, *args, frames=None, ready=True, fail=False, start_error=False, error=None, **kwargs
    ):
        self.frames = list(frames if frames is not None else [])
        self._ready = ready
        self._fail = fail
        self._start_error = start_error
        # Mirrors SysmonStreamReader.error (a plain attr here is enough for
        # getattr(reader, "error", None) in the session). Default None so the
        # existing fallback tests keep degrading to single-shot.
        self.error = error
        self.closed = False

    def start(self):
        if self._start_error:
            raise pmd3.Pymobiledevice3Error("streamer unavailable")

    def wait_ready(self, timeout):
        return self._ready

    def failed(self):
        return self._fail

    def drain(self):
        out = self.frames
        self.frames = []
        return out

    def close(self):
        self.closed = True


def test_ios_stream_session_aggregates_per_second_frames(monkeypatch):
    monkeypatch.setattr(ios_backend.time, "time", lambda: 2.0)
    frames = [
        {"t": 1.0, "pid": 1, "cpu": 10.0, "mem_bytes": 100 * 1024, "fps": 60},
        {"t": 2.0, "pid": 1, "cpu": 20.0, "mem_bytes": 200 * 1024, "fps": 58},
    ]
    monkeypatch.setattr(
        ios_backend.pymobiledevice,
        "SysmonStreamReader",
        lambda *a, **k: _FakeStreamReader(frames=frames),
    )
    monkeypatch.setattr(ios_backend.time, "sleep", lambda _s: None)

    session = ios_backend.IOSPymobiledeviceStreamingCaptureSession(
        "UDID-1",
        DeviceInfo(serial="UDID-1", platform=Platform.IOS),
        PackageInfo(bundle_id="com.example.game"),
        pid=1,
    )
    snap = session.sample(1.0)

    assert snap.cpu.cpu_percent == 15.0  # mean of 10 and 20
    assert snap.memory.rss_total_kb == 200  # last frame's mem_bytes
    assert snap.frames.fps_estimate == 59.0  # mean of 60 and 58
    assert snap.cpu.pid == 1


def test_ios_stream_session_populates_gpu_and_battery(monkeypatch):
    monkeypatch.setattr(ios_backend.time, "time", lambda: 2.0)
    frames = [
        {
            "t": 1.0,
            "pid": 1,
            "cpu": 10.0,
            "mem_bytes": 100 * 1024,
            "fps": 58,
            "gpu": 92.0,
            "battery": {
                "level_percent": 54,
                "current_ma": -1260.0,
                "voltage_mv": 3805.0,
                "status": "Discharging",
                "temp_c": 30.5,
            },
        },
        {"t": 2.0, "pid": 1, "cpu": 20.0, "mem_bytes": 200 * 1024, "fps": 58, "gpu": 94.0},
    ]
    monkeypatch.setattr(
        ios_backend.pymobiledevice,
        "SysmonStreamReader",
        lambda *a, **k: _FakeStreamReader(frames=frames),
    )
    monkeypatch.setattr(ios_backend.time, "sleep", lambda _s: None)

    session = ios_backend.IOSPymobiledeviceStreamingCaptureSession(
        "UDID-1",
        DeviceInfo(serial="UDID-1", platform=Platform.IOS),
        PackageInfo(bundle_id="com.example.game"),
        pid=1,
    )
    snap = session.sample(1.0)

    # GPU busy% is the mean of the window's Device Utilization readings.
    assert snap.gpu.busy_percent == 93.0
    # Battery (polled at ~5 s, so only the first frame carried it) is surfaced.
    assert snap.battery.level_percent == 54
    assert snap.battery.current_ma == -1260.0
    assert snap.battery.voltage_mv == 3805.0
    assert snap.battery.status == "Discharging"
    # Battery temperature rides along and becomes the "battery" thermal zone,
    # which feeds the Temp card (primary series thermal:battery).
    assert snap.thermal.zones_c["battery"] == 30.5


def test_ios_stream_session_normalizes_cpu(monkeypatch):
    monkeypatch.setattr(ios_backend.time, "time", lambda: 2.0)
    # The streamer emits raw app CPU (cpu), the per-core-summed system total
    # (cpu_total), and the core count (cores). The session keeps cpu_percent raw
    # (can exceed 100 across cores) and derives total_percent + the core/thread-
    # normalized cpu_norm by dividing by the core count.
    frames = [
        {"t": 1.0, "pid": 1, "cpu": 80.0, "cpu_total": 400.0, "cores": 8, "mem_bytes": 100 * 1024},
        {"t": 2.0, "pid": 1, "cpu": 160.0, "cpu_total": 480.0, "cores": 8, "mem_bytes": 100 * 1024},
    ]
    monkeypatch.setattr(
        ios_backend.pymobiledevice,
        "SysmonStreamReader",
        lambda *a, **k: _FakeStreamReader(frames=frames),
    )
    monkeypatch.setattr(ios_backend.time, "sleep", lambda _s: None)

    session = ios_backend.IOSPymobiledeviceStreamingCaptureSession(
        "UDID-1",
        DeviceInfo(serial="UDID-1", platform=Platform.IOS),
        PackageInfo(bundle_id="com.example.game"),
        pid=1,
    )
    snap = session.sample(1.0)

    assert snap.cpu.cpu_percent == 120.0  # raw app % (mean of 80, 160), >100 across cores
    assert snap.cpu.total_percent == 55.0  # mean(400, 480) / 8 cores
    assert snap.cpu.cpu_norm == 15.0  # 120 app% / 8 cores

    # With no core count (hardware_information unavailable) the raw app% still
    # flows, but the normalized/total values stay unset rather than wrong.
    reader2 = _FakeStreamReader(frames=[{"t": 3.0, "pid": 1, "cpu": 90.0, "cpu_total": 400.0}])
    monkeypatch.setattr(
        ios_backend.pymobiledevice, "SysmonStreamReader", lambda *a, **k: reader2
    )
    session2 = ios_backend.IOSPymobiledeviceStreamingCaptureSession(
        "UDID-1",
        DeviceInfo(serial="UDID-1", platform=Platform.IOS),
        PackageInfo(bundle_id="com.example.game"),
        pid=1,
    )
    snap2 = session2.sample(1.0)
    assert snap2.cpu.cpu_percent == 90.0
    assert snap2.cpu.total_percent is None
    assert snap2.cpu.cpu_norm is None


def test_ios_stream_session_holds_last_battery_between_polls(monkeypatch):
    monkeypatch.setattr(ios_backend.time, "time", lambda: 2.0)
    # Battery polls at ~5 s while sampling runs at 1 s, so most windows drain no
    # battery frame. The session must hold the last reading so the card stays
    # populated instead of flickering blank every second.
    reader = _FakeStreamReader(frames=[])
    monkeypatch.setattr(
        ios_backend.pymobiledevice, "SysmonStreamReader", lambda *a, **k: reader
    )
    monkeypatch.setattr(ios_backend.time, "sleep", lambda _s: None)

    session = ios_backend.IOSPymobiledeviceStreamingCaptureSession(
        "UDID-1",
        DeviceInfo(serial="UDID-1", platform=Platform.IOS),
        PackageInfo(bundle_id="com.example.game"),
        pid=1,
    )

    reader.frames = [
        {"t": 1.0, "pid": 1, "cpu": 10.0, "gpu": 90.0, "battery": {"level_percent": 54}}
    ]
    first = session.sample(1.0)
    assert first.battery.level_percent == 54

    # Next window has no battery frame — the last reading is held, not dropped.
    reader.frames = [{"t": 2.0, "pid": 1, "cpu": 12.0}]
    second = session.sample(1.0)
    assert second.battery.level_percent == 54


def test_ios_stream_session_falls_back_when_streamer_cannot_start(monkeypatch):
    monkeypatch.setattr(
        ios_backend.pymobiledevice,
        "SysmonStreamReader",
        lambda *a, **k: _FakeStreamReader(start_error=True),
    )
    sentinel = object()
    captured = {}

    def fake_legacy(serial, device, package, pid, duration_sec):
        captured["call"] = (serial, pid, duration_sec)
        return sentinel

    monkeypatch.setattr(ios_backend, "_capture_pymobiledevice_sample", fake_legacy)

    session = ios_backend.IOSPymobiledeviceStreamingCaptureSession(
        "UDID-1",
        DeviceInfo(serial="UDID-1", platform=Platform.IOS),
        PackageInfo(bundle_id="com.example.game"),
        pid=7,
    )
    out = session.sample(1.0)

    assert out is sentinel
    assert captured["call"] == ("UDID-1", 7, 1.0)


def test_ios_stream_session_falls_back_when_connection_never_ready(monkeypatch):
    monkeypatch.setattr(
        ios_backend.pymobiledevice,
        "SysmonStreamReader",
        lambda *a, **k: _FakeStreamReader(ready=False),
    )
    sentinel = object()
    monkeypatch.setattr(
        ios_backend,
        "_capture_pymobiledevice_sample",
        lambda *a, **k: sentinel,
    )

    session = ios_backend.IOSPymobiledeviceStreamingCaptureSession(
        "UDID-1",
        DeviceInfo(serial="UDID-1", platform=Platform.IOS),
        PackageInfo(bundle_id="com.example.game"),
        pid=7,
    )

    assert session.sample(1.0) is sentinel


def test_ios_stream_session_surfaces_streamer_error_instead_of_redundant_single_shot(
    monkeypatch,
):
    # When the streamer reports a concrete failure reason (fatal/error event or its
    # stderr tail), the first sample must surface THAT error rather than silently
    # re-running the identical single-shot, which over a wireless tunnel floods and
    # times out the same way (the old ~90 s + ~90 s opaque stall).
    monkeypatch.setattr(
        ios_backend.pymobiledevice,
        "SysmonStreamReader",
        lambda *a, **k: _FakeStreamReader(
            ready=False, fail=True, error="boom: no tunnel for device"
        ),
    )

    def must_not_run(*_a, **_k):
        raise AssertionError(
            "single-shot must not run when the streamer reported a concrete reason"
        )

    monkeypatch.setattr(ios_backend, "_capture_pymobiledevice_sample", must_not_run)

    session = ios_backend.IOSPymobiledeviceStreamingCaptureSession(
        "UDID-1",
        DeviceInfo(serial="UDID-1", platform=Platform.IOS),
        PackageInfo(bundle_id="com.example.game"),
        pid=7,
    )

    with pytest.raises(pmd3.Pymobiledevice3Error, match="boom: no tunnel for device"):
        session.sample(1.0)


def test_ios_stream_session_restarts_streamer_after_mid_capture_death(monkeypatch):
    # Sample 1 streams fine; then the streamer dies. The session must spawn a fresh
    # streamer and resume 1 Hz frames instead of degrading the rest of the capture
    # to the slow single-shot path.
    first = _FakeStreamReader(frames=[{"cpu": 10.0, "mem_bytes": 100 * 1024, "fps": 60}])
    second = _FakeStreamReader(frames=[{"cpu": 20.0, "mem_bytes": 200 * 1024, "fps": 58}])
    created: list = []

    def factory(*_a, **_k):
        reader = (first, second)[min(len(created), 1)]
        created.append(reader)
        return reader

    monkeypatch.setattr(ios_backend.pymobiledevice, "SysmonStreamReader", factory)
    monkeypatch.setattr(ios_backend.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        ios_backend,
        "_capture_pymobiledevice_sample",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("must restart the streamer, not degrade to single-shot")
        ),
    )

    session = ios_backend.IOSPymobiledeviceStreamingCaptureSession(
        "UDID-1",
        DeviceInfo(serial="UDID-1", platform=Platform.IOS),
        PackageInfo(bundle_id="com.example.game"),
        pid=1,
    )

    s1 = session.sample(1.0)
    assert s1.cpu.cpu_percent == 10.0
    assert len(created) == 1

    first._fail = True  # streamer dies mid-capture
    s2 = session.sample(1.0)

    assert len(created) == 2  # a fresh streamer was spawned
    assert s2.cpu.cpu_percent == 20.0  # resumed streaming from the new reader
    assert first.closed is True  # the dead streamer was cleaned up


def test_ios_stream_session_close_during_connect_returns_without_legacy(monkeypatch):
    reader = _FakeStreamReader(ready=False)  # connection never completes
    monkeypatch.setattr(
        ios_backend.pymobiledevice, "SysmonStreamReader", lambda *a, **k: reader
    )

    def boom(*_a, **_k):
        raise AssertionError("legacy single-shot must not run while closing")

    monkeypatch.setattr(ios_backend, "_capture_pymobiledevice_sample", boom)

    session = ios_backend.IOSPymobiledeviceStreamingCaptureSession(
        "UDID-1",
        DeviceInfo(serial="UDID-1", platform=Platform.IOS),
        PackageInfo(bundle_id="com.example.game"),
        pid=1,
    )
    session.close()
    snap = session.sample(1.0)

    assert reader.closed is True
    assert snap.cpu.cpu_percent is None
    assert snap.frames.fps_estimate is None


def test_ios_stream_session_close_releases_reader(monkeypatch):
    reader = _FakeStreamReader(frames=[])
    monkeypatch.setattr(
        ios_backend.pymobiledevice, "SysmonStreamReader", lambda *a, **k: reader
    )

    session = ios_backend.IOSPymobiledeviceStreamingCaptureSession(
        "UDID-1",
        DeviceInfo(serial="UDID-1", platform=Platform.IOS),
        PackageInfo(bundle_id="com.example.game"),
        pid=1,
    )
    session.close()

    assert reader.closed is True

