from __future__ import annotations

import pytest

from mpi.ios import pmd3
from mpi.ios.parsers import enrich_pymobiledevice3_device, parse_devicectl_devices
from mpi.schema import DeviceInfo, Platform


def device():
    return DeviceInfo(serial="fixture-udid", platform=Platform.IOS)


@pytest.mark.parametrize("number,name", [
    ("A3473", "iPhone 18 Pro Max"),
    ("A3717", "iPhone 18 Pro Max"),
    ("A3293", "iPhone 16 Pro"),
    ("A3360", "iPad Pro 13-inch (M5)"),
])
def test_model_number_resolves_without_product_type(number, name):
    found = enrich_pymobiledevice3_device(device(), {"RegulatoryModelNumber": number})
    assert found.model == name
    assert found.model_number == number
    assert found.model_identifier == ""


def test_product_type_resolves_name_without_inventing_regional_number():
    found = enrich_pymobiledevice3_device(device(), {"ProductType": "iPhone19,3"})
    assert found.model == "iPhone 18 Pro Max"
    assert found.model_identifier == "iPhone19,3"
    assert found.model_number == ""


def test_retail_part_number_is_not_a_regulatory_model_number():
    found = enrich_pymobiledevice3_device(device(), {
        "ProductType": "iPhone99,1", "ModelNumber": "MQ9U3KH/A",
    })
    assert found.model == "iPhone99,1"
    assert found.model_identifier == "iPhone99,1"
    assert found.model_number == ""


def test_observed_future_marketing_name_wins_and_sparse_refresh_preserves_identity():
    found = enrich_pymobiledevice3_device(device(), {
        "ProductType": "iPhone99,1", "RegulatoryModelNumber": " a9999 ",
        "MobileGestalt": {"MarketingProductName": "Future iPhone"},
        "DeviceName": "Tester phone",
    })
    enrich_pymobiledevice3_device(found, {"ProductVersion": "27.1"})
    assert (found.model, found.model_identifier, found.model_number) == (
        "Future iPhone", "iPhone99,1", "A9999",
    )
    assert found.display_name == "Tester phone"


def test_mac_devicectl_retains_model_name_identifier_and_number():
    [found] = parse_devicectl_devices({"devices": [{
        "identifier": "fixture-udid", "deviceProperties": {"name": "Lab iPad"},
        "hardwareProperties": {"productType": "iPad17,3", "modelNumber": "A3360"},
    }]})
    assert found.model == "iPad Pro 13-inch (M5)"
    assert found.model_number == "A3360"
    assert found.model_identifier == "iPad17,3"
    assert found.display_name == "Lab iPad"


@pytest.mark.parametrize("diagnostics_fail", [False, True])
def test_discovery_queries_only_two_identity_keys_once_and_keeps_device_on_failure(monkeypatch, diagnostics_fail):
    pmd3._clear_device_metadata_cache()
    calls = []
    monkeypatch.setattr(pmd3, "_list_usbmux_simple", lambda flag, connection: [device()] if connection == "usb" else [])
    monkeypatch.setattr(pmd3, "list_tunneld_devices", lambda: [])

    def query(args, *, udid, timeout):
        calls.append((args, timeout))
        if args == ["lockdown", "info"]:
            return {"ProductType": "iPhone19,3", "ModelNumber": "MQ9U3KH/A"}
        assert args == ["diagnostics", "mg", "--keys", "MarketingProductName", "--keys", "RegulatoryModelNumber"]
        assert timeout == 5.0
        if diagnostics_fail:
            raise pmd3.Pymobiledevice3Error("locked")
        return {"MobileGestalt": {"MarketingProductName": "iPhone 18 Pro Max", "RegulatoryModelNumber": "A3473"}}

    monkeypatch.setattr(pmd3, "run_json", query)
    try:
        [first] = pmd3.list_devices()
        [second] = pmd3.list_devices()
        assert first.model == second.model == "iPhone 18 Pro Max"
        assert first.model_number == second.model_number == ("" if diagnostics_fail else "A3473")
        assert len(calls) == 2
        if diagnostics_fail:
            diagnostics_fail = False
            pmd3._IDENTITY_RETRY_AT["fixture-udid"] = 0.0
            [recovered] = pmd3.list_devices()
            assert recovered.model_number == "A3473"
            assert len(calls) == 4
            assert "fixture-udid" not in pmd3._IDENTITY_RETRY_AT
    finally:
        pmd3._clear_device_metadata_cache()
