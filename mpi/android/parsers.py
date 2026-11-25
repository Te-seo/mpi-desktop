from __future__ import annotations

import math
import re
from bisect import bisect_left
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..schema import BatteryStats, CpuStats, FrameStats, MemStats


_GFX_TOTAL_FRAMES = re.compile(r"Total frames rendered:\s*(\d+)")
_GFX_JANKY = re.compile(r"Janky frames:\s*(\d+)\s*\(([\d.]+)%\)")
_GFX_PERCENTILE = re.compile(r"(\d+)(?:st|nd|rd|th)\s+percentile:\s*([\d.]+)ms")
_GFX_MISSED_VSYNC = re.compile(r"Number Missed Vsync:\s*(\d+)")
_GFX_SLOW_UI = re.compile(r"Number Slow UI thread:\s*(\d+)")
_GFX_FRAME_DEADLINE = re.compile(r"Number Frame deadline missed:\s*(\d+)")
_GFX_PROFILEDATA = re.compile(
    r"---PROFILEDATA---\s*(.*?)\s*---PROFILEDATA---", re.DOTALL
)
_GFX_WINDOW = re.compile(r"^[ \t]*Window:[ \t]*([^\r\n]+)", re.MULTILINE)
_GFX_VIEW_ROOT = re.compile(
    r"^[ \t]*([^ \t\r\n][^\r\n]*?/android\.view\.ViewRootImpl@[0-9a-fA-F]+)",
    re.MULTILINE,
)


def parse_gfxinfo(text: str) -> FrameStats:
    stats = FrameStats()
    if not text:
        return stats

    count = _GFX_TOTAL_FRAMES.search(text)
    if count is not None:
        stats.total_frames = int(count.group(1))
    if m := _GFX_JANKY.search(text):
        stats.janky_frames = int(m.group(1))
        stats.jank_percent = float(m.group(2))

    # When total_frames=0 the gfxinfo summary still emits percentile lines
    # for completeness, but the values come from the upper edge of an empty
    # histogram (e.g. "50th percentile: 4950ms" on Android 16). They are
    # noise: skip them so a SurfaceFlinger fallback can fill p50/p90/...
    if stats.total_frames > 0:
        for m in _GFX_PERCENTILE.finditer(text):
            pct = int(m.group(1))
            val = float(m.group(2))
            if pct == 50:
                stats.p50_ms = val
            elif pct == 90:
                stats.p90_ms = val
            elif pct == 95:
                stats.p95_ms = val
            elif pct == 99:
                stats.p99_ms = val

    if m := _GFX_MISSED_VSYNC.search(text):
        stats.missed_vsync = int(m.group(1))
    if m := _GFX_SLOW_UI.search(text):
        stats.slow_ui_thread = int(m.group(1))
    if m := _GFX_FRAME_DEADLINE.search(text):
        stats.frame_deadline_missed = int(m.group(1))

    stats.fps_estimate = _estimate_fps(text)
    stats.raw_events, stats.raw_events_dropped = _parse_frame_events(text)
    if count is not None or stats.raw_events:
        stats.source = "android_gfxinfo_framestats"
        stats.scope = "app_window"
    return stats


def _parse_frame_events(text: str, limit: int = 4096) -> Tuple[List[Dict[str, Any]], int]:
    """Keep measured intended-vsync → completion durations, never infer frames from FPS."""
    events: List[Dict[str, Any]] = []
    seen = set()
    dropped = 0
    previous_end = 0
    for block_index, match in enumerate(_GFX_PROFILEDATA.finditer(text)):
        # Android can put per-dump GPU histograms between the window title and
        # PROFILEDATA. Only a window identifier can link frames across dumps.
        window = _GFX_WINDOW.search(text, previous_end, match.start())
        if window is None:
            window = _GFX_VIEW_ROOT.search(text, previous_end, match.start())
        stream = window.group(1).strip() if window else f"window-{block_index}"
        previous_end = match.end()
        lines = [line.strip() for line in match.group(1).splitlines() if line.strip()]
        if not lines:
            continue
        header = [part.strip() for part in lines[0].split(",")]
        try:
            flags_col = header.index("Flags")
            start_col = header.index("IntendedVsync")
            end_col = header.index("FrameCompleted")
        except ValueError:
            continue
        for line in lines[1:]:
            fields = line.split(",")
            try:
                flags, start, end = (int(fields[col]) for col in (flags_col, start_col, end_col))
            except (IndexError, ValueError):
                continue
            key = (stream, start)
            if flags != 0 or start <= 0 or end < start or end >= 2**63 - 1 or key in seen:
                continue
            seen.add(key)
            if len(events) >= limit:
                dropped += 1
                continue
            events.append({"timestamp_ns": start, "duration_ns": end - start,
                           "source": "android_gfxinfo_framestats", "scope": "app_window",
                           "stream_id": stream})
    return events, dropped


