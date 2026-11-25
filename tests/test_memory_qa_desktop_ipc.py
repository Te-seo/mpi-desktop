from decimal import Decimal

from mpi import api, memory_qa


class FakeAdb:
    created = []

    def __init__(self, executable, serial):
        self.created.append((executable, serial))

    def get_state(self):
        return "device"

    def mem_total(self):
        return 4_800 * 1024

    def sdk_int(self):
        return 35

    def processes(self, package):
        assert package == "com.example.qa"
        return {31: package, 64: package + ":service"}

    def start_ticks(self, pid):
        return 10000 + pid

    def oom_score_adj(self, pid):
        return 200 if pid == 64 else 0

    def status(self, pid):
        suffix = "VmSwap:\t10 kB\n" if pid == 31 else ""
        return f"Pid:\t{pid}\nRssAnon:\t100 kB\n" + suffix

    def bitmap_dump(self, pid):
        assert pid == 31
        return ("** MEMINFO in pid 31 [com.example.qa] **\n\nNative Allocations\n"
                "   Count Total(kB)\n"
                "   Bitmap (malloced): 1 20\n"
                "   Bitmap (nonmalloced): 2 15\n")


def _sample(**extra):
    return api._dispatch({"id": "test", "command": "play_memory_qa_sample", "payload": {
        "serial": "authorized-phone", "package": "com.example.qa", "category": "game",
        "state": "auto", "sample_index": 0, **extra,
    }})


def test_memory_qa_dispatch_preserves_process_provenance_and_missing_metrics(monkeypatch):
    monkeypatch.setattr(memory_qa, "Adb", FakeAdb)
    monkeypatch.setattr(api, "_physical_device_key", lambda *args: "android:phone")
    response = _sample()
    assert response["ok"]
    assert response["result"]["bitmap_rows"] == []
    main, service = response["result"]["rows"]
    assert FakeAdb.created[-1] == ("adb", "authorized-phone")
    assert (main["state"], main["state_source"], main["start_ticks"]) == ("foreground", "measured_oom_score_adj", 10031)
    assert (main["anon_rss_plus_swap_kib"], main["bitmap_kib"]) == (110, None)
    assert service["anon_rss_plus_swap_kib"] is None
    summary = api._dispatch({"command": "play_memory_qa_summary", "payload": {"rows": [main, service]}})
    assert summary["ok"]
    main_result, service_result = summary["result"]["groups"]
    assert (main_result["ram_tier"], main_result["local_p90_kib"]) == ("6_gb", 110)
    assert main_result["form_factor"] == "unknown" and main_result["threshold_local_kib"] is None
    assert service_result["comparison"] == "not_compared"
    assert "android:phone" not in api._reserved_serials


def test_local_reference_requires_explicit_phone_or_tablet(monkeypatch):
    monkeypatch.setattr(memory_qa, "Adb", FakeAdb)
    monkeypatch.setattr(api, "_physical_device_key", lambda *args: "android:phone")
    rows = [row for factor in ("phone_tablet", "other")
            for row in _sample(form_factor=factor)["result"]["rows"] if row["pid"] == 31]
    groups = api._dispatch({"command": "play_memory_qa_summary", "payload": {"rows": rows}})["result"]["groups"]
    qualified, unqualified = groups
    assert qualified["local_p90_kib"] == unqualified["local_p90_kib"] == 110
    assert Decimal(qualified["threshold_local_kib"]) == 2_883_584
    assert unqualified["threshold_local_kib"] is None


def test_memory_qa_dispatch_refuses_busy_trace_or_untrusted_package(monkeypatch):
    monkeypatch.setattr(api, "_physical_device_key", lambda *args: "android:phone")
    monkeypatch.setattr(api, "_device_busy_locked", lambda key: True)
    monkeypatch.setattr(memory_qa, "Adb", lambda *_: (_ for _ in ()).throw(AssertionError("unwanted ADB")))
    assert "device already sampling or tracing" in _sample()["error"]
    assert "select one exact Android package" in _sample(package="com.example.qa; id")["error"]


def test_memory_qa_dispatch_releases_reservation_after_disconnect(monkeypatch):
    class UnauthorizedAdb(FakeAdb):
        def get_state(self):
            return "offline"

    monkeypatch.setattr(memory_qa, "Adb", UnauthorizedAdb)
    monkeypatch.setattr(api, "_physical_device_key", lambda *args: "android:phone")
    assert "select one authorized ADB device" in _sample()["error"]
    assert "android:phone" not in api._reserved_serials


def test_exact_ipv6_adb_serial_is_supported_but_not_flags_or_shell_text(monkeypatch):
    monkeypatch.setattr(memory_qa, "Adb", FakeAdb)
    monkeypatch.setattr(api, "_physical_device_key", lambda *args: "android:phone")
    serial = "[fe80::abcd%12]:5555"
    assert memory_qa.valid_serial(serial)
    assert _sample(serial=serial)["ok"]
    assert FakeAdb.created[-1] == ("adb", serial)
    for bad in ("-d", "phone 123", "phone;id", "phone|id", "phone&exit", "phone\n123", ""):
        assert not memory_qa.valid_serial(bad)
        assert "select one authorized ADB device" in _sample(serial=bad)["error"]


def test_memory_qa_dispatch_opt_in_bitmap_is_separate_and_bounded(monkeypatch):
    monkeypatch.setattr(memory_qa, "Adb", FakeAdb)
    monkeypatch.setattr(api, "_physical_device_key", lambda *args: "android:phone")
    response = _sample(bitmap=True)
    assert response["ok"]
    assert all(row["bitmap_kib"] is None for row in response["result"]["rows"])
    bitmap, capped = response["result"]["bitmap_rows"]
    assert bitmap["bitmap_kib"] == 35
    assert bitmap["pid"] == 31 and bitmap["bitmap_status"] == "ok"
    assert "state" not in bitmap
    assert capped["bitmap_status"] == "process_listing_capped"
    assert capped["omitted_processes"] == 1
    assert not _sample(bitmap="true")["ok"]
