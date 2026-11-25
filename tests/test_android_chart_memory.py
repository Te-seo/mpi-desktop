from dataclasses import replace
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from mpi import api
from mpi.analysis import analyze_session
from mpi.android import adb
from mpi.android.backend import AndroidBackend
from mpi.android.chart_memory import SOURCE, form_factor, package_category, parse_replay_memory, proc_command, read_capture_proc
from mpi.report import build_endpoint_payload, generate_csv_report
from mpi.schema import DeviceInfo, MemStats, MetricsSnapshot, PackageInfo, Platform


def proc_payload(*, package="com.example.qa", tail_package=None, swap="VmSwap:\t0 kB", after="10", oom_after="0"):
    def stat(start):
        return "99 (name with spaces) R " + "0 " * 18 + start + " 0"
    return "\n_MPI_MEM_BOUNDARY_\n".join([
        stat("10"), package, "0", f"RssAnon:\t2048 kB\n{swap}", oom_after, tail_package or package, stat(after), ""
    ])


def test_capture_checks_exact_package_pid_lifetime_and_preserves_real_zero():
    assert read_capture_proc(proc_payload(), 99, "com.example.qa") == (2048, 0, "foreground", 10)
    assert read_capture_proc(proc_payload(package="com.example.qa:remote"), 99, "com.example.qa") == (2048, 0, "foreground", 10)
    assert read_capture_proc(proc_payload(package="com.example.qab"), 99, "com.example.qa") == (None, None, None, None)
    assert read_capture_proc(proc_payload(after="11"), 99, "com.example.qa") == (None, None, None, None)
    assert read_capture_proc(proc_payload(tail_package="com.example.qa:remote"), 99, "com.example.qa") == (None, None, None, None)


@pytest.mark.parametrize("newline", ["\r\n", "\n"])
def test_adb_text_preserves_chart_memory_and_context_across_line_endings(monkeypatch, newline):
    package = "com.example.qa"
    proc = proc_payload(package=package)
    context = (f"Packages:\n  Package [{package}] (abc):\n    category=0\n"
               "\n_MPI_MEM_CONTEXT_\nfeature:android.hardware.telephony\n")

    def fake_run(args, **kwargs):
        output = context if "dumpsys package" in args[-1] else proc
        return CompletedProcess(args, 0, output.replace("\n", newline).encode(), b"")

    monkeypatch.setattr(adb.subprocess, "run", fake_run)
    raw = adb.shell(proc_command(99), serial="fixture")
    assert read_capture_proc(raw, 99, package) == (2048, 0, "foreground", 10)
    assert AndroidBackend()._capture_memory_context("fixture", package, "unknown") == (
        ("game", "android_package_category_hint"), "phone_tablet",
    )


def test_unreadable_swap_is_missing_and_state_requires_stable_scores():
    assert read_capture_proc(proc_payload(swap="", oom_after="15"), 99, "com.example.qa") == (2048, None, None, 10)


def test_category_hint_requires_exact_dump_record_and_explicit_metadata():
    dump = "Packages:\n  Package [com.example.other] (aaa):\n    pkgFlags=[ IS_GAME ]\n  Package [com.example.qa] (bbb):\n"
    assert package_category(dump, "com.example.qa") == (None, None)
    assert package_category(dump + "    pkgFlags=[ IS_GAME ]\n", "com.example.qa") == ("game", "android_package_is_game_hint")
    assert package_category(dump + "    category=6\n", "com.example.qa") == ("app", "android_package_category_hint")
    assert package_category(dump + "    category=6\n    pkgFlags=[ IS_GAME ]\n", "com.example.qa") == (None, None)
    assert form_factor("tablet,watch") == "other"
    assert form_factor("nosdcard") == "unknown"
    assert form_factor("nosdcard,tablet") == "phone_tablet"


def test_capture_csv_replay_retains_provenance_and_missing_rows(tmp_path: Path):
    device = DeviceInfo(serial="fixture", platform=Platform.ANDROID)
    package = PackageInfo(bundle_id="com.example.qa")
    snapshots = [
        MetricsSnapshot(device=device, package=package, captured_at=f"2026-11-21T10:00:0{i}Z",
                        duration_sec=1, elapsed_sec=i, memory=m)
        for i, m in enumerate([
            MemStats(pss_total_kb=77, rss_total_kb=88),
            MemStats(memory_source=SOURCE, anon_rss_kib=2048, swap_kib=0,
                     anon_rss_plus_swap_kib=2048, play_memory_state="foreground",
                     play_memory_state_source="measured_oom_score_adj",
                     play_memory_ram_kib=7*1024**2, play_memory_pid=99, play_memory_start_ticks=10),
            MemStats(memory_source=SOURCE, rss_total_kb=3000),
        ])
    ]
    path = tmp_path / "report.csv"
    generate_csv_report(snapshots, str(path))
    assert [s.memory for s in api._parse_live_csv(str(path))] == [s.memory for s in snapshots]
    analysis = analyze_session(str(path))
    assert analysis.memory_source == SOURCE
    assert analysis.timeseries["anon_rss_plus_swap_mib"] == [None, 2.0, None]
    assert analysis.timeseries["app_memory_mb"] == [77/1024, None, None]
    assert analysis.memory_reference_context["play_memory_state"] is None
    endpoint = build_endpoint_payload(snapshots, info_hash="fixture")
    cols, rows = endpoint["timeseries"]["columns"], endpoint["timeseries"]["rows"]
    assert cols[-1] == "elapsed_sec"
    assert [row[cols.index("mem_anon_rss_plus_swap_kib")] for row in rows] == [None, 2048, None]
    assert [row[cols.index("mem_memory_source")] for row in rows] == [None, SOURCE, SOURCE]