def _estimate_fps(text: str) -> Optional[float]:
    m = _GFX_PROFILEDATA.search(text)
    if not m:
        return None

    block = m.group(1).strip()
    lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
    if len(lines) < 3:
        return None

    header = [c.strip() for c in lines[0].split(",")]
    try:
        col = header.index("FrameCompleted")
    except ValueError:
        return None

    completed: list[int] = []
    for ln in lines[1:]:
        parts = ln.split(",")
        if len(parts) <= col:
            continue
        try:
            completed.append(int(parts[col]))
        except ValueError:
            continue

    if len(completed) < 2:
        return None

    span_ns = completed[-1] - completed[0]
    if span_ns <= 0:
        return None

    return (len(completed) - 1) / span_ns * 1e9


_MEM_TOTAL_PSS = re.compile(r"TOTAL PSS:\s*(\d+)")
_MEM_TOTAL_RSS = re.compile(r"TOTAL RSS:\s*(\d+)")
_MEM_APP_SUMMARY_LINE = re.compile(
    r"^\s*(Java Heap|Native Heap|Code|Stack|Graphics|Private Other|System|Unknown):\s*(\d+)",
    re.MULTILINE,
)


def parse_meminfo(text: str) -> MemStats:
    stats = MemStats()
    if not text:
        return stats

    if m := _MEM_TOTAL_PSS.search(text):
        stats.pss_total_kb = int(m.group(1))
    if m := _MEM_TOTAL_RSS.search(text):
        stats.rss_total_kb = int(m.group(1))

    for m in _MEM_APP_SUMMARY_LINE.finditer(text):
        key = m.group(1)
        val = int(m.group(2))
        if key == "Java Heap":
            stats.java_heap_kb = val
        elif key == "Native Heap":
            stats.native_heap_kb = val
        elif key == "Code":
            stats.code_kb = val
        elif key == "Stack":
            stats.stack_kb = val
        elif key == "Graphics":
            stats.graphics_kb = val

    return stats


_PROC_STATUS_VMRSS = re.compile(r"^VmRSS:\s*(\d+)\s*kB", re.MULTILINE)
_PROC_MEMINFO_TOTAL = re.compile(r"^MemTotal:\s*(\d+)\s*kB", re.MULTILINE)
_PROC_MEMINFO_AVAIL = re.compile(r"^MemAvailable:\s*(\d+)\s*kB", re.MULTILINE)
_PROC_STAT_CPU = re.compile(r"^cpu[ \t]+([^\r\n]+)$", re.MULTILINE)
_PROC_STAT_CORES = re.compile(r"^cpu(\d+)[ \t]+([^\r\n]+)$", re.MULTILINE)
_SF_LAYER_BLOCK = re.compile(r"layerName\s*=\s*(.*)")
_SF_AVG_FPS = re.compile(r"averageFPS\s*=\s*([\d.]+)")
_SF_TOTAL_FRAMES = re.compile(r"totalFrames\s*=\s*(\d+)")
_SF_FRAME_RATE = re.compile(r"^frameRate\s*=\s*([\d.]+)", re.MULTILINE)
_SF_REFRESH_RATE = re.compile(
    r"^(?:displayRefreshRate|refreshRate)\s*=\s*([\d.]+)", re.MULTILINE
)


def parse_sf_timestats_fps(text: str, bundle_id: str) -> Optional[float]:
    """Find the SurfaceView layer matching ``bundle_id`` in `dumpsys
    SurfaceFlinger --timestats -dump` output and return its averageFPS.

    Per-layer sections are delimited by new ``layerName = ...`` lines. The
    first layer whose name contains ``bundle_id`` and has an ``averageFPS``
    field wins.
    """
    if not text or not bundle_id:
        return None
    blocks = re.split(r"(?=^layerName\s*=)", text, flags=re.MULTILINE)
    for block in blocks:
        name_match = _SF_LAYER_BLOCK.search(block)
        if not name_match or bundle_id not in name_match.group(1):
            continue
        fps_match = _SF_AVG_FPS.search(block)
        if fps_match:
            try:
                return float(fps_match.group(1))
            except ValueError:
                continue
    return None



def parse_cpuinfo_total(text: str) -> Optional[float]:
    """Read Android's measured TOTAL row without confusing per-process CPU."""
    matches = re.findall(r"^\s*(\d+(?:\.\d+)?)%\s+TOTAL\s*:", text, re.MULTILINE)
    if len(matches) != 1:
        return None
    value = float(matches[0])
    return value if math.isfinite(value) and 0 <= value <= 100 else None


def _gpu_percentage(text: str) -> Optional[float]:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*%?\s*", text)
    if match is None:
        return None
    value = float(match.group(1))
    return value if math.isfinite(value) and 0 <= value <= 100 else None


