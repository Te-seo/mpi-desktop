"""End-to-end tests for mpi.api over real subprocess stdin/stdout."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List

from mpi.report import generate_csv_report
from mpi.schema import (
    CpuStats,
    DeviceInfo,
    FrameStats,
    GpuStats,
    MetricsSnapshot,
    PackageInfo,
    Platform,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_api(requests: Iterable[Dict[str, Any]], timeout: float = 30.0) -> List[Dict[str, Any]]:
    """Spawn `python -m mpi.api`, feed requests, return parsed responses."""
    payload = "\n".join(json.dumps(r) for r in requests) + "\n"
    proc = subprocess.run(
        [sys.executable, "-m", "mpi.api"],
        input=payload,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=str(REPO_ROOT),
    )
    # api logs to stderr; never assert on it (helpful for debugging on failure)
    if proc.returncode not in (0, None):
        raise AssertionError(
            f"api exited {proc.returncode}\nstderr:\n{proc.stderr}"
        )
    responses = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        responses.append(json.loads(line))
    return responses


def _by_id(responses: List[Dict[str, Any]], rid: str) -> Dict[str, Any]:
    """Look up a response by its id. The api dispatches handlers on a thread
    pool, so output order is non-deterministic across requests."""
    for r in responses:
        if r.get("id") == rid:
            return r
    raise AssertionError(f"no response with id={rid!r} in {responses}")


def test_ping_roundtrip():
    responses = _run_api([
        {"id": "1", "command": "ping"},
        {"id": "2", "command": "shutdown"},
    ])
    ping = _by_id(responses, "1")
    assert ping["ok"] is True
    assert ping["result"]["pong"] is True


def test_unknown_command_returns_error():
    responses = _run_api([
        {"id": "1", "command": "no_such_handler"},
        {"id": "2", "command": "shutdown"},
    ])
    err = _by_id(responses, "1")
    assert err["ok"] is False
    assert "unknown command" in err["error"]


def test_invalid_json_returns_error_with_null_id():
    proc = subprocess.run(
        [sys.executable, "-m", "mpi.api"],
        input="this is not json\n{\"id\":\"1\",\"command\":\"shutdown\"}\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        cwd=str(REPO_ROOT),
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert lines, f"no stdout. stderr:\n{proc.stderr}"
    first = json.loads(lines[0])
    assert first["ok"] is False
    assert first["id"] is None
    assert "invalid JSON" in first["error"]


def test_stderr_logs_do_not_corrupt_stdout():
    """Even with INFO logging on, every stdout line must be valid JSON."""
    env = dict(os.environ, MPI_LOG="DEBUG")
    proc = subprocess.run(
        [sys.executable, "-m", "mpi.api"],
        input='{"id":"1","command":"ping"}\n{"id":"2","command":"shutdown"}\n',
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        cwd=str(REPO_ROOT),
        env=env,
    )
    assert proc.stderr  # info logs land here
    for line in proc.stdout.splitlines():
        if line.strip():
            json.loads(line)  # must round-trip


def test_overlay_commands_answer_without_a_device():
    """Both HUD commands must degrade to structured answers: the desktop asks
    about the overlay for whatever device is selected, and an absent/old
    companion is a normal state, not a crash."""
    responses = _run_api(
        [
            {
                "id": "1",
                "command": "companion_overlay_status",
                "payload": {"serial": "NO-SUCH-DEVICE"},
            },
            {
                "id": "2",
                "command": "set_capture_overlay",
                "payload": {
                    "capture_id": "not-a-capture",
                    "enabled": True,
                    "metrics": ["fps", "cpu"],
                },
            },
            {"id": "3", "command": "shutdown"},
        ],
        timeout=60.0,
    )

    status = _by_id(responses, "1")
    assert status["ok"] is True
    result = status["result"]
    assert result["serial"] == "NO-SUCH-DEVICE"
    assert result["installed"] is False
    assert result["supported"] is False
    assert result["can_draw_overlays"] is False
    assert result["protocol"] is None
    assert result["reason"] == "device_unreachable"

    toggle = _by_id(responses, "2")
    assert toggle["ok"] is False
    assert "unknown capture_id" in toggle["error"]


def test_load_file_accepts_live_csv_export(tmp_path):
    sample = tmp_path / "live.csv"
    device = DeviceInfo(
        serial="FAKE-SERIAL-1",
        platform=Platform.ANDROID,
        model="FakePhone",
        os_version="Android 16",
    )
    package = PackageInfo(bundle_id="com.example.alpha", label="Alpha")
    snapshots = [
        MetricsSnapshot(
            device=device,
            package=package,
            captured_at="2026-05-18T09:00:00+00:00",
            duration_sec=0.5,
            frames=FrameStats(fps_estimate=60.0, total_frames=30),
            cpu=CpuStats(cpu_percent=12.5, mem_percent=3.2, per_core_freq_mhz=[1200.0]),
            gpu=GpuStats(busy_percent=40.0),
        ),
        MetricsSnapshot(
            device=device,
            package=package,
            captured_at="2026-05-18T09:00:01+00:00",
            duration_sec=0.5,
            frames=FrameStats(fps_estimate=58.0, total_frames=29),
            cpu=CpuStats(cpu_percent=15.5, mem_percent=3.4, per_core_freq_mhz=[1300.0]),
            gpu=GpuStats(busy_percent=44.0),
        ),
    ]
    generate_csv_report(snapshots, str(sample))

    responses = _run_api([
        {
            "id": "1",
            "command": "load_file",
            "payload": {"path": str(sample), "measure_minutes": 50},
        },
        {"id": "2", "command": "shutdown"},
    ])
    load = _by_id(responses, "1")
    assert load["ok"] is True
    result = load["result"]
    assert result["source"] == "live_csv"
    assert result["report"]["source"] == "live_csv"
    assert result["report"]["fps"]["average"] == 59.0
    assert result["report"]["cpu_usage_avg"] == 14.0
    assert len(result["snapshots"]) == 2
    assert result["snapshots"][0]["frames"]["fps_estimate"] == 60.0


def test_removed_companion_sideload_ipc_cannot_install_any_file():
    responses = _run_api([
        {"id": "1", "command": "install_companion_apk",
         "payload": {"serial": "synthetic", "apk_path": "arbitrary.apk", "force": True}},
        {"id": "2", "command": "shutdown"},
    ])
    reply = _by_id(responses, "1")
    assert reply["ok"] is False
    assert "unknown command" in reply["error"].lower()


def test_play_bridge_bootstrap_requires_explicit_boolean_and_no_capture(monkeypatch):
    from mpi import api
    from mpi.android import companion
    states = []
    monkeypatch.setattr(companion, "overlay_support", lambda serial, *, allow_cold_start: states.append((serial, allow_cold_start)))
    monkeypatch.setattr(api, "_capture_running_on", lambda serial: serial == "RUNNING")
    for request in [
        {"serial": "S1"}, {"serial": "S1", "connect": "true"},
        {"serial": "S1", "connect": True}, {"serial": "RUNNING", "connect": True},
    ]:
        api._handle_companion_overlay_status(request)
    assert states == [("S1", False), ("S1", False), ("S1", True), ("RUNNING", False)]
