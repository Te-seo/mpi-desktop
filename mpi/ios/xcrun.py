"""Thin subprocess wrapper for ``xcrun`` (PLAN-ios §5, Phase i1).

Mirrors ``mpi/android/adb.py`` in spirit: no parsing, only process
plumbing. All functions raise :class:`XcrunError` on failure and include the
offending argv in the message.
"""

from __future__ import annotations

import json
import subprocess
from typing import List, Optional


class XcrunError(RuntimeError):
    """Raised on xcrun failure, absence, or timeout."""


def run(args: List[str], timeout: float = 60.0) -> str:
    """Invoke ``xcrun`` with the given arguments and return stdout.

    Decodes with ``errors="replace"``. Raises :class:`XcrunError` on non-zero
    exit, :class:`FileNotFoundError`-equivalent "xcrun not installed", or
    timeout. The longer default timeout vs. adb accounts for xcrun cold-start.

    """
    cmd = ["xcrun", *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise XcrunError(
            "xcrun executable not found. iOS capture requires macOS with "
            "Xcode command line tools installed."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise XcrunError(
            f"xcrun command timed out after {timeout:.1f}s: {' '.join(cmd)}"
        ) from exc

    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        raise XcrunError(
            f"xcrun failed ({' '.join(cmd)}): {msg or f'exit {proc.returncode}'}"
        )
    return proc.stdout


def run_json(args: List[str], timeout: float = 60.0) -> dict:
    """Invoke ``xcrun`` with ``--json-output -`` appended and parse stdout as JSON.

    Only valid for sub-tools that support the flag (currently ``devicectl``).

    """
    json_args = list(args)
    if "--json-output" not in json_args:
        json_args.extend(["--json-output", "-"])
    text = run(json_args, timeout=timeout)
    try:
        value = json.loads(text or "{}")
    except json.JSONDecodeError as exc:
        raise XcrunError(
            f"xcrun returned invalid JSON ({' '.join(['xcrun', *json_args])}): {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise XcrunError(
            f"xcrun JSON output was not an object: {' '.join(['xcrun', *json_args])}"
        )
    return value


def devicectl(args: List[str], udid: Optional[str] = None) -> str:
    """Run ``xcrun devicectl <args>`` with optional ``--device <udid>`` injected.

    Convenience wrapper returning raw stdout. For JSON output, callers should
    pass ``--json-output -`` explicitly and use :func:`run_json` on the full
    argv.

    """
    full_args = ["devicectl", *args]
    if udid and "--device" not in full_args:
        full_args.extend(["--device", udid])
    return run(full_args)