def parse_gpu_fallback(text: str) -> Dict[str, Optional[float]]:
    """Parse tagged GPU counters with explicit units; reject ambiguous output."""
    result: Dict[str, Optional[float]] = {"busy_percent": None, "clock_mhz": None}
    for line in text.splitlines():
        kind, separator, raw = line.partition(":")
        if not separator:
            continue
        if kind == "percent" and result["busy_percent"] is None:
            result["busy_percent"] = _gpu_percentage(raw)
        elif kind == "busy_pair" and result["busy_percent"] is None:
            match = re.fullmatch(r"\s*(\d{1,18})\s+(\d{1,18})\s*", raw)
            if match:
                busy, total = int(match.group(1)), int(match.group(2))
                if total > 0 and 0 <= busy <= total:
                    result["busy_percent"] = 100.0 * busy / total
        elif kind in {"hz", "khz"} and result["clock_mhz"] is None:
            if re.fullmatch(r"\s*\d{1,12}\s*", raw):
                hz = int(raw.strip()) * (1000 if kind == "khz" else 1)
                if 0 < hz <= 10_000_000_000:
                    result["clock_mhz"] = hz / 1_000_000.0
    return result


def parse_sysfs_bundle(
    text: str, thermal_labels: List[str], core_count: int = 8
) -> Dict[str, Any]:
    """Parse the fixed-order concatenation of sysfs files emitted by
    ``StreamingCaptureSession._build_sysfs_script``.

    Line layout:
      line 0                   : gpu_busy_percentage  ("99 %")
      line 1                   : kgsl clock_mhz       ("422")
      lines 2..1+core_count    : cpu scaling_cur_freq (kHz)
      rest                     : thermal zone temps   (millidegrees),
                                 positionally matched to ``thermal_labels``

    Returns a dict:
      gpu_busy_percent: Optional[float]  (0-100)
      gpu_clock_mhz:    Optional[float]
      cpu_freqs_mhz:    List[float]      (length = core_count)
      cpu_freqs_complete: bool           (all core positions valid and positive)
      thermal_c:        Dict[str, float] (label -> Celsius)
    """
    result: Dict[str, Any] = {
        "gpu_busy_percent": None,
        "gpu_clock_mhz": None,
        "cpu_freqs_mhz": [],
        "cpu_freqs_complete": False,
        "thermal_c": {},
    }
    if not text:
        return result

    lines = [ln.rstrip("\r") for ln in text.splitlines()]
    if not lines:
        return result

    # 0: gpu busy ("99 %")
    result["gpu_busy_percent"] = _gpu_percentage(lines[0]) if lines else None

    # 1: clock (MHz, from clock_mhz — already in MHz)
    if len(lines) > 1:
        try:
            val = float(lines[1].strip())
            # Defensive: if someone later wires in a Hz-reporting node, rescale.
            if math.isfinite(val) and val > 0:
                result["gpu_clock_mhz"] = val / 1e6 if val > 1e5 else val
        except ValueError:
            pass

    # 2..1+core_count: cpu freqs (kHz → MHz). Always emit exactly one list
    # position per logical CPU. The capture command writes a non-numeric sentinel
    # for failed reads, so a missing middle core remains 0.0 at that index rather
    # than shifting every following CPU/thermal value left.
    cpu_start = 2
    cpu_end = cpu_start + core_count
    expected_line_count = cpu_end + len(thermal_labels)
    cpu_freqs_complete = len(lines) == expected_line_count and core_count > 0
    for index in range(core_count):
        ln = lines[cpu_start + index] if cpu_start + index < len(lines) else ""
        try:
            khz = int(ln.strip())
        except ValueError:
            result["cpu_freqs_mhz"].append(0.0)
            cpu_freqs_complete = False
            continue
        if khz <= 0:
            result["cpu_freqs_mhz"].append(0.0)
            cpu_freqs_complete = False
            continue
        result["cpu_freqs_mhz"].append(khz / 1000.0)
    result["cpu_freqs_complete"] = cpu_freqs_complete

    # Thermal fields use the same absolute positions. A missing core or thermal
    # read therefore cannot cause a later value to be assigned to another label.
    for index, label in enumerate(thermal_labels):
        line_index = cpu_end + index
        ln = lines[line_index] if line_index < len(lines) else ""
        try:
            milli = int(ln.strip())
        except ValueError:
            continue
        result["thermal_c"][label] = milli / 1000.0

    return result


def parse_sf_timestats_total_frames(text: str, bundle_id: str) -> Optional[int]:
    if not text or not bundle_id:
        return None
    blocks = re.split(r"(?=^layerName\s*=)", text, flags=re.MULTILINE)
    for block in blocks:
        name_match = _SF_LAYER_BLOCK.search(block)
        if not name_match or bundle_id not in name_match.group(1):
            continue
        frames_match = _SF_TOTAL_FRAMES.search(block)
        if frames_match:
            try:
                return int(frames_match.group(1))
            except ValueError:
                continue
    return None