def test_capture_proc_worker_does_not_queue_or_reuse_late_result():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, Lock
    from mpi.android.backend import StreamingCaptureSession

    session = StreamingCaptureSession.__new__(StreamingCaptureSession)
    pool = ThreadPoolExecutor(max_workers=1)
    session._pool = pool
    session._pending = {}
    session._pool_lock = Lock()
    session._closed = Event()
    release = Event()
    started = Event()
    calls = []

    def read(pid):
        calls.append(pid)
        started.set()
        release.wait(2)
        return pid, 0, None, 1

    first = session._submit_probe("memory", None, read, 7)
    assert first is not None and started.wait(1)
    assert session._submit_probe("memory", None, read, 8) is None
    assert not first.done() and calls == [7]
    release.set()
    assert first.result(timeout=1)[0] == (7, 0, None, 1)
    latest = session._submit_probe("memory", None, read, 8)
    assert latest is not None and latest is not first
    assert latest.result(timeout=1)[0] == (8, 0, None, 1)
    pool.shutdown()


@pytest.mark.parametrize("bad", [
    {"anon_rss_plus_swap_kib": 4096}, {"swap_kib": None}, {"anon_rss_kib": -1},
    {"anon_rss_kib": 1_073_741_825, "anon_rss_plus_swap_kib": 1_073_741_825},
    {"swap_kib": 2.5}, {"play_memory_pid": 0}, {"play_memory_start_ticks": None},
    {"play_memory_start_ticks": 1 << 53},
])
def test_imported_typed_memory_requires_consistent_parts_and_process(tmp_path, bad):
    good = MemStats(memory_source=SOURCE, anon_rss_kib=2048, swap_kib=0,
                    anon_rss_plus_swap_kib=2048, play_memory_pid=99, play_memory_start_ticks=10,
                    play_memory_state="foreground", play_memory_state_source="measured_oom_score_adj",
                    play_memory_ram_kib=7*1024**2, play_memory_sdk_int=35,
                    play_memory_form_factor="phone_tablet", pss_total_kb=88, rss_total_kb=92)
    memories = [MemStats(pss_total_kb=77), good, replace(good, **bad)]
    device = DeviceInfo(serial="fixture", platform=Platform.ANDROID)
    path = tmp_path / "report.csv"
    generate_csv_report([
        MetricsSnapshot(device=device, package=PackageInfo(bundle_id="com.example.qa"), captured_at=f"2026-11-21T10:00:0{i}Z",
                        duration_sec=1, elapsed_sec=i, memory=memory)
        for i,memory in enumerate(memories)
    ], str(path))
    replay = [s.memory for s in api._parse_live_csv(str(path))]
    assert replay[:2] == memories[:2]
    assert replay[2].memory_source == SOURCE
    assert replay[2].anon_rss_plus_swap_kib is None and replay[2].play_memory_ram_kib is None
    analysis = analyze_session(str(path))
    assert analysis.timeseries["anon_rss_plus_swap_mib"] == [None, 2.0, None]
    assert analysis.timeseries["app_memory_mb"] == [77/1024, None, None]
    assert analysis.memory_source == SOURCE and not any(analysis.memory_reference_context.values())


def test_replay_metadata_cannot_promote_inconsistent_state_category_or_scope():
    raw = {"mem_" + key: value for key,value in {
        "memory_source": SOURCE, "anon_rss_kib": "1", "swap_kib": "0",
        "anon_rss_plus_swap_kib": "1", "play_memory_pid": "99", "play_memory_start_ticks": "10",
        "play_memory_state": "foreground", "play_memory_state_source": "operator_label",
        "play_memory_category": "app", "play_memory_category_source": "android_package_is_game_hint",
        "play_memory_ram_kib": "0", "play_memory_sdk_int": "34.0", "play_memory_form_factor": "tv",
    }.items()}
    parsed = parse_replay_memory(raw, "android")
    assert parsed["anon_rss_plus_swap_kib"] == 1
    assert not any(value is not None for key,value in parsed.items() if key.startswith("play_memory_") and
                   key not in ("play_memory_pid", "play_memory_start_ticks"))
    assert parse_replay_memory(raw, "ios") == {"memory_source": SOURCE}
    assert parse_replay_memory({"mem_anon_rss_plus_swap_kib": "1"}, "android") == {}
