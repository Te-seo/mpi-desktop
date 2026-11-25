"""Persistent iOS metrics streamer (run as a standalone subprocess).

This script is *not* imported by the mpi backend. It is executed as a
separate process by :mod:`mpi.ios.pmd3` using whichever Python
interpreter has ``pymobiledevice3`` installed (the running interpreter in dev,
a system Python in frozen builds — pymobiledevice3 is never bundled). It must
therefore stay self-contained: only the standard library and ``pymobiledevice3``
may be imported, and nothing from the ``mpi`` package.

Why a long-lived process? Each ``pymobiledevice3 ... sysmon process single``
invocation re-establishes the whole tunneld -> RemoteServiceDiscovery -> DVT
handshake, which costs tens of seconds. Sampling once per second by re-spawning
that command is impossible — the old streaming path produced ~1 sample every
30 s. Here we open the DVT connection **once** and hold it open, pulling
continuous samples from two Instruments services:

* ``Sysmontap``              -> per-process CPU% and physical memory footprint.
* ``Graphics``               -> device GPU utilization + system-display Core
                                Animation FPS; never selected-app frame data.
* ``CoreProfileSessionTap``  -> kdebug display-commit tap: EXPERIMENTAL,
                                opt-in via MPI_IOS_KDEBUG_FPS=1. On
                                the tested hardware it counts display
                                refreshes, not game frames, so it stays
                                smooth during visible stutter — unusable as
                                game FPS there (see _kdebug_fps_enabled).

Output: one JSON object per line on stdout (newline-delimited JSON), e.g.::

    {"t": 1719240000.12, "pid": 4321, "cpu": 12.5, "mem_bytes": 268435456, "fps": 59.7}

Status / diagnostics go to stderr so stdout stays a clean NDJSON stream. The
parent process (``mpi.ios.pmd3.SysmonStreamReader``) reads stdout and
ignores any line that is not a JSON object.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import queue
import re
import sys
import time
import warnings
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Iterable, Optional


# Normalisation helpers ----------------------------------------------------


def _norm(value: Any) -> str:
    return "".join(ch for ch in str(value).lower() if ch.isalnum())


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip().rstrip("%").strip())
    except (TypeError, ValueError):
        return None


def _get(record: dict, *names: str) -> Any:
    wanted = {_norm(name) for name in names}
    for key, value in record.items():
        if _norm(key) in wanted:
            return value
    return None


# Frames-per-second extraction --------------------------------------------

# The OpenGL/Core Animation instrument reports FPS under one of a few keys
# depending on iOS version. We deep-scan each event for any of them.
_FPS_KEYS = ("coreanimationframespersecond", "framespersecond", "fps")


def _scan_fps(event: Any) -> Optional[float]:
    """Best-effort search for a frames-per-second value in a Graphics event.

    Graphics events arrive either as ``(selector, [args...])`` tuples or as
    nested dicts/lists, so walk the whole structure looking for a known FPS key
    with a plausible numeric value.

    ``CoreAnimationFramesPerSecond`` is an aggregate system-display metric.
    It does not attribute frames to the selected PID and cannot supply raw
    frame durations. Some devices cap this reading near 60 on 120 Hz panels.
    """
    found: Optional[float] = None
    for key, value in _walk_items(event):
        norm = _norm(key)
        if norm in _FPS_KEYS or norm.endswith("framespersecond"):
            number = _num(value)
            if number is not None and 0 <= number <= 1000:
                found = number
    return found


# GPU utilization extraction -----------------------------------------------

# The Graphics/OpenGL instrument reports GPU load under "Device Utilization %"
# (overall), with "Renderer Utilization %" / "Tiler Utilization %" tracking the
# two GPU stages. _norm() strips the space and percent sign, so "Device
# Utilization %" -> "deviceutilization". We surface the overall figure, the iOS
# twin of Android's kgsl gpu_busy_percentage.
def _scan_gpu(event: Any) -> Optional[float]:
    """Best-effort GPU busy percentage from a Graphics event.

    Prefers the overall "Device Utilization %"; the renderer/tiler figures track
    it closely and are only used if the device figure is absent.
    """
    device: Optional[float] = None
    stage: Optional[float] = None
    for key, value in _walk_items(event):
        norm = _norm(key)
        if norm.startswith("deviceutilization"):
            number = _num(value)
            if number is not None and 0 <= number <= 1000:
                device = number
        elif norm.startswith(("rendererutilization", "tilerutilization")):
            number = _num(value)
            if number is not None and 0 <= number <= 1000:
                stage = number if stage is None else max(stage, number)
    return device if device is not None else stage


def _walk_items(value: Any) -> Iterable[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key), child
            yield from _walk_items(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk_items(child)


def _find_process(snapshot: list, pid: int) -> Optional[dict]:
    for entry in snapshot:
        if not isinstance(entry, dict):
            continue
        entry_pid = _num(_get(entry, "pid"))
        if entry_pid is not None and int(entry_pid) == pid:
            return entry
    return None


def _system_cpu_total(processes: Iterable[dict]) -> Optional[float]:
    """System-wide CPU% as the sum of every process's ``cpuUsage``.

    Sysmontap streams the full process table each frame, so summing every
    process's per-core-summed cpuUsage (one fully busy core == 100, so the sum
    spans 0..cores*100 — the *same* unit as the per-process app value) gives the
    total CPU the device is doing. The backend divides this by the core count to
    a 0-100 figure, exactly as it divides the app value. We sum the processes
    rather than read Sysmontap's separate ``SystemCPUUsage`` row because that row
    is dropped by ``iter_processes()`` and reconstructing it would mean rebuilding
    the (marginal over Wi-Fi) tap loop; the summed value is in the identical scale
    and needs no extra device round-trip. Returns None unless at least one
    finite, non-negative CPU value is present, so missing or malformed process
    rows do not masquerade as an all-idle 0% snapshot.
    """
    total = 0.0
    seen_cpu = False
    for entry in processes:
        if not isinstance(entry, dict):
            continue
        value = _num(_get(entry, "cpuUsage", "cpu"))
        if value is None or not math.isfinite(value) or value < 0:
            continue
        seen_cpu = True
        total += value
    return total if seen_cpu else None


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _status(message: str) -> None:
    sys.stderr.write(message.rstrip() + "\n")
    sys.stderr.flush()


# Device connection --------------------------------------------------------


async def _maybe_await(value):
    """Await pymobiledevice3 calls while tolerating older synchronous builds."""
    if asyncio.iscoroutine(value):
        return await value
    return value


async def _close_async(obj) -> None:
    """Best-effort close for service providers and async context managers."""
    if obj is None:
        return
    aexit = getattr(obj, "__aexit__", None)
    if aexit is not None:
        try:
            await _maybe_await(aexit(None, None, None))
            return
        except Exception:
            pass
    closer = getattr(obj, "close", None)
    if closer is not None:
        try:
            await _maybe_await(closer())
        except Exception:
            pass


async def _connect_usbmux(udid: str):
    """Connect through lockdown/usbmux (USB first, network when advertised)."""
    from pymobiledevice3.lockdown import create_using_usbmux

    return await _maybe_await(create_using_usbmux(serial=udid or None))


async def _connect_rsd(
    udid: str,
    tunneld: Optional[tuple[str, int]],
    rsd: Optional[tuple[str, int]],
):
    from pymobiledevice3.remote.remote_service_discovery import (
        RemoteServiceDiscoveryService,
    )
    from pymobiledevice3.tunneld.api import (
        TUNNELD_DEFAULT_ADDRESS,
        get_tunneld_device_by_udid,
        get_tunneld_devices,
    )

    if rsd is not None:
        service = RemoteServiceDiscoveryService((rsd[0], int(rsd[1])))
        await service.connect()
        return service

    address = tunneld or TUNNELD_DEFAULT_ADDRESS
    if udid:
        service = await get_tunneld_device_by_udid(udid, address)
        if service is not None:
            return service

    # No exact match (or no udid given): fall back to a lone active tunnel.
    rsds = await get_tunneld_devices(address)
    if len(rsds) == 1:
        return rsds[0]
    for extra in rsds:
        try:
            await extra.close()
        except Exception:
            pass
    raise RuntimeError(
        f"no RemoteServiceDiscovery tunnel available for udid {udid!r} "
        f"via tunneld {address!r} (found {len(rsds)} tunnels)"
    )


async def _read_core_count(dvt) -> Optional[int]:
    """Logical CPU count from the DVT DeviceInfo instrument, or None.

    ``numberOfCpus`` is the normalization divisor for the per-core-summed CPU
    percentages (app + system). On Apple silicon there is no SMT, so logical
    CPUs == physical cores == hardware threads (e.g. A15 = 6), which is why the
    same count serves as both the core and thread basis. Best effort: a failure
    just leaves total_percent / cpu_norm unset rather than breaking the stream.
    """
    try:
        from pymobiledevice3.services.dvt.instruments.device_info import DeviceInfo

        async with DeviceInfo(dvt) as device_info:
            hardware = await device_info.hardware_information()
        count = _num((hardware or {}).get("numberOfCpus"))
        if count is not None and count >= 1:
            return int(count)
    except Exception as exc:  # never let device-info probing break the stream
        _status(f"core count unavailable: {exc}")
    return None


async def _sysmon_loop(dvt, pid: int, interval_ms: int, state: dict) -> None:
    from pymobiledevice3.services.dvt.instruments.sysmontap import Sysmontap

    sysmon = await Sysmontap.create(dvt, interval=interval_ms)
    _status("sysmon: instrument created")
    # Core count is read once and reused to normalize the per-core-summed CPU
    # percentages; held in state so the per-frame emit can stamp it onto rows.
    state["cores"] = await _read_core_count(dvt)
    if state.get("cores"):
        _status(f"sysmon: {state['cores']} CPU cores")
    # pymobiledevice3 hardcodes Sysmontap's *output* frequency ("ur") to
    # MINIMUM_INTERVAL_MS = 1 ms, so the device re-emits the same process snapshot
    # ~1000x/s no matter what sampleInterval (1 s) we ask for. Over USB the host
    # drains that flood fine, but over a low-MTU Wi-Fi RemoteXPC tunnel it
    # saturates the link and the first complete DTX frame never reassembles within
    # the connect window — so WIRELESS live capture received ZERO samples and fell
    # back to a single-shot that floods (and times out at 90 s) the same way. Align
    # the output frequency with the sample interval so the device sends one
    # snapshot per interval (~1 Hz) instead of ~1000. We patch the already-built
    # instance config (sent verbatim by Tap.__aenter__) rather than the global
    # Sysmontap.MINIMUM_INTERVAL_MS class attribute, so the override stays scoped to
    # this streamer and can't leak into any other pymobiledevice3 consumer.
    try:
        config = getattr(sysmon, "_config", None)
        if isinstance(config, dict) and "ur" in config:
            config["ur"] = max(int(interval_ms), 1)
    except Exception as exc:  # never let a pmd3 internal change break capture
        _status(f"sysmon: could not set output frequency: {exc}")
    clock = state["clock"]
    # The device now emits at ~1 Hz, but keep a host-side floor at HALF the sample
    # interval as belt-and-suspenders against any residual burst: a near-1.0× gap
    # would drop a normal ~1 Hz row that arrived slightly early, leaving that parent
    # drain window empty, so interval/2 suppresses only true floods.
    min_gap = max(interval_ms, 1) / 1000.0 / 2
    last_emit = 0.0
    async with sysmon:
        _status("sysmon: streaming started")
        first_frame = True
        skip_first = True  # the first snapshot has uninitialised cpuUsage values
        async for snapshot in sysmon.iter_processes():
            if skip_first:
                skip_first = False
                continue
            now = clock()
            if now - last_emit < min_gap:
                continue
            last_emit = now
            if first_frame:
                first_frame = False
                _status("sysmon: first frame")
            fps, fps_at, fps_source = _current_fps_reading(state, now)
            line: dict = {"t": now, "pid": pid, "fps": fps,
                          "fps_at": fps_at, "fps_source": fps_source}
            # Drain the kdebug FPS windows completed since the previous line so
            # frame/jank counts are never dropped when window boundaries beat
            # against the ~1 Hz sysmon cadence. Counts (not gauges): absent keys
            # mean "no kdebug data", zeros mean "no frames committed". Frame-time
            # percentiles merge across drained windows as worst-case (max) for
            # the tail percentiles and mean for the median.
            windows = state.get("kdebug_windows")
            if windows:
                k_frames = k_jank = k_big = 0
                p50s: list = []
                p90s: list = []
                p95s: list = []
                p99s: list = []
                drained = False
                while True:
                    try:
                        frames_n, jank_n, big_n, pcts = windows.popleft()
                    except IndexError:
                        break
                    drained = True
                    k_frames += frames_n
                    k_jank += jank_n
                    k_big += big_n
                    p50, p90, p95, p99 = pcts
                    if p50 is not None:
                        p50s.append(p50)
                        p90s.append(p90)
                        p95s.append(p95)
                        p99s.append(p99)
                if drained:
                    line["k_frames"] = k_frames
                    line["k_jank"] = k_jank
                    line["k_big"] = k_big
                    if p50s:
                        line["k_p50"] = round(sum(p50s) / len(p50s), 2)
                        line["k_p90"] = max(p90s)
                        line["k_p95"] = max(p95s)
                        line["k_p99"] = max(p99s)
            gpu = state.get("gpu")
            if gpu is not None:
                line["gpu"] = gpu
                line["gpu_at"] = state.get("gpu_at")
            battery = state.get("battery")
            if battery:
                line["battery"] = battery
                line["battery_at"] = state.get("battery_at")
                line["battery_field_times"] = state.get("battery_field_times", {})
            # System-wide total CPU (raw per-core-summed) + core count, so the
            # backend can surface total CPU% and the core-normalized app CPU%.
            cores = state.get("cores")
            if cores:
                line["cores"] = cores
            total_cpu = _system_cpu_total(snapshot)
            if total_cpu is not None:
                line["cpu_total"] = total_cpu
            proc = _find_process(snapshot, pid)
            if proc is None:
                line["missing"] = True
            else:
                cpu = _num(_get(proc, "cpuUsage", "cpu"))
                mem = _num(
                    _get(
                        proc,
                        "physFootprint",
                        "memResidentSize",
                        "residentSize",
                        "memRss",
                    )
                )
                if cpu is not None:
                    line["cpu"] = cpu
                if mem is not None:
                    line["mem_bytes"] = int(mem)
            _drain_raw_events(state, line)
            _emit(line)


# kdebug display-FPS tap ----------------------------------------------------

# EXPERIMENTAL, opt-in (MPI_IOS_KDEBUG_FPS=1; default off). The kdebug
# trace tap counts render-server display-commit events — class 0x31, subclass
# 0x80, code 0xC6 => eventid 0x31800318 — the mechanism py-ios-device's
# `instruments display` documents for up-to-120 readings. HOWEVER, on this
# project's tested hardware (iPhone 15 Pro Max, ProMotion) the event evidently
# fires per display REFRESH, not per content commit: it read smooth/high while
# the game visibly stuttered and froze, and produced no janks. It is therefore
# NOT selected-app FPS. Graphics is also display-wide. The tap remains
# available for experimentation — its event-mix diagnostic
# is the tool for identifying a device's real event shape.
_KDEBUG_FPS_EVENT = 0x31800318
# kdf2 subclass filters: (class<<24) | (subclass<<16). 0x3180 carries the frame
# commit event; 0x31B0 (CA client transactions) and 0x2599 (DBG_PERF sampling)
# are the two companions py-ios-device's production FPS config requests. They
# matter beyond parity: their ambient events advance our DEVICE-TIME window
# clock even when the display commits nothing, so a frozen/static screen closes
# 0-fps windows in near-real-time instead of silently holding the last value.
_KDEBUG_FILTERS = frozenset({0x31800000, 0x31B00000, 0x25990000})
# One fps/jank window = this much DEVICE time (mach-timebase), gaps included —
# a second with no commits is a 0 fps second.
_KDEBUG_WINDOW_NS = 1_000_000_000
# DgbFuncQual.DBG_FUNC_END — the closing half of a START/END pair. The frame
# event is counted once per frame by EXCLUDING ENDs (devices emitting plain
# qual-NONE events count those; devices emitting START/END pairs count the
# START). Counting the masked eventid alone would double-count paired frames —
# and the pairs spread apart exactly when commits get SLOW, inflating the
# reported FPS precisely during stutter.
_KDEBUG_QUAL_END = 2
# Jank thresholds: a frame is a jank when it takes both 2x the
# average of the previous 3 frames AND longer than two movie frames (2/24 s);
# a big jank at 3x and three movie frames (3/24 s).
_KDEBUG_JANK_NS = 2 * 1_000_000_000 / 24
_KDEBUG_BIG_JANK_NS = 3 * 1_000_000_000 / 24
# Sub-4 ms commit deltas are double-commit artifacts of a single frame — merge
# them into the following frame instead of counting them (rust-ios-device does
# the same de-dup).
_KDEBUG_MIN_FRAME_NS = 4_000_000
# Hard cap on the tap's setConfig/start/ack handshake. An earlier UNFILTERED
# core-profile experiment on this project's Windows + Wi-Fi tunnel setup wedged
# tunneld; the narrow kernel-side kdf2 filter avoids that flood, but if the
# channel ever hangs anyway this bound strands only this optional task instead
# of holding resources until capture teardown.
_KDEBUG_START_TIMEOUT_SEC = 20.0


_GAUGE_MAX_AGE_SEC = 3.0
_RAW_EVENT_QUEUE_LIMIT = 4096


def _current_fps_reading(state: dict, now: float) -> tuple:
    """System-display FPS only; neither source identifies the selected app."""
    for key, source in (("fps_kdebug", "ios_kdebug_display_experimental"),
                        ("fps", "ios_graphics_core_animation")):
        value, observed_at = state.get(key), state.get(key + "_at")
        if (value is not None and isinstance(observed_at, (int, float))
                and 0 <= now - observed_at <= _GAUGE_MAX_AGE_SEC):
            return value, observed_at, source
    return None, None, None


def _current_fps(state: dict, now: float) -> Optional[float]:
    return _current_fps_reading(state, now)[0]


def _drain_raw_events(state: dict, line: dict) -> None:
    events = state.get("kdebug_raw_events")
    drained = []
    if events is not None:
        while True:
            try:
                drained.append(events.popleft())
            except IndexError:
                break
    if drained:
        line["raw_frame_events"] = drained
    # Cumulative counter avoids producer/consumer reset races.
    line["raw_frame_events_dropped"] = state.get("kdebug_raw_dropped", 0)


def _update_battery(state: dict, fields: dict, *, merge: bool = False) -> None:
    now = state.get("clock", time.time)()
    value = dict(state.get("battery") or {}) if merge else {}
    times = dict(state.get("battery_field_times") or {}) if merge else {}
    for key, field in fields.items():
        if key == "temp_at":
            continue
        value[key] = field
        times[key] = fields.get("temp_at", now) if key == "temp_c" else now
    state["battery"] = value
    state["battery_field_times"] = times
    state["battery_at"] = max(times.values()) if times else None


def _frame_time_percentiles(deltas_ns: list) -> tuple:
    """(p50, p90, p95, p99) frame times in ms for one window, or all-None."""
    if not deltas_ns:
        return (None, None, None, None)
    ordered = sorted(deltas_ns)
    n = len(ordered)

    def pick(q: float) -> float:
        return round(ordered[min(n - 1, int(q * n))] / 1_000_000, 2)

    return (pick(0.50), pick(0.90), pick(0.95), pick(0.99))


def _count_display_frames(stream, state: dict, ns_per_tick: float, clock) -> None:
    """Blocking consumer (runs on a worker thread): fold render-server frame
    commits from the kdebug stream into FIXED 1-second DEVICE-TIME windows of
    fps + jank counts + frame-time percentiles.

    Accuracy rules — each corresponds to a way stutter was previously hidden:

    * Only ONE event per frame is counted: qual-NONE/START markers, never the
      END of a START/END pair (_KDEBUG_QUAL_END). Matching the masked eventid
      alone double-counts paired frames, and the pair spreads apart exactly
      when commits get slow — inflating FPS precisely during stutter.
    * Windows are wall-clock time on the DEVICE timeline, gaps INCLUDED: a
      second in which nothing was committed is a genuine 0-fps window,
      so freezes and loading stalls show up instead of
      being skipped as "idle". Any filtered event — frame or companion-
      subclass heartbeat — advances the window clock, so 0-fps windows keep
      closing even while no frames commit at all.
    * A multi-second stall is also one (big-)jank frame whose true cost lands
      in the percentiles of the window where it completed.

    Timestamps are device-side mach ticks converted via the device timebase
    (numer/denom), immune to host/transport jitter. Ends when the stream hits
    EOF (tap closed / tunnel dropped) or the parser raises.
    """
    from pykdebugparser.pykdebugparser import PyKdebugParser

    parser = PyKdebugParser()
    parser.filter_class = sorted({f >> 24 for f in _KDEBUG_FILTERS})
    windows = state["kdebug_windows"]
    raw_events = state.setdefault("kdebug_raw_events", deque(maxlen=_RAW_EVENT_QUEUE_LIMIT))
    stream_id = state.setdefault("kdebug_stream_id", uuid.uuid4().hex)
    window_ticks = max(1, int(_KDEBUG_WINDOW_NS / ns_per_tick))
    win_start: Optional[int] = None
    last_frame_ts: Optional[int] = None
    carry_ns = 0.0
    win_frames = 0
    win_jank = 0
    win_big = 0
    win_deltas: list = []
    recent: deque = deque(maxlen=3)
    first_frame = True
    # One-shot diagnostics: which debugids this device actually emits (and at
    # what mix), so per-device event-shape questions are answerable from the
    # engine log without another instrumented build.
    diag_ids: dict = {}
    diag_events = 0

    def _close_window() -> None:
        nonlocal win_frames, win_jank, win_big, win_deltas
        state["fps_kdebug"] = win_frames / (_KDEBUG_WINDOW_NS / 1_000_000_000)
        state["fps_kdebug_at"] = clock()
        # deque append/popleft are thread-safe; the sysmon loop drains this.
        windows.append(
            (win_frames, win_jank, win_big, _frame_time_percentiles(win_deltas))
        )
        win_frames = 0
        win_jank = 0
        win_big = 0
        win_deltas = []

    for event in parser.kevents(stream):
        ts = event.timestamp
        if diag_events < 2000:
            diag_events += 1
            key = f"0x{event.debugid:08x}"
            diag_ids[key] = diag_ids.get(key, 0) + 1
            if diag_events == 2000:
                top = sorted(diag_ids.items(), key=lambda kv: -kv[1])[:8]
                _status(f"kdebug: event mix (first 2000): {top}")
        if win_start is None:
            win_start = ts
        # Close every elapsed 1 s device-time window — including EMPTY (0 fps)
        # ones during a freeze, which heartbeat events keep flushing out.
        while ts - win_start >= window_ticks:
            _close_window()
            win_start += window_ticks
        if event.eventid != _KDEBUG_FPS_EVENT:
            continue
        if event.func_qualifier == _KDEBUG_QUAL_END:
            continue  # closing half of a START/END pair — its START counted
        if last_frame_ts is None:
            last_frame_ts = ts
            continue
        if ts <= last_frame_ts:
            continue  # repeated/out-of-order marker must not move the baseline
        delta_ns = (ts - last_frame_ts) * ns_per_tick
        last_frame_ts = ts
        if delta_ns <= 0:
            continue
        if delta_ns < _KDEBUG_MIN_FRAME_NS:
            # Double-commit artifact: fold into the next frame's cost.
            carry_ns += delta_ns
            continue
        frame_ns = delta_ns + carry_ns
        carry_ns = 0.0
        if len(raw_events) == raw_events.maxlen:
            state["kdebug_raw_dropped"] = state.get("kdebug_raw_dropped", 0) + 1
        raw_events.append({"timestamp_ns": round(ts * ns_per_tick),
                           "duration_ns": round(frame_ns), "stream_id": stream_id,
                           "source": "ios_kdebug_display_experimental",
                           "scope": "system_display", "experimental": True})
        if first_frame:
            first_frame = False
            _status("kdebug: first display frame event")
        win_frames += 1
        win_deltas.append(frame_ns)
        if len(recent) == 3:
            avg = (recent[0] + recent[1] + recent[2]) / 3.0
            if frame_ns > 2 * avg and frame_ns > _KDEBUG_JANK_NS:
                win_jank += 1
                if frame_ns > 3 * avg and frame_ns > _KDEBUG_BIG_JANK_NS:
                    win_big += 1
        if frame_ns >= _KDEBUG_WINDOW_NS:
            # A multi-second stall was counted above (frame + jank +
            # percentile); reset the jank baseline so post-stall frames are
            # compared against fresh cadence, not against the stall itself.
            recent.clear()
        else:
            recent.append(frame_ns)

    # Stream ended (tap closed / capture stopping): flush the partial window's
    # COUNTS so a trailing stutter still reaches the jank/percentile data. The
    # fps gauge keeps the last full-window value (a sub-second remainder would
    # only distort it).
    if win_frames or win_jank or win_deltas:
        windows.append(
            (win_frames, win_jank, win_big, _frame_time_percentiles(win_deltas))
        )


async def _kdebug_fps_loop(dvt, state: dict) -> None:
    """Continuously derive experimental display-event FPS and jank from the
    kdebug trace tap. Best effort: any failure (kperf already owned by
    another profiler, tap unsupported, tunnel drop) must not stop CPU/memory
    streaming — the FPS stamp falls back to the Graphics-service value."""
    try:
        from pymobiledevice3.services.dvt.instruments.core_profile_session_tap import (
            CoreProfileSessionTap,
        )
        import pykdebugparser.pykdebugparser  # noqa: F401 - probe before starting the tap
    except Exception as exc:  # pragma: no cover - import guard
        _status(f"kdebug fps unavailable (import): {exc}")
        return

    try:
        time_config = await CoreProfileSessionTap.get_time_config(dvt)
        ns_per_tick = float(time_config["numer"]) / float(time_config["denom"])
    except Exception as exc:
        _status(f"kdebug fps unavailable (time config): {exc}")
        return

    clock = state["clock"]
    tap = CoreProfileSessionTap(dvt, time_config, filters=set(_KDEBUG_FILTERS))
    entered = False
    producer = None
    try:
        # Explicit __aenter__ (rather than `async with`) so the handshake can be
        # time-bounded — see _KDEBUG_START_TIMEOUT_SEC.
        await asyncio.wait_for(tap.__aenter__(), timeout=_KDEBUG_START_TIMEOUT_SEC)
        entered = True
        chunk_queue: queue.Queue = queue.Queue()
        stream = tap.get_kdbuf_stream(chunk_queue)
        producer = asyncio.create_task(tap.pump_kdbuf_chunks(chunk_queue))
        _status("kdebug: tap started (display frame events)")
        await asyncio.to_thread(
            _count_display_frames, stream, state, ns_per_tick, clock
        )
        _status("kdebug: event stream ended")
    except asyncio.CancelledError:
        raise
    except (asyncio.TimeoutError, TimeoutError):
        _status("kdebug fps unavailable: tap start timed out")
    except Exception as exc:
        _status(f"kdebug fps unavailable: {exc}")
    finally:
        # Whatever ended the tap, stop preferring its (now dead) readings so the
        # stamp falls back to the Graphics-service FPS.
        state["fps_kdebug"] = None
        state["fps_kdebug_at"] = None
        if producer is not None:
            producer.cancel()
            try:
                await producer
            except (asyncio.CancelledError, Exception):
                pass
        if entered:
            try:
                await asyncio.wait_for(tap.__aexit__(None, None, None), timeout=10.0)
            except (asyncio.CancelledError, Exception):
                pass


async def _graphics_loop(dvt, state: dict) -> None:
    """Continuously refresh ``state['fps']`` and ``state['gpu']`` from the one
    Graphics/OpenGL instrument (it carries both Core Animation FPS and GPU
    utilization). Best effort: a failure here must not stop CPU/memory streaming,
    so swallow errors after reporting them."""
    try:
        from pymobiledevice3.services.dvt.instruments.graphics import Graphics
    except Exception as exc:  # pragma: no cover - import guard
        _status(f"graphics instrument unavailable (import): {exc}")
        return

    try:
        async with Graphics(dvt) as graphics:
            async for event in graphics:
                fps = _scan_fps(event)
                if fps is not None:
                    state["fps"] = fps
                    state["fps_at"] = state["clock"]()
                gpu = _scan_gpu(event)
                if gpu is not None:
                    state["gpu"] = gpu
                    state["gpu_at"] = state["clock"]()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _status(f"graphics instrument unavailable: {exc}")


# Battery telemetry --------------------------------------------------------

# Battery changes slowly, so a 5 s poll is plenty and keeps the diagnostics
# relay off the per-frame hot path. The IOPMPowerSource dict also carries a live
# top-level "Temperature", which we surface as a battery temperature alongside
# level / current / voltage / charging-state (see _battery_temp_c).
_BATTERY_INTERVAL_SEC = 5.0
# Cap a single get_battery() call so a dropped/marginal Wi-Fi tunnel can't wedge
# the battery task forever (the iOS tunnel rotates/drops on phone lock). A
# timed-out poll just retries next cycle; CPU/mem/FPS/GPU run on sibling tasks
# and are unaffected either way (an async await never blocks the event loop).
_BATTERY_TIMEOUT_SEC = 15.0
_GUARD_INTERVAL_SEC = 1.0
_GUARD_FIRST_READING_TIMEOUT_SEC = 45.0
_GUARD_LAUNCH_TIMEOUT_SEC = 45.0
# After the first valid temperature, a disconnected diagnostics socket must not
# retain the guard/device reservation forever. A healthy 1 Hz relay answers in
# milliseconds; allow a generous five seconds per probe and three consecutive
# probes with zero successful source responses before declaring disconnect.
_GUARD_POST_VALID_PROBE_TIMEOUT_SEC = 5.0
_GUARD_PROBE_COMPLETION_GRACE_SEC = 0.25
_GUARD_MAX_CONSECUTIVE_PROBE_FAILURES = 3
_SYSLOG_TEMPERATURE_QUEUE_SIZE = 64
_SYSLOG_TEMPERATURE_DEDUPE_SEC = 2.0
_SYSLOG_TEMPERATURE_CONNECT_TIMEOUT_SEC = 10.0
_SYSLOG_TEMPERATURE_STALE_SEC = 90.0
_BATTERY_TEMPERATURE_DIAGNOSTIC_SOURCE_COUNT = 4

# Battery temperature extraction. IOPMPowerSource and AppleSmartBatteryPack
# publish Temperature in hundredths of a degree Celsius, e.g. 3980 -> 39.8 °C.
# On the tested iOS 27 device the outer power source omits it while the Pack
# node carries BatteryData.Temperature, so the search is deliberately broad:
#   • keys named Temperature / VirtualTemperature at ANY nesting depth (e.g.
#     inside a "BatteryData" gauge sub-dict) — EXCLUDING the AdapterDetails
#     subtree, which is the *charger's* temperature;
#   • physical Temperature keys globally before VirtualTemperature keys;
#   • source-specific centi-C hints for IOPM/Pack, with a compatibility
#     magnitude decoder only for unknown legacy diagnostics payloads.
# The 5–60 °C gate rejects boot-zero snapshots and unrelated sensor values.
# When IOPMPowerSource carries no temperature at all, the battery loop falls
# back through AppleSmartBatteryPack, AppleSmartBattery, and GasGauge on the
# same diagnostics relay.
_TEMP_KEY_NAMES = ("temperature", "virtualtemperature")
_TEMP_EXCLUDED_SUBTREES = (
    "adapterdetails",
    "acadapter",
    "charger",
    "chargerdata",
    "poweradapter",
    "usbpd",
)
_TEMP_SOURCE_DESCRIPTOR_KEYS = (
    "class",
    "ioclass",
    "name",
    "sensor",
    "source",
    "type",
)
_TEMP_NON_BATTERY_MARKERS = (
    "adapter",
    "ambient",
    "camera",
    "charger",
    "cpu",
    "display",
    "gpu",
    "logicboard",
    "skin",
    "soc",
)
_TEMP_MIN_C = 5.0
_TEMP_MAX_C = 60.0

# Newer iOS releases may omit the numeric Temperature field from both the
# classic and RemoteXPC diagnostics relay even though the on-device power
# daemons still publish the gas-gauge reading to Unified Logging.  Keep these
# patterns deliberately narrow and bind each one to the expected Apple daemon;
# accepting the same text from an arbitrary app would let unrelated log output
# spoof the launch guard.
_SYSLOG_DASD_TEMPERATURE_RE = re.compile(
    r"\bBatteryTemperatureReader\s+returning\s+value\s+(-?\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)
_SYSLOG_SYMPTOMSD_TEMPERATURE_RE = re.compile(
    r"\bbattery-temperature\s+(-?\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)


def _syslog_process_name(entry: Any) -> str:
    filename = str(getattr(entry, "filename", "") or "")
    normalized = filename.replace("\\", "/").lower()
    process = normalized.rsplit("/", 1)[-1]
    if process not in ("dasd", "symptomsd"):
        return ""
    # OsTrace normally provides the full Apple daemon path. Tolerate basename-
    # only older decoders, but reject an app-container executable named dasd.
    if "/" in normalized and normalized != f"/usr/libexec/{process}":
        return ""
    return process


def _parse_syslog_battery_temperature(
    entry: Any,
) -> Optional[tuple[float, str, float]]:
    """Decode one trusted Unified Log battery-temperature event.

    Both known log formats publish hundredths of a degree Celsius.  Unlike the
    legacy diagnostics scale ladder, this decoder is intentionally source-
    specific: ``0`` and every value outside the physical guard range are
    rejected instead of being reinterpreted using a different scale.
    """

    process = _syslog_process_name(entry)
    message = str(getattr(entry, "message", "") or "")
    if process == "dasd":
        match = _SYSLOG_DASD_TEMPERATURE_RE.search(message)
        source = "syslog:dasd"
    elif process == "symptomsd":
        match = _SYSLOG_SYMPTOMSD_TEMPERATURE_RE.search(message)
        source = "syslog:symptomsd"
    else:
        return None
    if match is None:
        return None
    raw = _num(match.group(1))
    if raw is None or not math.isfinite(raw):
        return None
    celsius = raw / 100.0
    if not (_TEMP_MIN_C <= celsius <= _TEMP_MAX_C):
        return None
    return round(celsius, 2), source, raw


class _BatteryTemperatureSyslogMonitor:
    """Own one os_trace stream and expose each temperature event once.

    The queue is for guard decisions: consuming an item removes it, so a sparse
    log entry can never be replayed by the 1 Hz loop to fabricate two
    consecutive threshold hits.  ``latest_reading`` is separate and is used
    only after launch to keep the telemetry card populated between log events.
    """

    def __init__(self, provider: Any, *, service: Any = None) -> None:
        if service is None:
            from pymobiledevice3.services.os_trace import OsTraceService

            try:
                service = OsTraceService(lockdown=provider)
            except TypeError:
                # Keep compatibility with older supported pymobiledevice3 9.x
                # builds whose base-service constructor was positional-only.
                service = OsTraceService(provider)
        self._service = service
        self._queue: asyncio.Queue = asyncio.Queue(
            maxsize=_SYSLOG_TEMPERATURE_QUEUE_SIZE
        )
        self._task: Optional[asyncio.Task] = None
        self._last_received_at: Optional[float] = None
        self._started_at_epoch: Optional[float] = None
        self._started_at_monotonic: Optional[float] = None
        self._latest: Optional[tuple[float, str, dict]] = None
        self.error: Optional[str] = None

    @property
    def healthy(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def latest_reading(self) -> Optional[tuple[float, str, dict]]:
        return self._latest

    @property
    def stale(self) -> bool:
        anchor = self._last_received_at
        if anchor is None:
            anchor = self._started_at_monotonic
        return anchor is not None and (
            time.monotonic() - anchor > _SYSLOG_TEMPERATURE_STALE_SEC
        )

    async def start(self) -> "_BatteryTemperatureSyslogMonitor":
        # Used as a second line of defence on older pymobiledevice3 builds that
        # cannot disable historical replay through StreamFlags.
        self._started_at_epoch = time.time()
        self._started_at_monotonic = time.monotonic()
        await asyncio.wait_for(
            self._service.connect(),
            timeout=_SYSLOG_TEMPERATURE_CONNECT_TIMEOUT_SEC,
        )
        self._task = asyncio.create_task(self._run())
        # Give the task a chance to enter syslog() before the caller starts a
        # potentially slow DVT/RSD handshake.
        await asyncio.sleep(0)
        return self

    def _accept_entry(
        self, entry: Any, *, received_at: Optional[float] = None
    ) -> bool:
        timestamp = getattr(entry, "timestamp", None)
        if self._started_at_epoch is not None and isinstance(timestamp, datetime):
            try:
                if timestamp.timestamp() < self._started_at_epoch:
                    return False
            except (OSError, OverflowError, ValueError):
                return False
        parsed = _parse_syslog_battery_temperature(entry)
        if parsed is None:
            return False
        temperature_c, source, _raw = parsed
        now = time.monotonic() if received_at is None else float(received_at)
        if (
            self._last_received_at is not None
            and now - self._last_received_at <= _SYSLOG_TEMPERATURE_DEDUPE_SEC
        ):
            return False
        self._last_received_at = now
        reading = (temperature_c, source, {"temp_c": temperature_c, "temp_at": time.time()})
        self._latest = reading
        if self._queue.full():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self._queue.put_nowait(reading)
        return True

    async def _run(self) -> None:
        try:
            try:
                from pymobiledevice3.services.os_trace import (
                    OS_TRACE_RELAY_STREAM_FLAGS_DEFAULT,
                    OsActivityStreamFlag,
                )

                live_flags = int(OS_TRACE_RELAY_STREAM_FLAGS_DEFAULT) & ~int(
                    OsActivityStreamFlag.HISTORICAL
                )
                entries = self._service.syslog(pid=-1, stream_flags=live_flags)
            except (AttributeError, ImportError, TypeError):
                # Older supported 9.x builds may not expose stream_flags. The
                # timestamp gate in _accept_entry still rejects replayed rows.
                entries = self._service.syslog(pid=-1)
            async for entry in entries:
                self._accept_entry(entry)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            _status(f"battery: Unified Log temperature stream stopped: {self.error}")

    def take_nowait(self) -> Optional[tuple[float, str, dict]]:
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    def discard_pending(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    async def close(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await _close_async(self._service)


async def _open_temperature_syslog_monitor(
    provider: Any,
) -> _BatteryTemperatureSyslogMonitor:
    monitor = _BatteryTemperatureSyslogMonitor(provider)
    try:
        return await monitor.start()
    except BaseException:
        await monitor.close()
        raise


def _select_temperature_reading(
    diagnostics_reading: Optional[tuple[float, str, dict]],
    syslog_monitor: Optional[_BatteryTemperatureSyslogMonitor],
) -> tuple[Optional[tuple[float, str, dict]], bool]:
    """Choose Diagnostics first, then one fresh Unified Log event.

    The boolean is true when the log stream is healthy but has no new event.
    That is an event-source idle period, not a missing periodic sample, and
    therefore must not reset the guard's consecutive-hit counter.
    """

    if diagnostics_reading is not None:
        if syslog_monitor is not None:
            syslog_monitor.discard_pending()
        return diagnostics_reading, False
    if syslog_monitor is None:
        return None, False
    reading = syslog_monitor.take_nowait()
    if reading is not None:
        return reading, False
    return None, syslog_monitor.healthy and not bool(
        getattr(syslog_monitor, "stale", False)
    )


def _iter_named_temp_candidates(value: Any, wanted: str) -> Iterable[float]:
    """Yield one exact temperature-key class throughout a payload tree.

    Diagnostics payloads occasionally carry the battery gauge and the USB/PD
    adapter in the same document.  A charger's ``Temperature`` is not a battery
    reading, even when it happens to be in the plausible 5--60 C range.
    """
    if isinstance(value, dict):
        descriptors = " ".join(
            str(child).lower()
            for key, child in value.items()
            if _norm(key) in _TEMP_SOURCE_DESCRIPTOR_KEYS
            and isinstance(child, (str, int, float))
        )
        # Some diagnostics variants return a generic sensor record rather than
        # a dedicated battery record. Accept records explicitly labelled as a
        # battery, but reject CPU/GPU/skin/charger/etc. before considering a
        # top-level Temperature field.
        if "battery" not in descriptors and any(
            marker in descriptors for marker in _TEMP_NON_BATTERY_MARKERS
        ):
            return
        for key, child in value.items():
            norm = _norm(key)
            if norm in _TEMP_EXCLUDED_SUBTREES or any(
                marker in norm
                for marker in (
                    "adapter",
                    "charger",
                    "cpusensor",
                    "gpusensor",
                    "skinsensor",
                )
            ):
                continue
            if norm == wanted:
                number = _num(child)
                if number is not None:
                    yield number
                continue
            if norm in _TEMP_KEY_NAMES:
                continue
            yield from _iter_named_temp_candidates(child, wanted)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _iter_named_temp_candidates(child, wanted)


def _iter_temp_candidates(value: Any) -> Iterable[float]:
    """Yield physical Temperature values globally before virtual values."""
    for wanted in _TEMP_KEY_NAMES:
        yield from _iter_named_temp_candidates(value, wanted)


def _decode_temperature_value(
    raw: float, *, scale_hint: Optional[float] = None
) -> Optional[float]:
    """Decode one iOS temperature using a source hint when one is known.

    IOPMPowerSource and AppleSmartBatteryPack are known centi-C sources, so
    callers pass ``100`` and even cold raw values such as 500 remain 5.0 C.
    Unknown legacy payloads use a compatibility magnitude heuristic for the
    observed centi-C, deci-C, and plain-C variants.
    """
    if not math.isfinite(raw):
        return None
    if scale_hint is not None:
        if not math.isfinite(scale_hint) or scale_hint <= 0:
            return None
        celsius = raw / scale_hint
    elif not float(raw).is_integer():
        celsius = raw
    elif abs(raw) >= 1000:
        celsius = raw / 100.0
    elif abs(raw) > _TEMP_MAX_C:
        celsius = raw / 10.0
    else:
        celsius = raw
    if not (_TEMP_MIN_C <= celsius <= _TEMP_MAX_C):
        return None
    return round(celsius, 1)


def _battery_temp_c(
    info: Any, *, scale_hint: Optional[float] = None
) -> Optional[float]:
    """Battery temperature in °C from an ioregistry battery dict, or None.

    Deep-scans physical Temperature keys before VirtualTemperature keys and
    decodes centi-C, deci-C, or plain Celsius by magnitude. A zero/out-of-range
    value (boot-time snapshot, wrong field) yields None so the thermal card
    never shows a fake 0 °C.
    """
    if not isinstance(info, dict):
        return None
    for raw in _iter_temp_candidates(info):
        celsius = _decode_temperature_value(raw, scale_hint=scale_hint)
        if celsius is not None:
            return celsius
    return None


def _battery_status(info: dict) -> Optional[str]:
    # Status strings mirror the Android dumpsys mapping (parse_battery) so the
    # two platforms read identically in the UI.
    if info.get("FullyCharged"):
        return "Full"
    if info.get("IsCharging"):
        return "Charging"
    if info.get("ExternalConnected"):
        return "Not charging"
    return "Discharging"


def _battery_fields(
    info: Any, *, temperature_scale_hint: Optional[float] = None
) -> Optional[dict]:
    """Extract the unified battery fields from a diagnostics get_battery() dict.

    Returns ``None`` when no usable reading is present so the streamer never
    emits an all-empty battery object (which would light the card with blanks).
    """
    if not isinstance(info, dict):
        return None
    level = _num(info.get("CurrentCapacity"))
    current = _num(info.get("Amperage"))
    if current is None:
        current = _num(info.get("InstantAmperage"))
    voltage = _num(info.get("Voltage"))
    if voltage is None:
        voltage = _num(info.get("AppleRawBatteryVoltage"))
    temp_c = _battery_temp_c(info, scale_hint=temperature_scale_hint)
    if level is None and current is None and voltage is None and temp_c is None:
        return None
    fields = {
        "level_percent": int(level) if level is not None else None,
        "current_ma": current,
        "voltage_mv": voltage,
        "status": _battery_status(info),
    }
    # Only include temp_c when a plausible reading exists, so a temperature-less
    # payload yields the same dict it always has (no bogus None temp key).
    if temp_c is not None:
        fields["temp_c"] = temp_c
    return fields


async def _open_diagnostics(provider):
    """Construct DiagnosticsService across supported pymobiledevice3 9.x APIs."""
    from pymobiledevice3.services.diagnostics import DiagnosticsService

    last_error: Optional[Exception] = None
    for attempt in (
        lambda: DiagnosticsService(lockdown=provider),
        lambda: DiagnosticsService(provider),
    ):
        try:
            return attempt()
        except TypeError as exc:
            last_error = exc
            continue
    if last_error is not None:
        raise last_error
    raise RuntimeError("DiagnosticsService constructor rejected service provider")


async def _query_battery_temperature(
    diag,
    *,
    timeout_sec: float = _BATTERY_TIMEOUT_SEC,
    probe_status: Optional[dict] = None,
) -> Optional[tuple[float, str, dict]]:
    """Query the ordered battery-temperature source ladder once.

    The returned tuple is ``(temperature_c, source, unified_battery_fields)``.
    Every stage is independently optional: an IOPMPowerSource query failure
    must not prevent AppleSmartBatteryPack, AppleSmartBattery, or GasGauge from
    providing the reading. Charger/adapter values are rejected by
    :func:`_battery_temp_c`.
    """

    timeout = max(float(timeout_sec), 0.001)
    deadline = asyncio.get_running_loop().time() + timeout

    def source_timeout(sources_left: int) -> float:
        # Divide the remaining total probe budget among all untried sources.
        # Fast sources donate their unused share to later fallbacks, while a
        # stuck upper source can never starve AppleSmartBatteryPack/GasGauge.
        remaining = max(deadline - asyncio.get_running_loop().time(), 0.001)
        return max(remaining / max(int(sources_left), 1), 0.001)
    if probe_status is not None:
        probe_status.clear()
        probe_status.update({"attempted": 0, "responses": 0, "failures": []})

    def attempted() -> None:
        if probe_status is not None:
            probe_status["attempted"] += 1

    def responded() -> None:
        if probe_status is not None:
            probe_status["responses"] += 1

    def failed(source: str, exc: Exception) -> None:
        if probe_status is not None:
            probe_status["failures"].append(
                f"{source}: {type(exc).__name__}: {exc}"
            )

    iopm = None
    fields: Optional[dict] = None
    attempted()
    try:
        iopm = await asyncio.wait_for(
            diag.get_battery(),
            timeout=source_timeout(_BATTERY_TEMPERATURE_DIAGNOSTIC_SOURCE_COUNT),
        )
        responded()
        fields = _battery_fields(iopm, temperature_scale_hint=100.0)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        failed("IOPMPowerSource", exc)
        _status(f"battery: IOPMPowerSource query failed: {exc}")

    temp_c = _battery_temp_c(iopm, scale_hint=100.0)
    if temp_c is not None:
        result = dict(fields or {})
        result["temp_c"] = temp_c
        return temp_c, "IOPMPowerSource", result

    attempted()
    try:
        pack = await asyncio.wait_for(
            diag.ioregistry(ioclass="AppleSmartBatteryPack"),
            timeout=source_timeout(3),
        )
        responded()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        pack = None
        failed("AppleSmartBatteryPack", exc)
        _status(f"battery: AppleSmartBatteryPack query failed: {exc}")
    temp_c = _battery_temp_c(pack, scale_hint=100.0)
    if temp_c is not None:
        result = dict(
            fields
            or _battery_fields(pack, temperature_scale_hint=100.0)
            or {}
        )
        result["temp_c"] = temp_c
        return temp_c, "AppleSmartBatteryPack", result

    attempted()
    try:
        smart = await asyncio.wait_for(
            diag.ioregistry(ioclass="AppleSmartBattery"),
            timeout=source_timeout(2),
        )
        responded()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        smart = None
        failed("AppleSmartBattery", exc)
        _status(f"battery: AppleSmartBattery query failed: {exc}")
    temp_c = _battery_temp_c(smart)
    if temp_c is not None:
        result = dict(fields or _battery_fields(smart) or {})
        result["temp_c"] = temp_c
        return temp_c, "AppleSmartBattery", result

    attempted()
    try:
        gasgauge = await asyncio.wait_for(
            diag.info("GasGauge"), timeout=source_timeout(1)
        )
        responded()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        gasgauge = None
        failed("GasGauge", exc)
        _status(f"battery: GasGauge query failed: {exc}")
    temp_c = _battery_temp_c(gasgauge)
    if temp_c is not None:
        result = dict(fields or _battery_fields(gasgauge) or {})
        result["temp_c"] = temp_c
        return temp_c, "GasGauge", result
    return None


async def _battery_loop(service, state: dict) -> None:
    """Poll battery telemetry via the diagnostics relay over the shared RSD.

    Best effort: a failure here must not stop CPU/memory/FPS/GPU streaming, so
    swallow errors after reporting them. The diagnostics service shares the one
    RemoteServiceDiscovery connection with sysmon/graphics, exactly as Xcode
    runs Instruments and the battery gauge together.
    """
    try:
        from pymobiledevice3.services.diagnostics import DiagnosticsService
    except Exception as exc:  # pragma: no cover - import guard
        _status(f"battery instrument unavailable (import): {exc}")
        return
    # The base service ctor takes the lockdown/RSD provider; the keyword name has
    # been stable as `lockdown`, but fall back to positional in case a build
    # renamed it, so a signature drift disables battery gracefully rather than
    # for no reason.
    diag = None
    for attempt in (
        lambda: DiagnosticsService(lockdown=service),
        lambda: DiagnosticsService(service),
    ):
        try:
            diag = attempt()
            break
        except TypeError:
            continue
        except Exception as exc:
            _status(f"battery instrument unavailable: {exc}")
            return
    if diag is None:
        _status("battery instrument unavailable: DiagnosticsService ctor rejected")
        return

    temperature_syslog: Optional[_BatteryTemperatureSyslogMonitor] = None
    try:
        temperature_syslog = await _open_temperature_syslog_monitor(service)
        _status("battery: Unified Log temperature fallback ready over RSD")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _status(f"battery: Unified Log temperature fallback unavailable: {exc}")

    first = True
    # Temperature-source resolution ladder, decided once per session:
    #   1. IOPMPowerSource (get_battery) — the documented Temperature key.
    #   2. AppleSmartBatteryPack — BatteryData.Temperature on newer devices.
    #   3. AppleSmartBattery ioregistry entry.
    #   4. The legacy "GasGauge" diagnostics request (idevicediagnostics'
    #      classic gauge dump, which carries Temperature on many builds).
    # Live-probed on the connected iPhone 15 Pro Max / iOS 27 beta: stage 1
    # omits temperature, while stage 2 publishes BatteryData.Temperature=2709
    # (27.09 C) and VirtualTemperature=2700. The outer AppleSmartBattery node
    # only contains stale boot averages, which the exact-key decoder ignores.
    # Whichever stage first yields a plausible reading becomes the session's
    # source; if every stage answers WITHOUT one, a single diagnostic logs all
    # key sets and probing stops ("none"). Transient query failures leave the
    # ladder undecided so it retries next poll.
    temp_source: Optional[str] = None
    # None=undecided | "iopm"|"pack"|"smart"|"gasgauge"|"none"
    try:
        while True:
            try:
                info = await asyncio.wait_for(
                    diag.get_battery(), timeout=_BATTERY_TIMEOUT_SEC
                )
                fields = (
                    _battery_fields(info, temperature_scale_hint=100.0) or {}
                )
                if "temp_c" in fields and temp_source is None:
                    temp_source = "iopm"
                if "temp_c" not in fields and temp_source != "none":
                    temp_c = None
                    pack = smart = gasgauge = None
                    pack_ok = smart_ok = gasgauge_ok = False
                    if temp_source in (None, "pack"):
                        try:
                            pack = await asyncio.wait_for(
                                diag.ioregistry(ioclass="AppleSmartBatteryPack"),
                                timeout=_BATTERY_TIMEOUT_SEC,
                            )
                            pack_ok = True
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            if temp_source is None:
                                _status(
                                    "battery: AppleSmartBatteryPack query failed: "
                                    f"{exc}"
                                )
                        temp_c = _battery_temp_c(pack, scale_hint=100.0)
                        if temp_c is not None and temp_source is None:
                            temp_source = "pack"
                            _status(
                                "battery: temperature via AppleSmartBatteryPack "
                                "ioregistry"
                            )
                    if temp_source in (None, "smart"):
                        try:
                            smart = await asyncio.wait_for(
                                diag.ioregistry(ioclass="AppleSmartBattery"),
                                timeout=_BATTERY_TIMEOUT_SEC,
                            )
                            smart_ok = True
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            if temp_source is None:
                                _status(
                                    "battery: AppleSmartBattery query failed: "
                                    f"{exc}"
                                )
                        temp_c = _battery_temp_c(smart)
                        if temp_c is not None and temp_source is None:
                            temp_source = "smart"
                            _status(
                                "battery: temperature via AppleSmartBattery "
                                "ioregistry"
                            )
                    if temp_c is None and temp_source in (None, "gasgauge"):
                        try:
                            gasgauge = await asyncio.wait_for(
                                diag.info("GasGauge"), timeout=_BATTERY_TIMEOUT_SEC
                            )
                            gasgauge_ok = True
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            if temp_source is None:
                                _status(f"battery: GasGauge query failed: {exc}")
                        temp_c = _battery_temp_c(gasgauge)
                        if temp_c is not None and temp_source is None:
                            temp_source = "gasgauge"
                            _status("battery: temperature via GasGauge diagnostics")
                    if temp_c is not None:
                        fields["temp_c"] = temp_c
                    elif (
                        temp_source is None
                        and pack_ok
                        and smart_ok
                        and gasgauge_ok
                    ):
                        # Every stage answered and none carries a temperature —
                        # log the full evidence once and stop probing.
                        temp_source = "none"

                        def _keys(value: Any) -> Any:
                            if isinstance(value, dict):
                                return sorted(value.keys())
                            return repr(value)[:200]

                        _status(
                            "battery: no live temperature on this device; "
                            f"IOPMPowerSource keys={_keys(info)}; "
                            f"AppleSmartBatteryPack keys={_keys(pack)}; "
                            f"AppleSmartBattery keys={_keys(smart)}; "
                            f"GasGauge={_keys(gasgauge)}"
                        )
                if (
                    "temp_c" not in fields
                    and temperature_syslog is not None
                ):
                    # A log event is a fresh sample; between events retain only
                    # the latest value for telemetry display.  The launch guard
                    # consumes the queue separately and never reuses latest for
                    # its consecutive-hit decision.
                    syslog_reading = temperature_syslog.take_nowait()
                    if syslog_reading is None:
                        syslog_reading = temperature_syslog.latest_reading
                    if syslog_reading is not None:
                        _temperature_c, source, syslog_fields = syslog_reading
                        fields.update(syslog_fields)
                        if first:
                            _status(f"battery: temperature via {source}")
                if fields:
                    _update_battery(state, fields)
                    if first:
                        first = False
                        _status("battery: first reading")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _status(f"battery poll failed: {exc}")
            await asyncio.sleep(_BATTERY_INTERVAL_SEC)
    finally:
        # get_battery() lazily opens a dedicated diagnostics socket that the
        # outer RSD close() does not track, so release it explicitly on
        # cancellation rather than leaking it until the subprocess exits.
        if temperature_syslog is not None:
            await temperature_syslog.close()
        try:
            await diag.close()
        except Exception:
            pass


def _battery_enabled() -> bool:
    # Default to enabled; opt out with MPI_IOS_BATTERY=0 if a device/iOS
    # combo dislikes the diagnostics relay. Never let an env-read hiccup crash
    # the streamer before it starts.
    try:
        raw = os.environ.get("MPI_IOS_BATTERY", "").strip().lower()
    except Exception:
        return True
    return raw not in ("0", "false", "off", "no")


def _fps_enabled() -> bool:
    # Default to enabled, and never let an env-read hiccup disable FPS or, worse,
    # crash the streamer before it starts (this used to NameError on a missing
    # `import os`, which killed the whole stream and forced single-shot sampling).
    try:
        raw = os.environ.get("MPI_IOS_FPS", "").strip().lower()
    except Exception:
        return True
    return raw not in ("0", "false", "off", "no")


def _kdebug_fps_enabled() -> bool:
    # OPT-IN (default OFF). On-device testing showed the kdebug display-commit
    # counter (eventid 0x31800318) staying smooth and high while the game
    # visibly stuttered and froze — on this hardware it evidently fires per
    # display REFRESH (ProMotion + touch keeps the panel fast) rather than per
    # content commit, so it cannot serve as the game FPS. The Graphics-service
    # CoreAnimationFramesPerSecond also measures the whole display and cannot
    # establish the selected application's frame rate.
    # Set MPI_IOS_KDEBUG_FPS=1 to re-enable the tap for
    # experimentation — its one-shot "event mix" diagnostic in the engine log
    # identifies the device's actual event shape.
    try:
        raw = os.environ.get("MPI_IOS_KDEBUG_FPS", "").strip().lower()
    except Exception:
        return False
    return raw in ("1", "true", "on", "yes")


def _validate_guard_pmd3_version() -> str:
    """Return the installed version, rejecting anything outside major 9.

    The guard relies on the async DVT/diagnostics APIs and launch semantics
    stabilized in pymobiledevice3 9.x.  Normal streaming remains untouched;
    this check is intentionally guard-only (including on macOS).
    """
    try:
        from importlib.metadata import version

        raw = version("pymobiledevice3").strip()
        match = re.search(r"(?<!\d)(\d+)\.(\d+)", raw)
        if match is None:
            raise ValueError(f"unparseable version {raw!r}")
        parsed = (int(match.group(1)), int(match.group(2)))
    except Exception as exc:
        raise RuntimeError(
            "unsupported_pymobiledevice3: unable to determine installed version"
        ) from exc
    if not ((9, 13) <= parsed < (10, 0)):
        raise RuntimeError(
            "unsupported_pymobiledevice3: temperature guard requires "
            f"pymobiledevice3 >=9.13,<10 (found {raw})"
        )
    return raw


async def _open_dvt(provider):
    """Enter a DVT provider, cleaning a partially-opened provider on failure."""
    from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider

    dvt = DvtProvider(provider)
    try:
        entered = await _maybe_await(dvt.__aenter__())
        return entered if entered is not None else dvt
    except Exception:
        await _close_async(dvt)
        raise


async def _prepare_guard_services(
    udid: str,
    tunneld: Optional[tuple[str, int]],
    rsd: Optional[tuple[str, int]],
) -> dict:
    """Prepare DVT and Diagnostics without launching the target app.

    usbmux/lockdown is always attempted first.  It is enough for classic
    diagnostics on every supported iOS and also for DVT on pre-iOS-17 devices.
    If DVT is unavailable there (or usbmux itself cannot see a network device),
    the prepared connection falls back to the existing RSD/tunneld path.
    """
    resources: dict = {
        "usbmux": None,
        "rsd": None,
        "dvt": None,
        "diagnostics": None,
        "temperature_syslog": None,
        "dvt_transport": "",
        "diagnostics_transport": "",
        "temperature_syslog_transport": "",
    }
    try:
        try:
            resources["usbmux"] = await _connect_usbmux(udid)
            _status("guard: usbmux/lockdown connected")
            try:
                resources["temperature_syslog"] = (
                    await _open_temperature_syslog_monitor(resources["usbmux"])
                )
                resources["temperature_syslog_transport"] = "usbmux"
                _status("guard: Unified Log temperature fallback ready over usbmux")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _status(
                    "guard: Unified Log temperature over usbmux unavailable "
                    f"({type(exc).__name__}: {exc})"
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _status(
                "guard: usbmux connect failed "
                f"({type(exc).__name__}: {exc}); trying RSD/tunneld"
            )

        if resources["usbmux"] is not None:
            try:
                resources["dvt"] = await _open_dvt(resources["usbmux"])
                resources["dvt_transport"] = "usbmux"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _status(
                    "guard: DVT over usbmux unavailable "
                    f"({type(exc).__name__}: {exc}); trying RSD/tunneld"
                )

        if resources["dvt"] is None:
            resources["rsd"] = await _connect_rsd(udid, tunneld, rsd)
            resources["dvt"] = await _open_dvt(resources["rsd"])
            resources["dvt_transport"] = "rsd"

        if (
            resources["temperature_syslog"] is None
            and resources["rsd"] is not None
        ):
            try:
                resources["temperature_syslog"] = (
                    await _open_temperature_syslog_monitor(resources["rsd"])
                )
                resources["temperature_syslog_transport"] = "rsd"
                _status("guard: Unified Log temperature fallback ready over RSD")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _status(
                    "guard: Unified Log temperature over RSD unavailable "
                    f"({type(exc).__name__}: {exc})"
                )

        # iOS 18+ exposes its richer battery snapshot through the RemoteXPC
        # diagnostics shim (the same path used by Appium's advanced battery
        # endpoint), so prefer RSD whenever the DVT tunnel is already open.
        # Pre-iOS-17 devices and tunnel-less fallbacks still use classic usbmux.
        providers = []
        if resources["rsd"] is not None:
            providers.append(("rsd", resources["rsd"]))
        if resources["usbmux"] is not None:
            providers.append(("usbmux", resources["usbmux"]))
        last_error: Optional[Exception] = None
        for transport, provider in providers:
            try:
                resources["diagnostics"] = await _open_diagnostics(provider)
                resources["diagnostics_transport"] = transport
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                _status(
                    f"guard: DiagnosticsService over {transport} unavailable: {exc}"
                )
        if resources["diagnostics"] is None and resources["rsd"] is None:
            try:
                resources["rsd"] = await _connect_rsd(udid, tunneld, rsd)
                resources["diagnostics"] = await _open_diagnostics(resources["rsd"])
                resources["diagnostics_transport"] = "rsd"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                _status(f"guard: DiagnosticsService RSD fallback unavailable: {exc}")
        if resources["diagnostics"] is None and not (
            resources["temperature_syslog"] is not None
            and resources["temperature_syslog"].healthy
        ):
            raise RuntimeError(
                "temperature_unavailable: neither DiagnosticsService nor the "
                "Unified Log temperature stream could be prepared"
            ) from last_error
        return resources
    except BaseException:
        await _close_guard_services(resources)
        raise


async def _close_guard_services(resources: dict) -> None:
    """Close every guard-owned socket/channel exactly once, in dependency order."""
    seen: set[int] = set()
    for key in (
        "process_control",
        "temperature_syslog",
        "diagnostics",
        "dvt",
        "rsd",
        "usbmux",
    ):
        obj = resources.get(key)
        if obj is None or id(obj) in seen:
            continue
        seen.add(id(obj))
        await _close_async(obj)


async def _guard_battery_loop(
    diag,
    state: dict,
    temperature_syslog: Optional[_BatteryTemperatureSyslogMonitor] = None,
) -> None:
    """Keep battery fields fresh after the guard has promoted to metrics."""
    while True:
        try:
            diagnostics_reading = (
                await _query_battery_temperature(diag) if diag is not None else None
            )
            reading, _waiting_for_syslog = _select_temperature_reading(
                diagnostics_reading, temperature_syslog
            )
            if reading is not None:
                _temperature_c, _source, fields = reading
                _update_battery(state, fields, merge=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _status(f"battery poll failed: {exc}")
        await asyncio.sleep(_BATTERY_INTERVAL_SEC)


async def _launch_guard_target(dvt, bundle_id: str) -> tuple[Any, int]:
    """Attach to the target, launching only when it is not already running.

    The temperature guard must not reset an app the user already has open.  Use
    the same prepared DVT ProcessControl connection to inspect the bundle first;
    only an unambiguous "not running" result permits a launch.
    """
    from pymobiledevice3.services.dvt.instruments.process_control import ProcessControl

    control = ProcessControl(dvt)

    async def lookup_running_pid() -> int:
        """Return 0 only for a definite absent process, never for an error."""
        lookup = getattr(control, "process_identifier_for_bundle_identifier", None)
        if not callable(lookup):
            raise RuntimeError("ProcessControl has no process identifier lookup")
        pid = await asyncio.wait_for(
            _maybe_await(lookup(bundle_id)), timeout=_GUARD_LAUNCH_TIMEOUT_SEC
        )
        if pid is None:
            return 0
        if isinstance(pid, bool):
            raise RuntimeError(f"process lookup returned invalid pid {pid!r}")
        try:
            numeric_pid = int(pid)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"process lookup returned invalid pid {pid!r}") from exc
        if numeric_pid == 0:
            return 0
        if numeric_pid < 0:
            raise RuntimeError(f"process lookup returned invalid pid {pid!r}")
        return numeric_pid

    try:
        # ProcessControl is a DtxService in current pymobiledevice3 releases.
        # Constructing it does not open its channel: launch() otherwise raises
        # ``not connected - use async with or await connect()`` exactly when the
        # temperature threshold is reached.  Keep the getattr/_maybe_await seam
        # for older supported 9.x builds whose connector may be absent or sync.
        connector = getattr(control, "connect", None)
        if callable(connector):
            await _maybe_await(connector())
        existing_pid = await lookup_running_pid()
        if existing_pid:
            # The selected app may exit/restart (and its PID may be reused)
            # between the first read and the sysmon hand-off. Re-read the same
            # bundle immediately before attaching. A vanished process follows
            # the absent-app launch path; a lookup error must never launch.
            existing_pid = await lookup_running_pid()
            if existing_pid:
                return control, existing_pid

        try:
            pid = await asyncio.wait_for(
                _maybe_await(control.launch(bundle_id, kill_existing=False)),
                timeout=_GUARD_LAUNCH_TIMEOUT_SEC,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # A user or the OS can launch the target between the read-only
            # lookup and our launch attempt.  Re-check once; attach only when
            # a concrete PID is now available, otherwise preserve the launch
            # failure instead of guessing that the app is running.
            raced_pid = await lookup_running_pid()
            if raced_pid:
                return control, raced_pid
            raise
        try:
            numeric_pid = int(pid)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"launch returned invalid pid {pid!r}") from exc
        if numeric_pid <= 0:
            raise RuntimeError(f"launch returned invalid pid {numeric_pid}")
    except BaseException:
        await _close_async(control)
        raise
    return control, numeric_pid


async def _run_guard(args: argparse.Namespace) -> int:
    """Stream battery temperature, then attach or launch at a threshold."""
    preview_only = bool(getattr(args, "temperature_preview", False))
    try:
        _validate_guard_pmd3_version()
    except Exception as exc:
        _emit(
            {
                "event": "error",
                "code": "unsupported_pymobiledevice3",
                "error": str(exc),
            }
        )
        return 5
    if not args.bundle_id:
        _emit({"event": "error", "code": "invalid_guard_config", "error": "bundle id required"})
        return 2
    threshold_c = float(args.threshold_c)
    if not math.isfinite(threshold_c) or not (_TEMP_MIN_C <= threshold_c <= _TEMP_MAX_C):
        _emit(
            {
                "event": "error",
                "code": "invalid_guard_config",
                "error": f"threshold must be between {_TEMP_MIN_C:.1f} and {_TEMP_MAX_C:.1f} C",
            }
        )
        return 2
    required_hits = int(args.required_hits)
    if required_hits < 1:
        _emit(
            {
                "event": "error",
                "code": "invalid_guard_config",
                "error": "required hits must be at least 1",
            }
        )
        return 2

    resources: dict = {}
    state: dict = {
        "fps": None,
        "clock": time.time,
        "fps_kdebug": None,
        "fps_kdebug_at": None,
        "kdebug_windows": deque(),
    }
    _status(
        f"guard connecting: udid={args.udid or '?'} bundle={args.bundle_id} "
        f"threshold={threshold_c:.1f} hits={required_hits} preview={preview_only}"
    )
    try:
        resources = await _prepare_guard_services(args.udid, args.tunneld, args.rsd)
        diag = resources["diagnostics"]
        dvt = resources["dvt"]
        temperature_syslog = resources.get("temperature_syslog")
        _emit(
            {
                "event": "guard_ready",
                "udid": args.udid,
                "bundle_id": args.bundle_id,
                "dvt_transport": resources["dvt_transport"],
                "diagnostics_transport": resources["diagnostics_transport"],
                "temperature_syslog_transport": resources.get(
                    "temperature_syslog_transport", ""
                ),
                "threshold_c": threshold_c,
                "required_hits": required_hits,
            }
        )

        loop = asyncio.get_running_loop()
        first_deadline: Optional[float] = loop.time() + max(
            float(args.temperature_timeout_sec), 0.001
        )
        interval_sec = max(float(args.interval_ms) / 1000.0, 0.05)
        hits = 0
        saw_valid_temperature = False
        consecutive_probe_failures = 0
        trigger_temp: Optional[float] = None
        trigger_source = ""
        while preview_only or trigger_temp is None:
            poll_started = loop.time()
            remaining = (
                first_deadline - poll_started if first_deadline is not None else None
            )
            reading: Optional[tuple[float, str, dict]] = None
            waiting_for_syslog = False
            if remaining is not None and remaining <= 0:
                # An event may have landed between the previous poll and the
                # deadline. Consume it once before declaring the source absent.
                reading, waiting_for_syslog = _select_temperature_reading(
                    None, temperature_syslog
                )
                if reading is None:
                    _emit(
                        {
                            "event": "error",
                            "code": "temperature_unavailable",
                            "error": "temperature_unavailable: no valid battery "
                            "temperature "
                            f"within {float(args.temperature_timeout_sec):.0f}s",
                        }
                    )
                    return 3
            probe_status: dict = {}
            post_valid_probe_budget = _GUARD_POST_VALID_PROBE_TIMEOUT_SEC
            if reading is None and (remaining is None or remaining > 0):
                diagnostics_reading = None
                if diag is not None:
                    try:
                        diagnostics_reading = await asyncio.wait_for(
                            _query_battery_temperature(
                                diag,
                                timeout_sec=min(_BATTERY_TIMEOUT_SEC, remaining)
                                if remaining is not None
                                else post_valid_probe_budget,
                                probe_status=probe_status,
                            ),
                            timeout=remaining
                            if remaining is not None
                            else (
                                _GUARD_POST_VALID_PROBE_TIMEOUT_SEC
                                + _GUARD_PROBE_COMPLETION_GRACE_SEC
                            ),
                        )
                    except asyncio.CancelledError:
                        raise
                    except (asyncio.TimeoutError, TimeoutError):
                        diagnostics_reading = None
                    except Exception as exc:
                        _status(f"guard: temperature poll failed: {exc}")
                        diagnostics_reading = None
                reading, waiting_for_syslog = _select_temperature_reading(
                    diagnostics_reading, temperature_syslog
                )

            if reading is None and waiting_for_syslog:
                # Unified Log is event-driven and may be quiet for many poll
                # intervals. No new event is not a contradictory sample, so
                # preserve hits and wait for the next unique event.
                consecutive_probe_failures = 0
            elif reading is None:
                hits = 0
                transport_failed = (
                    saw_valid_temperature
                    and int(probe_status.get("responses", 0)) == 0
                )
                source_stalled = (
                    saw_valid_temperature
                    and temperature_syslog is not None
                    and bool(getattr(temperature_syslog, "stale", False))
                    and int(probe_status.get("responses", 0)) > 0
                )
                source_failed = transport_failed or source_stalled
                if source_failed:
                    consecutive_probe_failures += 1
                else:
                    # At least one Diagnostics source answered. It may simply
                    # omit temperature on this poll; that is a reset, not a
                    # transport disconnect.
                    consecutive_probe_failures = 0
                _emit(
                    {
                        "event": "temperature_reset",
                        "sampled_at": datetime.now(timezone.utc).isoformat(),
                        "reason": (
                            "source_stalled"
                            if source_stalled
                            else "probe_failed"
                            if transport_failed
                            else "unavailable"
                        ),
                        "hits": 0,
                    }
                )
                if (
                    consecutive_probe_failures
                    >= _GUARD_MAX_CONSECUTIVE_PROBE_FAILURES
                ):
                    failures = probe_status.get("failures") or []
                    detail = f"; last probe: {failures[-1]}" if failures else ""
                    if source_stalled:
                        _emit(
                            {
                                "event": "error",
                                "code": "temperature_unavailable",
                                "error": "temperature_unavailable: Unified Log "
                                "produced no fresh battery temperature for "
                                f"more than {_SYSLOG_TEMPERATURE_STALE_SEC:.0f}s",
                            }
                        )
                        return 3
                    _emit(
                        {
                            "event": "error",
                            "code": "device_disconnected",
                            "error": "device_disconnected: temperature sources had "
                            "no successful response for "
                            f"{consecutive_probe_failures} consecutive polls"
                            f"{detail}",
                        }
                    )
                    return 6
            else:
                consecutive_probe_failures = 0
                temperature_c, source, fields = reading
                if not math.isfinite(temperature_c) or not (
                    _TEMP_MIN_C <= temperature_c <= _TEMP_MAX_C
                ):
                    hits = 0
                    _emit(
                        {
                            "event": "temperature_reset",
                            "sampled_at": datetime.now(timezone.utc).isoformat(),
                            "reason": "invalid",
                            "hits": 0,
                        }
                    )
                else:
                    # The 45-second limit applies only to acquiring the FIRST
                    # valid battery reading. Afterwards low temperatures wait
                    # indefinitely, while repeated all-source transport failures
                    # are bounded by the liveness policy above.
                    first_deadline = None
                    saw_valid_temperature = True
                    _update_battery(state, fields)
                    # Preview mode is deliberately incapable of accumulating a
                    # trigger.  Do not rely on an unreachable sentinel
                    # threshold: battery diagnostics must remain read-only for
                    # an arbitrarily long idle preview.
                    hits = (
                        0
                        if preview_only
                        else hits + 1
                        if temperature_c >= threshold_c
                        else 0
                    )
                    sampled_at = datetime.now(timezone.utc).isoformat()
                    _emit(
                        {
                            "event": "temperature",
                            "temperature_c": temperature_c,
                            "sampled_at": sampled_at,
                            "source": source,
                            "hits": hits,
                            "threshold_c": threshold_c,
                        }
                    )
                    if not preview_only and hits >= required_hits:
                        trigger_temp = temperature_c
                        trigger_source = source
                        break

            if first_deadline is not None and loop.time() >= first_deadline:
                continue
            sleep_for = interval_sec - (loop.time() - poll_started)
            if sleep_for > 0:
                if first_deadline is None:
                    await asyncio.sleep(sleep_for)
                else:
                    await asyncio.sleep(
                        max(0.0, min(sleep_for, first_deadline - loop.time()))
                    )

        _emit(
            {
                "event": "triggering",
                "temperature_c": trigger_temp,
                "source": trigger_source,
                "bundle_id": args.bundle_id,
            }
        )
        try:
            control, pid = await _launch_guard_target(dvt, args.bundle_id)
            resources["process_control"] = control
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _emit(
                {
                    "event": "error",
                    "code": "launch_failed",
                    "error": f"launch_failed: {type(exc).__name__}: {exc}",
                }
            )
            return 4

        _emit(
            {
                "event": "triggered",
                "pid": pid,
                "temperature_c": trigger_temp,
                "source": trigger_source,
                "udid": args.udid,
                "bundle_id": args.bundle_id,
            }
        )

        graphics_task = (
            asyncio.create_task(_graphics_loop(dvt, state)) if _fps_enabled() else None
        )
        kdebug_task = (
            asyncio.create_task(_kdebug_fps_loop(dvt, state))
            if _fps_enabled() and _kdebug_fps_enabled()
            else None
        )
        battery_task = asyncio.create_task(
            _guard_battery_loop(diag, state, temperature_syslog)
        )
        try:
            await _sysmon_loop(dvt, pid, args.interval_ms, state)
        finally:
            for task in (graphics_task, kdebug_task, battery_task):
                if task is not None:
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
        return 0
    finally:
        await _close_guard_services(resources)


async def _run(args: argparse.Namespace) -> int:
    state: dict = {
        "fps": None,
        "clock": time.time,
        "fps_kdebug": None,
        "fps_kdebug_at": None,
        "kdebug_windows": deque(),
    }
    _status(
        f"connecting: udid={args.udid or '?'} rsd={args.rsd or 'auto'} "
        f"tunneld={args.tunneld or 'default'}"
    )
    service = await _connect_rsd(args.udid, args.tunneld, args.rsd)
    _status("connected: RemoteServiceDiscovery established")

    from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider

    try:
        async with DvtProvider(service) as dvt:
            pid = args.pid
            if pid <= 0:
                _emit({"event": "error", "error": "no pid supplied to streamer"})
                return 2

            _emit({"event": "ready", "pid": pid})
            # Graphics (FPS + GPU utilization) shares the DVT connection with
            # sysmon, and the battery poller shares the underlying RSD, exactly
            # as Xcode Instruments runs multiple instruments at once. Both are
            # opt-out (MPI_IOS_FPS=0 / MPI_IOS_BATTERY=0) in case
            # a device/iOS combo dislikes one; CPU/memory streaming keeps working
            # regardless of either.
            graphics_task = (
                asyncio.create_task(_graphics_loop(dvt, state))
                if _fps_enabled()
                else None
            )
            # The kdebug tap is EXPERIMENTAL and opt-in (default off): on the
            # tested hardware it counts display refreshes, not game frames.
            # The graphics loop is the FPS source (plus GPU utilization).
            kdebug_task = (
                asyncio.create_task(_kdebug_fps_loop(dvt, state))
                if _fps_enabled() and _kdebug_fps_enabled()
                else None
            )
            battery_task = (
                asyncio.create_task(_battery_loop(service, state))
                if _battery_enabled()
                else None
            )
            try:
                await _sysmon_loop(dvt, pid, args.interval_ms, state)
            finally:
                for task in (graphics_task, kdebug_task, battery_task):
                    if task is not None:
                        task.cancel()
                        try:
                            await task
                        except (asyncio.CancelledError, Exception):
                            pass
    finally:
        try:
            await service.close()
        except Exception:
            pass
    return 0


def _parse_addr(raw: Optional[str]) -> Optional[tuple[str, int]]:
    if not raw:
        return None
    host, sep, port = raw.rpartition(":")
    if sep and host and port.isdigit():
        return (host, int(port))
    return None


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Persistent iOS sysmon/graphics streamer")
    parser.add_argument("--udid", default="")
    parser.add_argument("--pid", type=int, default=0)
    parser.add_argument("--bundle-id", default="")
    temperature_mode = parser.add_mutually_exclusive_group()
    temperature_mode.add_argument(
        "--temperature-guard",
        action="store_true",
        help="wait for battery temperature, attach or launch, then stream metrics",
    )
    temperature_mode.add_argument(
        "--temperature-preview",
        action="store_true",
        help="stream battery temperature without ever launching an app",
    )
    parser.add_argument("--threshold-c", type=float, default=25.0)
    parser.add_argument("--required-hits", type=int, default=2)
    parser.add_argument(
        "--temperature-timeout-sec",
        type=float,
        default=_GUARD_FIRST_READING_TIMEOUT_SEC,
    )
    parser.add_argument("--tunneld", default="", help="tunneld HOST:PORT")
    parser.add_argument(
        "--rsd",
        nargs=2,
        metavar=("HOST", "PORT"),
        default=None,
        help="explicit RemoteServiceDiscovery HOST PORT (skips tunneld lookup)",
    )
    parser.add_argument("--interval-ms", type=int, default=1000)
    args = parser.parse_args(argv)

    # Keep pymobiledevice3's chatty loggers off stdout; route everything to
    # stderr so the NDJSON stream stays clean.
    logging.basicConfig(level=logging.ERROR, stream=sys.stderr)

    args.tunneld = _parse_addr(args.tunneld)
    if args.rsd is not None:
        args.rsd = (args.rsd[0], int(args.rsd[1]))

    # pymobiledevice3 requires the selector event loop on Windows (its proactor
    # path breaks raw-socket usage); mirror what its own CLI does.
    if sys.platform == "win32":
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=DeprecationWarning)
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        target = (
            _run_guard(args)
            if args.temperature_guard or args.temperature_preview
            else _run(args)
        )
        return asyncio.run(target)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        _emit({"event": "fatal", "error": f"{type(exc).__name__}: {exc}"})
        return 1


if __name__ == "__main__":
    sys.exit(main())
