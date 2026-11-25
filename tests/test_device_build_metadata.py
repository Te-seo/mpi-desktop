"""Firmware identity remains intact through live, CSV replay and analysis."""
from dataclasses import asdict
from mpi import api, report, analysis
from mpi.android import backend as android
from mpi.ios import parsers as ios
from mpi.schema import DeviceInfo, Platform, PackageInfo, MetricsSnapshot


def test_android_full_build_identifiers_are_separate_from_short_sw_version(monkeypatch):
    props = {
        "ro.product.model": "Test Phone", "ro.build.version.release": "16",
        "ro.build.version.sdk": "36", "ro.build.display.id": "BP2A.250605.031.FULL1234",
        "ro.build.version.incremental": "12345678", "ro.build.id": "BP2A.250605.031",
    }
    monkeypatch.setattr(android.adb, "run", lambda *_a, **_k: "List of devices attached\nTEST device model:Test\n")
    monkeypatch.setattr(android.adb, "shell", lambda command, **_k:
        "\n".join(f"[{k}]: [{v}]" for k, v in props.items()) if command == "getprop" else "")
    [device] = android.AndroidBackend().list_devices()
    assert device.build_version == "12345678"
    assert device.build_name == "BP2A.250605.031.FULL1234"
    assert device.sw_ver == "1234"
    assert device.os_version == "Android 16 (SDK 36)"


def test_ios_build_identity_never_replaces_market_os_version():
    device = ios.enrich_pymobiledevice3_device(DeviceInfo("TEST", Platform.IOS), {
        "ProductVersion": "26.0.1", "ProductBuildVersion": "23A355", "BuildTrain": "GoldenGate",
    })
    assert device.os_version == "iOS 26.0.1"
    assert device.build_version == "23A355"
    assert device.build_name == "GoldenGate"
    missing = ios.enrich_pymobiledevice3_device(DeviceInfo("OTHER", Platform.IOS), {"ProductVersion": "26.0.1"})
    assert missing.build_version == missing.build_name == ""


def test_ios_devicectl_and_usbmux_build_versions():
    [device] = ios.parse_devicectl_devices({"result": {"devices": [{"identifier": "TEST", "deviceProperties": {
        "osVersion": "26.0.1", "BuildVersion": "23A355", "osVersionExtra": "GoldenGate",
    }}]}})
    assert device.build_version == "23A355"
    assert device.build_name == "GoldenGate"
    [device] = ios.parse_pymobiledevice3_devices([{"Properties": {
        "SerialNumber": "TEST", "ProductVersion": "26.0.1", "BuildVersion": "23A355",
    }}])
    assert device.build_version == "23A355"


def test_build_identity_survives_csv_replay_analysis_and_json_roundtrip(tmp_path):
    device = DeviceInfo("SERIAL", Platform.ANDROID, os_version="Android 16", build_version="12345678", build_name="BP2A.FULL1234")
    snapshot = MetricsSnapshot(device, PackageInfo("com.example.game"), "2026-10-21T01:00:00+00:00", 1.0)
    restored = api._snapshot_from_dict(asdict(snapshot))
    assert restored.device.build_version == "12345678"
    assert restored.device.build_name == "BP2A.FULL1234"
    path = tmp_path / "build.csv"
    report.generate_csv_report([snapshot], str(path))
    [replayed] = api._parse_live_csv(str(path))
    assert replayed.device.build_version == "12345678"
    assert replayed.device.build_name == "BP2A.FULL1234"
    result = analysis.analyze_session(str(path))
    assert result.device_build_version == "12345678"
    assert result.device_build_name == "BP2A.FULL1234"
    payload = report.build_endpoint_payload([snapshot])
    assert payload["metadata"]["device"]["build_name"] == "BP2A.FULL1234"
    assert payload["metadata"]["device"]["build_version"] == "12345678"


def test_legacy_snapshot_missing_build_fields_remains_readable():
    snapshot = api._snapshot_from_dict({"device": {"serial": "OLD", "platform": "android"}})
    assert snapshot.device.build_version == snapshot.device.build_name == ""