def parse_sf_timestats_refresh_hz(text: str, bundle_id: str = "") -> Optional[float]:
    """Return the highest display/layer refresh hint in a timestats dump."""
    if not text:
        return None

    candidates: List[float] = []
    blocks = re.split(r"(?=^layerName\s*=)", text, flags=re.MULTILINE)
    for block in blocks:
        if bundle_id:
            name_match = _SF_LAYER_BLOCK.search(block)
            if name_match and bundle_id not in name_match.group(1):
                continue
        for pattern in (_SF_FRAME_RATE, _SF_REFRESH_RATE):
            for match in pattern.finditer(block):
                try:
                    value = float(match.group(1))
                except ValueError:
                    continue
                if 0 < value <= 1000:
                    candidates.append(value)

    if candidates:
        return max(candidates)

    # Some dumps only expose display refresh outside the target layer block.
    if bundle_id:
        return parse_sf_timestats_refresh_hz(text)
    return None


def parse_sf_timestats_rate_hints(text: str, bundle_id: str = "") -> List[float]:
    """Return ordered cadence hints from a SurfaceFlinger timestats dump.

    A target layer's ``frameRate`` vote is more specific than a display-wide
    ``refreshRate``.  Keep both (rather than taking only ``max()``) because a
    45 fps layer on a 120 Hz display needs the 45 Hz hint to reverse the
    integer-millisecond histogram quantization.  Hints from unrelated layers
    are intentionally excluded when ``bundle_id`` is supplied.
    """
    if not text:
        return []

    blocks = re.split(r"(?=^layerName\s*=)", text, flags=re.MULTILINE)
    target_blocks: List[str] = []
    global_blocks: List[str] = []
    all_blocks: List[str] = []
    for block in blocks:
        name_match = _SF_LAYER_BLOCK.search(block)
        if name_match is None:
            global_blocks.append(block)
        elif not bundle_id or bundle_id in name_match.group(1):
            target_blocks.append(block)
        if not bundle_id or name_match is None or (
            bundle_id in name_match.group(1)
        ):
            all_blocks.append(block)

    def values(source_blocks: List[str], pattern: "re.Pattern[str]") -> List[float]:
        found: List[float] = []
        for block in source_blocks:
            for match in pattern.finditer(block):
                try:
                    value = float(match.group(1))
                except ValueError:
                    continue
                if not math.isfinite(value) or not 0 < value <= 1000:
                    continue
                if not any(
                    math.isclose(value, seen, abs_tol=1e-6) for seen in found
                ):
                    found.append(value)
        return found

    if not bundle_id:
        return values(all_blocks, _SF_FRAME_RATE) + values(
            all_blocks, _SF_REFRESH_RATE
        )

    ordered = (
        values(target_blocks, _SF_FRAME_RATE)
        + values(target_blocks, _SF_REFRESH_RATE)
        + values(global_blocks, _SF_FRAME_RATE)
        + values(global_blocks, _SF_REFRESH_RATE)
    )
    deduped: List[float] = []
    for value in ordered:
        if not any(math.isclose(value, seen, abs_tol=1e-6) for seen in deduped):
            deduped.append(value)
    return deduped


@dataclass
class SfTimestatsGlobal:
    """Per-display ('Legacy stats') section of `dumpsys SurfaceFlinger
    --timestats -dump`, accumulated since the last `--timestats -clear`.

    For SurfaceView/Vulkan games on Android 12+, `dumpsys gfxinfo <pkg>`
    no longer reports frames or jank — those frames are only visible at
    the SurfaceFlinger composition layer. This struct captures the
    display-wide totals (target app dominates a full-screen game) and
    the present-to-present histogram for percentile estimation.
    """
    total_frames: int = 0
    missed_frames: int = 0
    client_composition_frames: int = 0
    display_on_time_ms: int = 0
    average_frame_duration_ms: Optional[float] = None
    # (bucket_ms, count) pairs as written, e.g. [(8, 170), (16, 44), (24, 3)].
    present_to_present_histogram: List[Tuple[int, int]] = field(default_factory=list)


_SF_LEGACY_HEADER = re.compile(r"^Legacy stats are as follows:", re.MULTILINE)
_SF_TOTAL_FRAMES_LINE = re.compile(r"^totalFrames\s*=\s*(\d+)", re.MULTILINE)
_SF_MISSED_FRAMES_LINE = re.compile(r"^missedFrames\s*=\s*(\d+)", re.MULTILINE)
_SF_CLIENT_COMP_LINE = re.compile(
    r"^clientCompositionFrames\s*=\s*(\d+)", re.MULTILINE
)
_SF_DISPLAY_ON_TIME = re.compile(r"^displayOnTime\s*=\s*(\d+)\s*ms", re.MULTILINE)
_SF_AVG_FRAME_DURATION = re.compile(
    r"^averageFrameDuration\s*=\s*([\d.]+)\s*ms", re.MULTILINE
)
_SF_P2P_HISTOGRAM_HEADER = re.compile(
    r"^presentToPresent histogram is as below:\s*\n(.+)$", re.MULTILINE
)
_HIST_BUCKET = re.compile(r"(\d+)ms=(\d+)")


