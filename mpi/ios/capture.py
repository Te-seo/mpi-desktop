"""xctrace record + export orchestration (PLAN-ios §5, Phase i4).

Pure subprocess plumbing: resolve the target pid, run ``xctrace record`` for
the requested duration, then ``xctrace export`` the needed streams to XML.
Parsing lives in :mod:`mpi.ios.parsers`.
"""

from __future__ import annotations

import re
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Optional, TypedDict

from . import xcrun
from .xcrun import XcrunError


class TraceResult(TypedDict):
    trace_path: str
    pid: int
    activity_monitor_xml: str
    core_animation_xml: Optional[str]


def record_trace(
    udid: str,
    bundle_id: str,
    duration_sec: float,
    template: str = "Activity Monitor",
    include_core_animation: bool = True,
) -> TraceResult:
    """Record a trace bundle and export the requested streams.

    Flow (see PLAN-ios §5 Phase i4):

    1. Resolve pid via ``devicectl device process list --bundle-identifier``
       (launch via ``process launch`` if not running).
    2. ``xctrace record --device <udid> --template <template> --attach <pid>
       --time-limit <duration_sec>s --output <tmp>/<ts>.trace``.
    3. Export Activity Monitor table (and optionally Core Animation) via
       ``xctrace export --input <bundle> --xpath ...``.
    4. Return paths + XML bodies for the parser layer.

    Returns a :class:`TraceResult` with the raw XML bodies so callers can
    stash them in ``MetricsSnapshot.raw`` for the CLI's RAW panel.

    """
    if duration_sec <= 0:
        raise ValueError("duration_sec must be > 0")

    pid = _resolve_pid(udid, bundle_id)
    trace_path = _trace_output_path(bundle_id)

    xcrun.run(
        [
            "xctrace",
            "record",
            "--device",
            udid,
            "--template",
            template,
            "--attach",
            str(pid),
            "--time-limit",
            f"{duration_sec:.3f}s",
            "--output",
            trace_path,
        ],
        timeout=max(duration_sec + 90.0, 120.0),
    )

    activity_xml = _export_first(
        trace_path,
        (
            '//table[@schema="activity-monitor"]',
            '//table[contains(@schema, "activity")]',
            "//table",
        ),
        required=True,
    )
    core_xml = None
    if include_core_animation:
        core_xml = _export_first(
            trace_path,
            (
                '//table[@schema="core-animation"]',
                '//table[contains(@schema, "core-animation")]',
                '//table[contains(@schema, "animation")]',
            ),
            required=False,
        )

    return {
        "trace_path": trace_path,
        "pid": pid,
        "activity_monitor_xml": activity_xml,
        "core_animation_xml": core_xml,
    }


def _trace_output_path(bundle_id: str) -> str:
    safe_bundle = re.sub(r"[^A-Za-z0-9._-]", "_", bundle_id) or "app"
    run_dir = Path(tempfile.mkdtemp(prefix="mpi-ios-"))
    return str(run_dir / f"{safe_bundle}-{int(time.time())}.trace")


def _resolve_pid(udid: str, bundle_id: str) -> int:
    errors: list[str] = []
    for _ in range(2):
        try:
            data = xcrun.run_json(
                [
                    "devicectl",
                    "device",
                    "process",
                    "list",
                    "--device",
                    udid,
                    "--bundle-identifier",
                    bundle_id,
                ],
                timeout=45.0,
            )
            pid = _find_pid(data, bundle_id)
            if pid:
                return pid
        except XcrunError as exc:
            errors.append(str(exc))

        try:
            launch_output = xcrun.run(
                [
                    "devicectl",
                    "device",
                    "process",
                    "launch",
                    "--device",
                    udid,
                    bundle_id,
                ],
                timeout=60.0,
            )
            pid = _parse_pid_from_text(launch_output)
            if pid:
                return pid
        except XcrunError as exc:
            errors.append(str(exc))

        time.sleep(1.0)

    details = "; ".join(errors[-3:]) if errors else "process list returned no pid"
    raise XcrunError(f"could not resolve running pid for {bundle_id} on {udid}: {details}")


def _find_pid(value: Any, bundle_id: str) -> int:
    matched = _find_pid_for_bundle(value, bundle_id)
    if matched:
        return matched
    pids: set[int] = set()
    for item in _walk_dicts(value):
        pid = _lookup_int(item, ("pid", "processIdentifier", "processID"))
        if pid > 0:
            pids.add(pid)
    if len(pids) == 1:
        return next(iter(pids))
    return 0


def _find_pid_for_bundle(value: Any, bundle_id: str) -> int:
    """Return only a PID whose own record identifies ``bundle_id``.

    Unlike :func:`_find_pid`, this deliberately has no single-PID fallback and
    is therefore suitable for destructive operations such as app termination.
    """
    for item in _walk_dicts(value):
        text = " ".join(
            str(child) for child in item.values() if not isinstance(child, (dict, list))
        )
        if bundle_id not in text and not _dict_has_bundle(item, bundle_id):
            continue
        pid = _lookup_int(item, ("pid", "processIdentifier", "processID"))
        if pid > 0:
            return pid
    return 0


def _dict_has_bundle(value: dict, bundle_id: str) -> bool:
    for child in _walk_values(value):
        if str(child) == bundle_id:
            return True
    return False


def _walk_dicts(value: Any) -> Iterable[dict]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _walk_values(value: Any) -> Iterable[Any]:
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_values(child)
    else:
        yield value


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _lookup_int(value: Any, keys: Iterable[str]) -> int:
    wanted = {_norm(k) for k in keys}
    for item in _walk_dicts(value):
        for key, raw in item.items():
            if _norm(str(key)) not in wanted:
                continue
            try:
                return int(str(raw).strip())
            except (TypeError, ValueError):
                continue
    return 0


def _parse_pid_from_text(text: str) -> int:
    for pattern in (
        r"\bpid\s*[:=]\s*(\d+)\b",
        r"\bprocess(?:Identifier|ID)?\s*[:=]\s*(\d+)\b",
    ):
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return 0


def _export_first(trace_path: str, xpaths: Iterable[str], required: bool) -> Optional[str]:
    errors: list[str] = []
    for xpath in xpaths:
        try:
            text = xcrun.run(
                ["xctrace", "export", "--input", trace_path, "--xpath", xpath],
                timeout=90.0,
            )
        except XcrunError as exc:
            errors.append(str(exc))
            continue
        if text.strip():
            return text
    if required:
        detail = "; ".join(errors[-3:]) if errors else "empty xctrace export"
        raise XcrunError(f"could not export Activity Monitor data from {trace_path}: {detail}")
    return None
