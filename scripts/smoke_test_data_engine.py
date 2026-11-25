"""End-to-end smoke test for the bundled data-engine.exe sidecar.

Spawns the executable, sends a few JSON-IPC requests over stdin, and validates
the responses on stdout. Exits 0 on success, prints a diff and exits 1 on
failure.

Usage:
    python scripts/smoke_test_data_engine.py [path/to/data-engine.exe]

If no path is given, uses ``dist/data-engine.exe`` relative to the repo root.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXE = REPO_ROOT / "dist" / "data-engine.exe"


def _by_id(responses: List[Dict[str, Any]], rid: str) -> Dict[str, Any]:
    """Look up a response by id. The api dispatches handlers on a thread pool
    so output order is not guaranteed across requests."""
    for r in responses:
        if r.get("id") == rid:
            return r
    raise AssertionError(f"no response with id={rid!r} in {responses}")


def _run(exe: Path, requests: Iterable[Dict[str, Any]], timeout: float = 60.0) -> List[Dict[str, Any]]:
    payload = "\n".join(json.dumps(r) for r in requests) + "\n"
    proc = subprocess.run(
        [str(exe)],
        input=payload,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )
    print(f"[exit] {proc.returncode}")
    if proc.stderr:
        print("[stderr]")
        for line in proc.stderr.splitlines()[:20]:
            print(f"  {line}")
    out: List[Dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as exc:
            print(f"[error] non-JSON stdout line: {line!r} ({exc})")
            sys.exit(1)
    return out


def main() -> int:
    exe = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_EXE
    if not exe.exists():
        print(f"[error] not found: {exe}")
        return 1

    print(f"[info] testing {exe}")

    # Test 1: ping
    responses = _run(
        exe,
        [
            {"id": "1", "command": "ping"},
            {"id": "2", "command": "shutdown"},
        ],
    )
    pong = next((r for r in responses if r.get("id") == "1"), None)
    if not pong or not pong.get("ok") or not pong.get("result", {}).get("pong"):
        print(f"[fail] ping: {responses}")
        return 1
    print("[pass] ping")

    # Test 2: list_devices — should succeed even with no devices attached.
    responses = _run(
        exe,
        [
            {"id": "1", "command": "list_devices"},
            {"id": "2", "command": "shutdown"},
        ],
    )
    listing = _by_id(responses, "1")
    if not listing.get("ok"):
        print(f"[fail] list_devices: {responses}")
        return 1
    devices = listing["result"]["devices"]
    print(f"[pass] list_devices ({len(devices)} device(s))")

    print("[ok] all smoke tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