def parse_sf_timestats_global(text: str) -> Optional[SfTimestatsGlobal]:
    """Parse the display-wide 'Legacy stats' section. Returns None if
    no legacy section is found OR if totalFrames is 0 (no signal)."""
    if not text or not _SF_LEGACY_HEADER.search(text):
        return None

    # Restrict matches to the segment BEFORE the first per-layer block, so
    # we don't accidentally pick up a layer's totalFrames=… first.
    end = text.find("\nlayerName")
    head = text if end < 0 else text[:end]

    out = SfTimestatsGlobal()
    if m := _SF_TOTAL_FRAMES_LINE.search(head):
        out.total_frames = int(m.group(1))
    if m := _SF_MISSED_FRAMES_LINE.search(head):
        out.missed_frames = int(m.group(1))
    if m := _SF_CLIENT_COMP_LINE.search(head):
        out.client_composition_frames = int(m.group(1))
    if m := _SF_DISPLAY_ON_TIME.search(head):
        out.display_on_time_ms = int(m.group(1))
    if m := _SF_AVG_FRAME_DURATION.search(head):
        try:
            out.average_frame_duration_ms = float(m.group(1))
        except ValueError:
            pass
    if m := _SF_P2P_HISTOGRAM_HEADER.search(head):
        for bm, ct in _HIST_BUCKET.findall(m.group(1)):
            count = int(ct)
            if count:
                out.present_to_present_histogram.append((int(bm), count))

    if out.total_frames <= 0:
        return None
    return out


def jank_from_histogram(
    histogram: List[Tuple[int, int]],
    threshold_factor: float = 1.5,
    min_threshold_ms: float = 18.0,
) -> Tuple[int, int]:
    """Estimate jank from a present-to-present histogram, adapted to the
    game's actual target frame rate.

    The mode bucket (most common ms boundary) is treated as the app's
    intended frame interval; a frame is "janky" if its bucket exceeds
    `mode * threshold_factor`. A floor of `min_threshold_ms` (≈ 60fps
    deadline + slack, per Android Studio jank docs) prevents flagging
    natural variation when the mode is very small (e.g. 8ms = 120fps).

    Returns `(total_frames, janky_frames)`. SurfaceFlinger's display-wide
    `missedFrames` counter is *not* used here because on a 120Hz display
    a steady 30fps game logs 75% of frames as "missed" even though no
    deadline was actually missed by the app.

    Why not Perfetto FrameTimeline? Investigated 2026-04: Vulkan/
    SurfaceView games (most modern mobile games) bypass the View system
    and never emit `surface_frame_token` events, so
    `actual_frame_timeline_slice` rows arrive without per-app upid
    attribution — the data is identical to what `dumpsys SurfaceFlinger
    --timestats` already gives us, just at higher capture cost
    (~500ms/sample for trace start + pull + TraceProcessor cold-start).
    """
    if not histogram:
        return 0, 0
    total = sum(c for _, c in histogram)
    if total <= 0:
        return 0, 0
    mode_bucket, _ = max(histogram, key=lambda x: x[1])
    threshold = max(mode_bucket * threshold_factor, min_threshold_ms)
    janky = sum(c for bucket_ms, c in histogram if bucket_ms > threshold)
    return total, janky


def jank_split_from_histogram(
    histogram: List[Tuple[int, int]],
    threshold_factor: float = 1.5,
    min_threshold_ms: float = 18.0,
    big_threshold_factor: float = 3.0,
    big_min_threshold_ms: float = 32.0,
) -> Tuple[int, int, int, int]:
    """Split janky frames into small vs big jank from a present-to-present
    histogram, adapting to the app's actual target frame rate (see
    :func:`jank_from_histogram`).

    A frame is janky if its bucket exceeds ``mode * threshold_factor`` (floored
    at ``min_threshold_ms``). Of those, frames beyond ``mode *
    big_threshold_factor`` (floored at ``big_min_threshold_ms`` ≈ two missed
    60fps deadlines) are *big* jank; the remainder are *small* jank.

    Returns ``(total_frames, janky_frames, small_jank, big_jank)`` with
    ``small_jank + big_jank == janky_frames``.
    """
    if not histogram:
        return 0, 0, 0, 0
    total = sum(c for _, c in histogram)
    if total <= 0:
        return 0, 0, 0, 0
    mode_bucket, _ = max(histogram, key=lambda x: x[1])
    threshold = max(mode_bucket * threshold_factor, min_threshold_ms)
    big_threshold = max(mode_bucket * big_threshold_factor, big_min_threshold_ms)
    small = 0
    big = 0
    for bucket_ms, count in histogram:
        if bucket_ms > big_threshold:
            big += count
        elif bucket_ms > threshold:
            small += count
    return total, small + big, small, big


def percentiles_from_histogram(
    histogram: List[Tuple[int, int]],
    percentiles: Tuple[int, ...] = (50, 90, 95, 99),
) -> Dict[int, float]:
    """Compute approximate percentiles (ms) from a `(bucket_ms, count)`
    histogram. Bucket value is treated as the upper edge of the bin
    (Android SF convention: 8ms bucket = frames in (prev_bucket, 8ms]).
    Returns {percentile: ms} using a step interpolation: the smallest
    bucket whose cumulative count crosses the target rank.
    Empty histograms return {}.
    """
    total = sum(c for _, c in histogram)
    if total <= 0:
        return {}
    # Histogram order matters; SF emits ascending bucket boundaries.
    sorted_h = sorted(histogram, key=lambda x: x[0])
    out: Dict[int, float] = {}
    for p in percentiles:
        # Use ceil(N * p/100) as the rank, 1-indexed (matches "p99 of 100
        # samples = the largest sample" intuition).
        rank = max(1, (total * p + 99) // 100)
        cum = 0
        for bucket_ms, count in sorted_h:
            cum += count
            if cum >= rank:
                out[p] = float(bucket_ms)
                break
    return out


# AOSP TimeStats' default histogram configuration.  It first floors each
# present-to-present duration to an integer millisecond, then inserts it into
# the first configured bucket >= that value.  Consequently a 60 Hz period
# (16.667 ms) lands in the 16 ms bucket and a naive ``1000 / 16`` calculation
# reports 62.5 fps (rounded to 63 in the UI).
_SF_HISTOGRAM_BUCKETS_MS: Tuple[int, ...] = (
    tuple(range(0, 35))
    + tuple(range(36, 51, 2))
    + tuple(range(54, 151, 4))
    + tuple(range(200, 1001, 50))
)


def _histogram_bucket_interval_ms(bucket_ms: int) -> Optional[Tuple[int, int]]:
    """Continuous-ms interval represented by an AOSP TimeStats bucket."""
    index = bisect_left(_SF_HISTOGRAM_BUCKETS_MS, bucket_ms)
    if (
        index >= len(_SF_HISTOGRAM_BUCKETS_MS)
        or _SF_HISTOGRAM_BUCKETS_MS[index] != bucket_ms
    ):
        return None
    previous = _SF_HISTOGRAM_BUCKETS_MS[index - 1] if index else -1
    # Integer duration values previous+1 .. bucket map here.  Before flooring,
    # that is the half-open continuous interval [previous+1, bucket+1).
    return previous + 1, bucket_ms + 1


def _fps_from_histogram_at_cadence(
    histogram: List[Tuple[int, int]], cadence_hz: float
) -> Optional[float]:
    """Reverse AOSP ms-bucket quantization when every bucket fits a cadence.

    Returns ``None`` when even one non-empty bucket cannot be explained as an
    integer number of ``cadence_hz`` periods.  This all-or-nothing rule keeps a
    stale 60 Hz hint from changing a genuine 120/VRR histogram.
    """
    if not math.isfinite(cadence_hz) or cadence_hz <= 0 or cadence_hz > 1000:
        return None

    period_ms = 1000.0 / cadence_hz
    total_frames = 0
    total_periods = 0
    for bucket_ms, count in histogram:
        if bucket_ms <= 0 or count <= 0:
            continue
        interval = _histogram_bucket_interval_ms(bucket_ms)
        if interval is None:
            return None
        lower_ms, upper_ms = interval
        # Coarse tail buckets can contain several cadence multiples (for
        # example the 200 ms bucket spans [151, 201)).  Quantization cannot
        # tell those apart, so reconstruct only when exactly one period count
        # is compatible with the represented interval.
        search_start = max(1, math.floor(lower_ms / period_ms) - 1)
        search_stop = math.ceil(upper_ms / period_ms) + 1
        compatible_periods = [
            periods
            for periods in range(search_start, search_stop + 1)
            if lower_ms <= periods * period_ms < upper_ms
        ]
        if len(compatible_periods) != 1:
            return None
        periods = compatible_periods[0]
        total_frames += count
        total_periods += periods * count

    if total_frames <= 0 or total_periods <= 0:
        return None
    return total_frames * cadence_hz / total_periods


def fps_from_histogram(
    histogram: List[Tuple[int, int]], cadence_hz: Optional[float] = None
) -> Optional[float]:
    """Estimate displayed FPS from SurfaceFlinger present-to-present buckets.

    Without a cadence hint the historical raw bucket estimate is preserved:
    a lone 16 ms bucket is information-theoretically ambiguous between a real
    16 ms interval and a floored 16.667 ms (60 Hz) interval.  With a compatible
    ``frameRate``/``refreshRate`` hint, exact cadence periods are reconstructed
    before averaging so 60 Hz becomes 60 rather than 62.5 while dropped periods
    still lower the result.
    """
    total_frames = 0
    total_ms = 0.0
    for bucket_ms, count in histogram:
        if bucket_ms <= 0 or count <= 0:
            continue
        total_frames += count
        total_ms += bucket_ms * count
    if total_frames <= 0 or total_ms <= 0:
        return None

    if cadence_hz is not None:
        corrected = _fps_from_histogram_at_cadence(histogram, cadence_hz)
        if corrected is not None:
            return corrected
    return total_frames / (total_ms / 1000.0)


def fps_from_histogram_rate_hints(
    histogram: List[Tuple[int, int]], cadence_hz_candidates: List[float]
) -> Optional[float]:
    """Use the first cadence hint compatible with every non-empty bucket."""
    for cadence_hz in cadence_hz_candidates:
        corrected = _fps_from_histogram_at_cadence(histogram, cadence_hz)
        if corrected is not None:
            return corrected
    return fps_from_histogram(histogram)


def parse_proc_status_rss_kb(text: str) -> Optional[int]:
    m = _PROC_STATUS_VMRSS.search(text)
    return int(m.group(1)) if m else None


def parse_proc_meminfo_total_kb(text: str) -> int:
    if not text:
        return 0
    m = _PROC_MEMINFO_TOTAL.search(text)
    return int(m.group(1)) if m else 0


def parse_proc_meminfo(text: str) -> Tuple[Optional[int], Optional[int]]:
    """Return (MemTotal_kb, MemAvailable_kb) from /proc/meminfo. Either element
    is None when its line is absent. System used = total - available."""
    total: Optional[int] = None
    avail: Optional[int] = None
    if text:
        if m := _PROC_MEMINFO_TOTAL.search(text):
            total = int(m.group(1))
        if m := _PROC_MEMINFO_AVAIL.search(text):
            avail = int(m.group(1))
    return total, avail


def parse_proc_stat_cpu(text: str) -> Optional[Tuple[int, int]]:
    """Return (total_jiffies, idle_jiffies) from the aggregate ``cpu`` line of
    /proc/stat (idle includes iowait). System-wide busy% over an interval is
    ``(total_delta - idle_delta) / total_delta * 100``. Returns None if the
    line is missing or malformed.
    """
    nums = parse_proc_stat_cpu_ticks(text)
    if nums is None:
        return None
    # user nice system idle iowait irq softirq steal guest guest_nice
    # guest/guest_nice are already included in user/nice. Counting them again
    # inflates utilization on devices running virtualized workloads.
    total = sum(nums)
    idle = nums[3] + nums[4]  # idle + iowait
    return total, idle


def parse_proc_stat_cpu_ticks(text: str) -> Optional[Tuple[int, ...]]:
    match = _PROC_STAT_CPU.search(text)
    return _parse_cpu_ticks(match.group(1)) if match else None


def _parse_cpu_ticks(text: str) -> Optional[Tuple[int, ...]]:
    try:
        nums = tuple(int(p) for p in text.split())
    except ValueError:
        return None
    if len(nums) < 5 or any(value < 0 for value in nums):
        return None
    return nums[:8] + (0,) * max(0, 8 - len(nums))


def parse_proc_stat_cpu_cores(text: str) -> Dict[int, Tuple[int, ...]]:
    """Return stable cpuN indices and non-overlapping counters; skip bad rows."""
    cores: Dict[int, Tuple[int, ...]] = {}
    for match in _PROC_STAT_CORES.finditer(text):
        try:
            index = int(match.group(1))
        except ValueError:
            continue
        ticks = _parse_cpu_ticks(match.group(2))
        if index < 1024 and ticks is not None:
            cores[index] = ticks
    return cores


def cpu_ticks_percent(previous: Tuple[int, ...], current: Tuple[int, ...]) -> Optional[float]:
    """A reset/rollback is unavailable, while a measured idle interval is 0%."""
    if len(previous) != len(current):
        return None
    deltas = [new - old for old, new in zip(previous, current)]
    total = sum(deltas)
    if total <= 0 or any(delta < 0 for delta in deltas):
        return None
    return (total - deltas[3] - deltas[4]) / total * 100.0


def parse_proc_net_dev(text: str) -> Tuple[Optional[int], Optional[int]]:
    """Return aggregate non-loopback RX/TX byte counters from ``/proc/net/dev``.

    The kernel format carries eight RX fields followed by eight TX fields.  A
    malformed interface line is ignored instead of making a partially readable
    device look like it has zero traffic; ``(None, None)`` means no usable
    interface was present.
    """
    rx_total = 0
    tx_total = 0
    found = False
    for line in text.splitlines():
        if ":" not in line:
            continue
        iface, fields_text = line.split(":", 1)
        if iface.strip() == "lo":
            continue
        fields = fields_text.split()
        if len(fields) < 9:
            continue
        try:
            rx = int(fields[0])
            tx = int(fields[8])
        except ValueError:
            continue
        if rx < 0 or tx < 0:
            continue
        rx_total += rx
        tx_total += tx
        found = True
    return (rx_total, tx_total) if found else (None, None)


def parse_proc_stat_jiffies(text: str) -> Optional[int]:
    """Return utime+stime (jiffies) from /proc/<pid>/stat.

    Field layout: pid (comm) state ppid ... utime(14) stime(15) ...
    comm may contain spaces; split after the last ')'.
    """
    if not text:
        return None
    text = text.strip()
    rparen = text.rfind(")")
    if rparen < 0:
        return None
    tail = text[rparen + 1 :].split()
    # After the closing paren the remaining fields are state (1) ppid (2) ...
    # utime is field 14 overall, which is tail[11]; stime is tail[12].
    if len(tail) < 13:
        return None
    try:
        user, system = int(tail[11]), int(tail[12])
        return user + system if user >= 0 and system >= 0 else None
    except ValueError:
        return None


def parse_proc_stat_starttime(text: str) -> Optional[int]:
    """Process birth counter distinguishes PID reuse from the same process."""
    tail = text[text.rfind(")") + 1:].split() if ")" in text else []
    try:
        value = int(tail[19])
    except (IndexError, ValueError):
        return None
    return value if value >= 0 else None


def parse_top(text: str, pid: int) -> CpuStats:
    stats = CpuStats(pid=pid, cpu_percent=None)
    if not text:
        return stats

    pid_col: Optional[int] = None
    cpu_col: Optional[int] = None
    mem_col: Optional[int] = None
    data_lines: list[str] = []

    for line in text.splitlines():
        if pid_col is None:
            if "PID" in line and "%CPU" in line and "%MEM" in line:
                normalized = line.replace("[", " ").replace("]", " ")
                columns = normalized.split()
                try:
                    pid_col = columns.index("PID")
                    cpu_col = columns.index("%CPU")
                    mem_col = columns.index("%MEM")
                except ValueError:
                    pid_col = cpu_col = mem_col = None
            continue
        data_lines.append(line)

    if pid_col is None or cpu_col is None or mem_col is None:
        return stats

    target = str(pid)
    for line in data_lines:
        tokens = line.split()
        if len(tokens) <= max(pid_col, cpu_col, mem_col):
            continue
        if tokens[pid_col] != target:
            continue
        try:
            cpu_percent = float(tokens[cpu_col])
            mem_percent = float(tokens[mem_col])
        except ValueError:
            continue
        if not math.isfinite(cpu_percent) or cpu_percent < 0:
            continue
        stats.cpu_percent = cpu_percent
        if math.isfinite(mem_percent) and mem_percent >= 0:
            stats.mem_percent = mem_percent
        return stats

    return stats


_BATTERY_LEVEL = re.compile(r"^\s*level:\s*(\d+)", re.MULTILINE)
_BATTERY_STATUS = re.compile(r"^\s*status:\s*(\d+)", re.MULTILINE)
_BATTERY_VOLTAGE = re.compile(r"^\s*voltage:\s*(-?\d+)", re.MULTILINE)
_BATTERY_CURRENT = re.compile(r"^\s*current now:\s*(-?\d+)", re.MULTILINE)
_BATTERY_TEMPERATURE = re.compile(
    r"^\s*temperature:\s*(-?\d+(?:\.\d+)?)\s*$", re.MULTILINE
)

_BATTERY_STATUS_MAP = {
    1: "Unknown",
    2: "Charging",
    3: "Discharging",
    4: "Not charging",
    5: "Full",
}


def parse_battery(text: str) -> BatteryStats:
    """Parse ``dumpsys battery`` output into BatteryStats.

    ``current now`` is in microamperes (µA); converted to mA. Sign convention
    varies by device (some vendors invert), so the raw sign is preserved.
    """
    stats = BatteryStats()
    if not text:
        return stats

    if m := _BATTERY_LEVEL.search(text):
        stats.level_percent = int(m.group(1))
    if m := _BATTERY_STATUS.search(text):
        stats.status = _BATTERY_STATUS_MAP.get(int(m.group(1)))
    if m := _BATTERY_VOLTAGE.search(text):
        stats.voltage_mv = float(m.group(1))
    if m := _BATTERY_CURRENT.search(text):
        stats.current_ma = float(m.group(1)) / 1000.0

    return stats


def parse_battery_temperature_c(text: str) -> Optional[float]:
    """Return the ``dumpsys battery`` pack temperature in Celsius.

    Android exposes this field in deci-degrees Celsius.  Only the exact
    top-level ``temperature:`` key is accepted; similarly named vendor fields
    (charger temperature, temperature status, etc.) must not be treated as the
    battery pack sensor.
    """
    if not text:
        return None
    match = _BATTERY_TEMPERATURE.search(text)
    if match is None:
        return None
    try:
        value = float(match.group(1)) / 10.0
    except ValueError:
        return None
    return value if math.isfinite(value) else None
