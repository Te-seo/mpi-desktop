from __future__ import annotations

from mpi.android import chart_memory

import ipaddress
import logging
import math
import re
import shlex
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_log = logging.getLogger(__name__)

from ..backend import AppBackgroundedError, AppExitedError, AppNotRunningError, DeviceBackend
from ..migration import cache_root
from ..schema import (
    BatteryTemperatureReading,
    CpuStats,
    DeviceInfo,
    GpuStats,
    MemStats,
    MetricsSnapshot,
    NetworkStats,
    PackageInfo,
    Platform,
    ThermalStats,
)
from . import adb, axml, parsers
from .proc_stream import ProcStream


_VERSION_NAME = re.compile(r"versionName=(\S+)")
_VERSION_CODE = re.compile(r"versionCode=(\S+)")
# Older Android prints ``userId=10056`` in dumpsys package; current builds
# (verified on One UI / Android 15) print ``appId=10056`` instead. Same value
# either way — the package's app uid, which the custom-android:process pid
# fallback keys on.
_USER_ID = re.compile(r"(?:userId|appId)=(\d+)")
_GETPROP_LINE = re.compile(r"^\[([^\]]+)\]: \[(.*)\]$")
_DEVICE_NAME_SPLIT = "__MPI_DEVICE_NAME_SPLIT__"


def _clean_device_name(value: str) -> str:
    for line in value.splitlines():
        name = line.strip()
        if name and name.lower() not in {"null", "none", "unknown", "undefined"}:
            return name
    return ""

# --- Launcher icon discovery (used by IPC fetch_app_icon) ----------------

# e.g. ``res/mipmap-xxxhdpi-v4/ic_launcher.png`` or
# ``res/drawable-hdpi/ic_launcher_round.webp``. Modern apps built with
# Android Studio defaults ship .webp launchers to save space, which the
# original PNG-only regex skipped — they showed up as missing icons in the
# package dropdown. WebP is rendered natively by Chromium, so no further
# conversion is needed on the renderer side.
_LAUNCHER_ICON_RE = re.compile(
    r"^res/(?:mipmap|drawable)([^/]*)/ic_launcher(?:_round)?\.(?:png|webp)$",
    re.IGNORECASE,
)
_DENSITY_RANK = {
    "xxxhdpi": 6,
    "xxhdpi": 5,
    "xhdpi": 4,
    "hdpi": 3,
    "tvdpi": 2,
    "mdpi": 2,
    "ldpi": 1,
    "anydpi": 0,
    "nodpi": 0,
}
_UNZIP_LIST_LINE = re.compile(r"^\s*(\d+)\s+\S+\s+\S+\s+(\S.*\S)\s*$")
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_RIFF_MAGIC = b"RIFF"
_WEBP_FORMAT = b"WEBP"

# Fallback hardware sanity bound for Android FPS, used only when the panel's
# real refresh can't be detected. No shipping mobile panel exceeds this, so it
# never clips a real 60/90/120/144/165 reading — it only rejects impossible
# GPU-submission bursts (gfxinfo PROFILEDATA FrameCompleted timestamps can imply
# hundreds of fps) during candidate selection.
#
# When the panel's MAX supported refresh IS detected (``_detect_max_refresh_hz``),
# it is used as the ceiling instead, so the reported fps can never exceed the
# panel rate. We take the maximum of all *supported* modes from ``dumpsys
# display`` (a stable, per-device constant) — NOT the per-dump active-mode hint,
# which frequently reads 60 Hz on a 120 Hz panel and would wrongly pin a true
# 120 fps game at 60. This is what fixes the periodic overshoot where a short
# (0.5 s) count/window sample reads e.g. 65 on a 60 Hz panel or 125 on a 120 Hz
# panel: the count spans the reset+dump latency but is divided by the nominal
# window, so a couple of extra frames inflate the rate past the true ceiling.
_DEFAULT_ANDROID_FPS_CEILING = 240.0
_FPS_CEILING_SLACK_RATIO = 0.05
_FPS_CEILING_MIN_SLACK = 3.0

# Plausible mobile-panel refresh window (Hz). Filters garbage when scanning
# dumpsys output for the max supported refresh.
_MIN_PLAUSIBLE_REFRESH_HZ = 24.0
_MAX_PLAUSIBLE_REFRESH_HZ = 360.0
_REFRESH_FPS_RE = re.compile(r"fps=([0-9]+(?:\.[0-9]+)?)")
_REFRESH_HZ_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*Hz\b")
_ANDROID_PACKAGE_RE = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+$")
_MAX_LOGICAL_CPU_COUNT = 4096


def _max_plausible_refresh(text: str, pattern: "re.Pattern[str]") -> Optional[float]:
    best = 0.0
    for m in pattern.finditer(text):
        try:
            v = float(m.group(1))
        except ValueError:
            continue
        if _MIN_PLAUSIBLE_REFRESH_HZ <= v <= _MAX_PLAUSIBLE_REFRESH_HZ and v > best:
            best = v
    return round(best) if best > 0 else None


def _parse_max_refresh_hz(text: str) -> Optional[float]:
    """Highest supported refresh (Hz) found in a dumpsys dump, or None.

    Prefers ``dumpsys display`` ``fps=`` entries (the supported-mode list);
    falls back to ``... Hz`` tokens (SurfaceFlinger mode list). Values outside a
    sane panel window are ignored so stray numbers don't masquerade as refresh.
    """
    if not text:
        return None
    return _max_plausible_refresh(text, _REFRESH_FPS_RE) or _max_plausible_refresh(
        text, _REFRESH_HZ_RE
    )


def _parse_cpu_list_count(text: str) -> int:
    """Return a trustworthy contiguous logical-CPU count, or 0.

    Linux cpulists can contain individual indices and inclusive ranges, such as
    ``0-7`` or ``0-3,4,5-7``.  The capture/report layout requires the stable
    contiguous sequence ``cpu0..cpuN``; a gap, overlap, duplicate, reversed
    range, or malformed token therefore means the divisor is unavailable.  A
    defensive upper bound prevents hostile/corrupt device output from creating
    an enormous per-core command.
    """
    value = text.strip()
    if not value:
        return 0

    expected = 0
    for raw_part in value.split(","):
        part = raw_part.strip()
        if re.fullmatch(r"[0-9]+", part):
            if len(part) > 4:
                return 0
            start = end = int(part)
        else:
            match = re.fullmatch(r"([0-9]+)-([0-9]+)", part)
            if match is None:
                return 0
            if len(match.group(1)) > 4 or len(match.group(2)) > 4:
                return 0
            start, end = (int(match.group(1)), int(match.group(2)))

        if start != expected or end < start or end >= _MAX_LOGICAL_CPU_COUNT:
            return 0
        expected = end + 1

    return expected


_MISSING_SYSFS_VALUE = "__MPI_MISSING__"

# Read only documented, unit-specific GPU counters. Missing/vendor-locked nodes
# remain absent; no GPU load is inferred from FPS or CPU use.
_GPU_FALLBACK_COMMAND = (
    "for f in /sys/class/kgsl/kgsl-3d0/gpubusy; do v=; "
    'if IFS= read -r v < "$f" 2>/dev/null || [ -n "$v" ]; then '
    "printf 'busy_pair:%s\\n' \"$v\"; fi; done; "
    "for f in /sys/kernel/gpu/gpu_busy /sys/class/drm/card*/device/gpu_busy_percent "
    "/sys/class/misc/mali0/device/utilization "
    "/sys/devices/platform/*mali*/utilization "
    "/sys/class/devfreq/*gpu*/load /sys/class/devfreq/*mali*/load; do v=; "
    'if IFS= read -r v < "$f" 2>/dev/null || [ -n "$v" ]; then '
    "printf 'percent:%s\\n' \"$v\"; fi; done; "
    # Samsung SGPU reports kHz even through devfreq; never treat it as Hz.
    "for f in /sys/class/devfreq/*sgpu*/cur_freq; do v=; "
    'if IFS= read -r v < "$f" 2>/dev/null || [ -n "$v" ]; then '
    "printf 'khz:%s\\n' \"$v\"; fi; done; "
    "for f in /sys/class/kgsl/kgsl-3d0/gpuclk "
    "/sys/class/kgsl/kgsl-3d0/devfreq/cur_freq "
    "/sys/class/devfreq/*gpu*/cur_freq /sys/class/devfreq/*mali*/cur_freq; do v=; "
    'case "$f" in *sgpu*) continue;; esac; '
    'if IFS= read -r v < "$f" 2>/dev/null || [ -n "$v" ]; then '
    "printf 'hz:%s\\n' \"$v\"; fi; done; true"
)



def _positional_sysfs_read_command(files: List[str]) -> str:
    """Read one line per sysfs path while retaining missing-file positions.

    A single ``cat path0 path1 ...`` silently omits a failed middle file, which
    shifts every following CPU/thermal value into the wrong slot. Android's
    shell ``read`` is a builtin, so this remains one adb call without spawning a
    process per file and emits an explicit placeholder for every failed read.
    """
    if not files:
        return "true"
    paths = " ".join(shlex.quote(path) for path in files)
    return (
        f"for f in {paths}; do v=; "
        'if IFS= read -r v 2>/dev/null < "$f" || [ -n "$v" ]; then '
        # The expected cells are numeric (optionally with units). Prevent a
        # malformed flag or escape from changing echo's line boundaries.
        f"""case "$v" in -[neE]*|*'\\'*) v={_MISSING_SYSFS_VALUE};; esac; """
        'echo "$v"; '
        f"else echo {_MISSING_SYSFS_VALUE}; fi; done"
    )


def _complete_khz_total(output: str, expected_count: int) -> int:
    """Sum an exact, position-preserving set of positive kHz readings."""
    if expected_count <= 0:
        return 0
    lines = output.splitlines()
    if len(lines) != expected_count:
        return 0
    values: List[int] = []
    for line in lines:
        value = line.strip()
        if not value.isdigit() or len(value) > 16:
            return 0
        parsed = int(value)
        if parsed <= 0:
            return 0
        values.append(parsed)
    return sum(values)


def _frequency_normalized_app_cpu(
    cpu_percent: Optional[float],
    core_count: int,
    current_freqs_mhz: List[float],
    current_freqs_complete: bool,
    max_freq_total_mhz: float,
) -> Optional[float]:
    """Return Android's frequency-normalized app CPU only from complete input."""
    if (
        cpu_percent is None
        or not math.isfinite(cpu_percent)
        or core_count <= 0
        or not current_freqs_complete
        or len(current_freqs_mhz) != core_count
        or max_freq_total_mhz <= 0
        or not math.isfinite(max_freq_total_mhz)
        or any(not math.isfinite(freq) or freq <= 0 for freq in current_freqs_mhz)
    ):
        return None
    current_total_mhz = sum(current_freqs_mhz)
    if current_total_mhz <= 0:
        return None
    app_fraction = max(0.0, min(100.0, cpu_percent / core_count))
    return max(
        0.0,
        min(100.0, app_fraction * current_total_mhz / max_freq_total_mhz),
    )


def _fps_is_plausible(fps: Optional[float], ceiling_hz: float) -> bool:
    if fps is None or fps <= 0:
        return False
    slack = max(_FPS_CEILING_MIN_SLACK, ceiling_hz * _FPS_CEILING_SLACK_RATIO)
    return fps <= ceiling_hz + slack


def _cap_fps_to_ceiling(fps: Optional[float], ceiling_hz: float) -> Optional[float]:
    if fps is None:
        return None
    if fps <= 0:
        return None
    return min(fps, ceiling_hz)


def _finalize_frame_fps(
    frames,
    *,
    sf_timestats: str,
    bundle_id: str,
    duration_sec: float,
    ceiling_hz: float = _DEFAULT_ANDROID_FPS_CEILING,
) -> None:
    """Keep FPS tied to displayed frames, not CPU/GPU queue submission bursts.

    ``ceiling_hz`` is the panel's max supported refresh when known (so the
    reported fps can never exceed it); callers that can't detect it pass the
    default sanity bound.
    """
    sf_global = parsers.parse_sf_timestats_global(sf_timestats) if sf_timestats else None
    sf_histogram_raw_fps = (
        parsers.fps_from_histogram(sf_global.present_to_present_histogram)
        if sf_global is not None
        else None
    )
    sf_rate_hints = (
        parsers.parse_sf_timestats_rate_hints(sf_timestats, bundle_id)
        if sf_timestats and sf_global is not None
        else []
    )
    sf_histogram_fps = (
        parsers.fps_from_histogram_rate_hints(
            sf_global.present_to_present_histogram, sf_rate_hints
        )
        if sf_global is not None and sf_rate_hints
        else sf_histogram_raw_fps
    )
    sf_layer_fps = (
        parsers.parse_sf_timestats_fps(sf_timestats, bundle_id)
        if sf_timestats
        else None
    )
    duration_fps = (
        frames.total_frames / duration_sec
        if frames.total_frames > 0 and duration_sec > 0
        else None
    )

    # ``averageFPS`` is itself calculated by SurfaceFlinger from the same
    # integer-ms present-to-present histogram.  Prefer the cadence-corrected
    # histogram only when it is demonstrably that same coarse signal (or the
    # layer value is absent/implausible).  A precise direct value such as 119.4
    # with a stale frameRate=60 hint must remain untouched.
    histogram_was_corrected = (
        sf_histogram_raw_fps is not None
        and sf_histogram_fps is not None
        and not math.isclose(
            sf_histogram_raw_fps, sf_histogram_fps, rel_tol=1e-9, abs_tol=1e-9
        )
    )
    layer_matches_raw_histogram = (
        sf_layer_fps is not None
        and sf_histogram_raw_fps is not None
        and math.isclose(
            sf_layer_fps, sf_histogram_raw_fps, rel_tol=1e-3, abs_tol=0.05
        )
    )
    prefer_corrected_histogram = histogram_was_corrected and (
        not _fps_is_plausible(sf_layer_fps, ceiling_hz)
        or layer_matches_raw_histogram
    )

    candidates = (
        (
            sf_histogram_fps,
            sf_layer_fps,
            frames.fps_estimate,
            duration_fps,
        )
        if prefer_corrected_histogram
        else (
            sf_layer_fps,
            sf_histogram_fps,
            frames.fps_estimate,
            duration_fps,
        )
    )

    for candidate in candidates:
        if _fps_is_plausible(candidate, ceiling_hz):
            frames.fps_estimate = candidate
            break
    else:
        for candidate in (
            sf_histogram_fps,
            sf_layer_fps,
            frames.fps_estimate,
            duration_fps,
        ):
            if candidate is not None and candidate > 0:
                frames.fps_estimate = _cap_fps_to_ceiling(candidate, ceiling_hz)
                break

    # Bound only against the absolute hardware sanity ceiling — never snapped
    # to a fixed tier, never capped to a (possibly-misread) per-dump refresh.
    frames.fps_estimate = _cap_fps_to_ceiling(frames.fps_estimate, ceiling_hz)


def _merge_surfaceflinger_frame_stats(
    frames,
    *,
    sf_timestats: str,
    bundle_id: str,
) -> None:
    if not sf_timestats:
        return

    sf_frames = parsers.parse_sf_timestats_total_frames(sf_timestats, bundle_id)
    if sf_frames is not None and frames.total_frames == 0:
        frames.total_frames = sf_frames
        frames.source = "android_surfaceflinger_timestats"
        frames.scope = "app_layer"

    # Jank fallback: SurfaceView/Vulkan apps don't populate the gfxinfo
    # "Janky frames:" line, so use SurfaceFlinger's display-side histogram.
    sf_global = parsers.parse_sf_timestats_global(sf_timestats)
    if sf_global is None:
        return
    if frames.total_frames == 0:
        frames.total_frames = sf_global.total_frames
        frames.source = "android_surfaceflinger_timestats"
        frames.scope = "device_display"
    if sf_global.present_to_present_histogram:
        sf_total, sf_janky, sf_small, sf_big = parsers.jank_split_from_histogram(
            sf_global.present_to_present_histogram
        )
        if sf_total > 0:
            # The histogram is the only source of the small/big split, so always
            # populate it when present — including View/gfxinfo apps that already
            # reported a "Janky frames:" aggregate. Only adopt the histogram's
            # aggregate janky_frames/percent as a fallback when gfxinfo gave none.
            frames.small_jank = sf_small
            frames.big_jank = sf_big
            if frames.janky_frames == 0:
                frames.janky_frames = sf_janky
                frames.jank_percent = sf_janky / sf_total * 100.0
    if frames.p50_ms == 0 and sf_global.present_to_present_histogram:
        pcts = parsers.percentiles_from_histogram(
            sf_global.present_to_present_histogram
        )
        frames.p50_ms = pcts.get(50, 0.0)
        frames.p90_ms = pcts.get(90, 0.0)
        frames.p95_ms = pcts.get(95, 0.0)
        frames.p99_ms = pcts.get(99, 0.0)


def _is_reachable_wireless_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    if ip.version != 4:
        return False
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        return False
    # Android devices can expose 192.0.0.2 through IPv4/IPv6 translation paths.
    # It is not a LAN address the desktop can reach with adb connect.
    if value.startswith("192.0.0."):
        return False
    return True


def _is_supported_image(data: bytes) -> bool:
    """Accept PNG (8-byte magic) or WebP (RIFF????WEBP at offset 0/8)."""
    if len(data) < 12:
        return False
    if data.startswith(_PNG_MAGIC):
        return True
    if data[:4] == _RIFF_MAGIC and data[8:12] == _WEBP_FORMAT:
        return True
    return False


def _icon_cache_dir() -> Path:
    """Persistent on-disk icon cache so package dropdown loads in subsequent
    sessions don't pay the adb extraction cost again."""
    p = cache_root() / "icons"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return p


def _safe_icon_filename(bundle_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", bundle_id) + ".png"


def _icon_cache_read(bundle_id: str) -> Optional[bytes]:
    if not bundle_id:
        return None
    path = _icon_cache_dir() / _safe_icon_filename(bundle_id)
    try:
        if not path.is_file():
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if not _is_supported_image(data):
        # Stale / corrupt cache file. Drop and refetch.
        try:
            path.unlink()
        except OSError:
            pass
        return None
    return data


def _icon_cache_write(bundle_id: str, data: bytes) -> None:
    if not bundle_id:
        return
    path = _icon_cache_dir() / _safe_icon_filename(bundle_id)
    try:
        path.write_bytes(data)
    except OSError as exc:
        _log.debug("icon cache write failed for %s: %s", bundle_id, exc)


def cache_app_icon(bundle_id: str, data: bytes) -> None:
    """Persist an icon obtained by an alternate source (for example Companion)."""
    if bundle_id and _is_supported_image(data):
        _icon_cache_write(bundle_id, data)


def _label_cache_dir() -> Path:
    """Persistent on-disk cache for resolved app labels, sibling to the icon
    cache. Resolving a label means streaming an APK's resources.arsc over adb
    (multiple MB for big apps); caching the tiny resolved string keeps repeat
    sessions instant."""
    p = cache_root() / "labels"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return p


def _label_cache_read(bundle_id: str) -> Optional[str]:
    if not bundle_id:
        return None
    path = _label_cache_dir() / (re.sub(r"[^A-Za-z0-9._-]", "_", bundle_id) + ".txt")
    try:
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    return text or None


def _clean_label(text: str) -> str:
    """Collapse whitespace in an app label. Some apps embed newlines/tabs (to
    wrap the launcher name onto two lines) or stray double spaces; flatten them
    so the single-line dropdown row stays clean."""
    return re.sub(r"\s+", " ", text).strip()


def _label_cache_write(bundle_id: str, label: str) -> None:
    if not bundle_id or not label:
        return
    path = _label_cache_dir() / (re.sub(r"[^A-Za-z0-9._-]", "_", bundle_id) + ".txt")
    try:
        path.write_text(label, encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        _log.debug("label cache write failed for %s: %s", bundle_id, exc)


def cache_app_label(bundle_id: str, label: str) -> None:
    """Persist a resolved friendly label for capture and legacy-CSV recovery."""
    clean = _clean_label(label) if isinstance(label, str) else ""
    if bundle_id and clean:
        _label_cache_write(bundle_id, clean)


def _qualifier_rank(qualifier: str) -> int:
    rank = 0
    for part in qualifier.split("-"):
        if part in _DENSITY_RANK:
            rank = max(rank, _DENSITY_RANK[part])
    return rank


def _parse_unzip_listing(text: str) -> List[Tuple[int, str]]:
    items: List[Tuple[int, str]] = []
    for line in text.splitlines():
        m = _UNZIP_LIST_LINE.match(line)
        if not m:
            continue
        path = m.group(2)
        if path.endswith(("files", "file")):
            continue
        items.append((int(m.group(1)), path))
    return items


def _pick_launcher_icon(entries: List[Tuple[int, str]]) -> Optional[str]:
    """Pick the highest-density ic_launcher PNG, preferring non-round."""
    best_path: Optional[str] = None
    best_key: Tuple[int, int, int] = (-1, -1, -1)
    for size, path in entries:
        m = _LAUNCHER_ICON_RE.match(path)
        if not m:
            continue
        rank = _qualifier_rank(m.group(1))
        is_round = "_round" in path.rsplit("/", 1)[-1].lower()
        round_score = -1 if is_round else 0
        key = (rank, round_score, size)
        if key > best_key:
            best_key = key
            best_path = path
    return best_path


def _uid_to_android_user(uid: int) -> Optional[str]:
    """Numeric uid → Android account name for app uids (10056 → "u0_a56").

    Only the per-user app range (appId 10000–19999) has this form; system uids
    (system/radio/...) return None. ``dumpsys package`` reports the appId for
    user 0, so the common case is ``u0_a<appId-10000>``.
    """
    user, app_id = divmod(uid, 100000)
    if 10000 <= app_id <= 19999:
        return f"u{user}_a{app_id - 10000}"
    return None


def _pick_pid_by_uid(ps_output: str, bundle_id: str, uid: str) -> int:
    """Pick the most main-looking process owned by ``uid`` from
    ``ps -A -o PID,UID,NAME`` output. Returns 0 when none matches.

    The UID column is numeric on current toybox ps; the ``u0_aNNN`` account
    form is accepted too in case a device resolves it. Matching the exact uid
    (not just the appId suffix) keeps clone-user duplicates out — e.g. a
    Secure Folder copy runs as 150xxxxx and must not be measured for the
    user-0 app.

    An app can own several processes (main + ``:sub`` services); prefer, in
    order: an exact bundle-id name, a name without ':' (the main process when
    the manifest gave it a custom ``android:process`` like
    ``android.process.acore``), a package-private ``<bundle_id>:sub`` name,
    then anything else with the uid. Ties keep the first hit — ps lists in
    pid order and the main process usually started first.

    Known limit: packages sharing a uid via the deprecated
    ``android:sharedUserId`` cannot be told apart here; acceptable for the
    user-installed games this tool measures.
    """
    ps_user = _uid_to_android_user(int(uid))
    best_pid, best_score = 0, -1
    for line in ps_output.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit():
            continue  # header or malformed line
        pid_s, uid_col, name = parts[0], parts[1], parts[2].strip()
        if uid_col != uid and (ps_user is None or uid_col != ps_user):
            continue
        if name == bundle_id:
            score = 3
        elif ":" not in name:
            score = 2
        elif name.startswith(bundle_id + ":"):
            score = 1
        else:
            score = 0
        if score > best_score:
            best_pid, best_score = int(pid_s), score
    return best_pid


def _pick_strict_target_pid_by_uid(
    ps_output: str, bundle_id: str, uid: str
) -> Tuple[int, bool]:
    """Return a definitely-target PID and whether the selected UID was seen.

    This is deliberately narrower than :func:`_pick_pid_by_uid`: the regular
    streaming path preserves its custom-process fallback, while a guard must
    never launch over an ambiguous ``sharedUserId`` sibling. Only the exact
    package process or one of its ``:<subprocess>`` names proves ownership.
    """
    ps_user = _uid_to_android_user(int(uid))
    exact_pid = 0
    sub_pid = 0
    saw_uid = False
    for line in ps_output.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        pid_s, uid_col, name = parts[0], parts[1], parts[2].strip()
        if uid_col != uid and (ps_user is None or uid_col != ps_user):
            continue
        saw_uid = True
        if name == bundle_id and not exact_pid:
            exact_pid = int(pid_s)
        elif name.startswith(bundle_id + ":") and not sub_pid:
            sub_pid = int(pid_s)
    return (exact_pid or sub_pid), saw_uid


class AndroidBatteryTemperatureMonitor:
    """App-independent Android battery temperature reader.

    ``dumpsys battery`` is authoritative and reports deci-degrees Celsius.  A
    sysfs fallback is used only when the exact field is absent/unparseable, and
    only a thermal zone whose type is exactly ``battery`` is eligible.
    """

    def __init__(self, backend: "AndroidBackend", serial: str) -> None:
        self._backend = backend
        self._serial = serial
        self._closed = threading.Event()
        self._battery_zone = backend._discover_battery_thermal_zone(serial)

    def sample(self) -> Optional[BatteryTemperatureReading]:
        if self._closed.is_set():
            return None

        battery_raw = adb.shell("dumpsys battery", serial=self._serial)
        temperature_c = parsers.parse_battery_temperature_c(battery_raw)
        source = "dumpsys:battery"

        if temperature_c is None and self._battery_zone is not None:
            zone_raw = adb.shell(
                f"cat /sys/class/thermal/thermal_zone{self._battery_zone}/temp",
                serial=self._serial,
            )
            temperature_c = self._parse_sysfs_temperature(zone_raw)
            source = "thermal:battery"

        if temperature_c is None:
            return None
        return BatteryTemperatureReading(
            temperature_c=temperature_c,
            sampled_at=datetime.now(timezone.utc).isoformat(),
            source=source,
        )

    @staticmethod
    def _parse_sysfs_temperature(text: str) -> Optional[float]:
        try:
            raw = float(text.strip())
        except (TypeError, ValueError):
            return None
        if not math.isfinite(raw):
            return None
        # Linux thermal sysfs is normally millidegrees.  A few vendor kernels
        # expose deci-degrees, so retain the same scale ladder used elsewhere.
        magnitude = abs(raw)
        if magnitude >= 1000:
            return raw / 1000.0
        if magnitude >= 100:
            return raw / 10.0
        return raw

    def close(self) -> None:
        self._closed.set()


def _add_dumpsys_battery_thermal_fallback(
    zones_c: Dict[str, float], battery_raw: str
) -> None:
    """Add the live-capture battery fallback when sysfs has no pack zone."""
    if "battery" in zones_c or "thermal:battery" in zones_c:
        return
    temperature_c = parsers.parse_battery_temperature_c(battery_raw)
    if temperature_c is None or not 5.0 <= temperature_c <= 60.0:
        return
    zones_c["battery"] = temperature_c


class AndroidBackend(DeviceBackend):
    def __init__(self) -> None:
        # Device/build metadata is static for a connected adb transport. The
        # selector polls frequently, so cache the expensive getprop result while
        # still running `adb devices` every time for prompt hotplug detection.
        self._cache_lock = threading.RLock()
        self._device_scan_lock = threading.Lock()
        self._device_cache: Dict[str, DeviceInfo] = {}
        self._package_cache: Dict[Tuple[str, str], PackageInfo] = {}
        self._package_details_cache: Dict[Tuple[str, str], PackageInfo] = {}
        self._static_cache: Dict[
            str, Tuple[int, int, int, List[Tuple[str, int]], float, float]
        ] = {}
        self._static_locks: Dict[str, threading.Lock] = {}

    def list_devices(self) -> List[DeviceInfo]:
        # Coalesce overlapping polls from multiple renderer windows. The second
        # caller still gets a fresh `adb devices` snapshot, but never launches a
        # duplicate property sweep while the first one is in progress.
        with self._device_scan_lock:
            output = adb.run(["devices", "-l"])
            devices: List[DeviceInfo] = []
            live_serials: set[str] = set()
            for line in output.splitlines():
                line = line.strip()
                if not line or line.startswith("List of devices"):
                    continue
                parts = line.split()
                if len(parts) < 2 or parts[1] != "device":
                    continue
                serial = parts[0]
                live_serials.add(serial)
                with self._cache_lock:
                    cached = self._device_cache.get(serial)
                if cached is not None:
                    devices.append(cached)
                    continue

                connection_type = "wireless" if ":" in serial else "usb"
                adb_fields = {
                    key: value.replace("_", " ")
                    for token in parts[2:]
                    if ":" in token
                    for key, value in [token.split(":", 1)]
                }
                try:
                    # One shell round-trip returns every property. The previous
                    # implementation spawned 6-8 independent adb processes per
                    # device, which dominated every selector refresh.
                    prop_output = adb.shell("getprop", serial=serial)
                except adb.AdbError:
                    prop_output = ""
                try:
                    names_output = adb.shell(
                        "settings get global device_name; "
                        f"echo {_DEVICE_NAME_SPLIT}; "
                        "settings get secure bluetooth_name",
                        serial=serial,
                    )
                except adb.AdbError:
                    names_output = ""
                props: Dict[str, str] = {}
                for prop_line in prop_output.splitlines():
                    match = _GETPROP_LINE.match(prop_line.strip())
                    if match:
                        props[match.group(1)] = match.group(2)

                physical_serial = (
                    props.get("ro.serialno") or props.get("ro.boot.serialno") or ""
                )
                # Preserve the compact historical representation when adb's
                # transport id already is the hardware serial, but retain the
                # real id for *any* differing transport (Wi-Fi and vendor USB
                # aliases alike) so reservation cannot split one phone in two.
                hw_serial = (
                    physical_serial
                    if physical_serial and physical_serial != serial
                    else ""
                )
                model = (
                    props.get("ro.product.model")
                    or adb_fields.get("model")
                    or "unknown"
                )
                global_name_raw, _, bluetooth_name_raw = names_output.partition(
                    _DEVICE_NAME_SPLIT
                )
                display_name = (
                    _clean_device_name(global_name_raw)
                    or _clean_device_name(bluetooth_name_raw)
                    or _clean_device_name(
                        props.get("ro.product.marketname")
                        or props.get("ro.vendor.product.marketname")
                        or ""
                    )
                    or model
                )
                release = props.get("ro.build.version.release") or "?"
                sdk = props.get("ro.build.version.sdk") or "?"
                code_name = props.get("ro.product.device") or adb_fields.get("device") or ""
                hw_ver = props.get("ro.product.board") or ""
                # Build display id looks like "TP1A.220624.014.A536NKSU3CWA1";
                # the SW build identifier is the trailing token's last 4 chars.
                display_id = props.get("ro.build.display.id") or ""
                last_token = display_id.rsplit(".", 1)[-1] if display_id else ""
                sw_ver = last_token[-4:] if last_token else ""
                device = DeviceInfo(
                    serial=serial,
                    platform=Platform.ANDROID,
                    model=model,
                    os_version=f"Android {release} (SDK {sdk})",
                    connection_type=connection_type,
                    code_name=code_name,
                    hw_ver=hw_ver,
                    sw_ver=sw_ver,
                    hw_serial=hw_serial,
                    display_name=display_name,
                    build_version=props.get("ro.build.version.incremental") or props.get("ro.build.id") or "",
                    build_name=display_id or props.get("ro.build.id") or "",
                    android_sdk_int=int(sdk) if sdk.isascii() and sdk.isdecimal() and len(sdk) <= 4 else None,
                    android_memory_form_factor=chart_memory.form_factor(props.get("ro.build.characteristics", "")),
                )
                devices.append(device)
                # A transport can briefly report `device` before getprop is
                # ready. Return the adb -l fallback now, but retry next poll
                # instead of pinning incomplete metadata for the whole session.
                if props:
                    with self._cache_lock:
                        self._device_cache[serial] = device

            # A successful authoritative scan is the cache invalidation point.
            # Do not clear on adb errors (the function raises before here), so a
            # transient transport failure cannot throw away warm metadata.
            with self._cache_lock:
                stale = set(self._device_cache) - live_serials
                for serial in stale:
                    self._device_cache.pop(serial, None)
                    self._static_cache.pop(serial, None)
                    self._static_locks.pop(serial, None)
                    for key in [k for k in self._package_cache if k[0] == serial]:
                        self._package_cache.pop(key, None)
                    for key in [k for k in self._package_details_cache if k[0] == serial]:
                        self._package_details_cache.pop(key, None)
            return devices

    def physical_device_id(self, serial: str) -> str:
        """Stable reservation key shared by this device's USB/Wi-Fi transports."""
        device = self._resolve_device(serial)
        return device.hw_serial or device.serial

    def prepare_wireless(self, serial: str, port: int = 5555) -> dict:
        if ":" in serial:
            raise ValueError("select a USB-connected device before preparing wireless adb")
        if port <= 0 or port > 65535:
            raise ValueError("port must be between 1 and 65535")

        ip = self._detect_wifi_ip(serial)
        target = f"{ip}:{port}"
        adb.run(["tcpip", str(port)], serial=serial, timeout=60.0)
        connect_output = adb.run(["connect", target], timeout=60.0).strip()
        if "failed" in connect_output.lower() or "unable" in connect_output.lower():
            raise adb.AdbError(
                f"wireless adb connect failed for {target}: {connect_output}"
            )

        deadline = time.monotonic() + 10.0
        devices: List[DeviceInfo] = []
        while time.monotonic() < deadline:
            devices = self.list_devices()
            device = next((d for d in devices if d.serial == target), None)
            if device is not None:
                return {"serial": target, "ip": ip, "port": port, "device": device}
            time.sleep(0.5)

        visible = ", ".join(d.serial for d in devices) or "none"
        raise adb.AdbError(
            "wireless adb connect did not produce an online device "
            f"({target}). adb connect output: {connect_output or 'empty'}; "
            f"visible devices: {visible}"
        )

    def _detect_wifi_ip(self, serial: str) -> str:
        candidates: List[str] = []
        commands = (
            ("ip -f inet addr show wlan0", r"\binet\s+(\d+\.\d+\.\d+\.\d+)/", True),
            ("ifconfig wlan0", r"\binet(?: addr:)?\s*(\d+\.\d+\.\d+\.\d+)\b", True),
            ("getprop dhcp.wlan0.ipaddress", r"\b(\d+\.\d+\.\d+\.\d+)\b", True),
            ("ip route get 8.8.8.8", r"\bsrc\s+(\d+\.\d+\.\d+\.\d+)\b", False),
        )
        for command, pattern, wlan_only in commands:
            try:
                text = adb.shell(command, serial=serial)
            except adb.AdbError:
                continue
            if not wlan_only and "wlan0" not in text:
                continue
            for match in re.finditer(pattern, text):
                ip = match.group(1)
                if ip not in candidates:
                    candidates.append(ip)
            for ip in candidates:
                if _is_reachable_wireless_ip(ip):
                    return ip

        for ip in candidates:
            if _is_reachable_wireless_ip(ip):
                return ip
        if candidates:
            raise adb.AdbError(
                "detected Wi-Fi IP address is not usable for wireless adb: "
                + ", ".join(candidates)
            )
        raise adb.AdbError("could not detect Wi-Fi IP address for selected device")

    def list_packages(self, serial: str, user_only: bool = True) -> List[PackageInfo]:
        cmd = "pm list packages -f -3" if user_only else "pm list packages -f"
        output = adb.shell(cmd, serial=serial)
        packages: List[PackageInfo] = []
        for line in output.splitlines():
            line = line.strip()
            if not line.startswith("package:"):
                continue
            payload = line[len("package:") :]
            if "=" not in payload:
                continue
            apk_path, bundle_id = payload.rsplit("=", 1)
            packages.append(PackageInfo(bundle_id=bundle_id, apk_path=apk_path))
        with self._cache_lock:
            live = {package.bundle_id for package in packages}
            for key in [
                key for key in self._package_cache if key[0] == serial and key[1] not in live
            ]:
                self._package_cache.pop(key, None)
                self._package_details_cache.pop(key, None)
            for package in packages:
                key = (serial, package.bundle_id)
                detailed = self._package_details_cache.get(key)
                # Android normally changes the base-APK path on update/reinstall.
                # Treat that inventory change as authoritative so capture startup
                # cannot reuse stale version, UID, or APK metadata.
                if detailed is not None and detailed.apk_path != package.apk_path:
                    self._package_details_cache.pop(key, None)
                self._package_cache[key] = package
        return packages

    def fetch_app_icon_bytes(
        self,
        serial: str,
        apk_path: Optional[str],
        bundle_id: Optional[str] = None,
    ) -> Optional[bytes]:
        """Extract the launcher icon from an installed APK as bytes.

        Three-tier lookup: in-memory (caller's frontend cache) → disk cache
        (~/.cache/mpi/icons/<bundle_id>.png) → adb extract. Streams
        just the icon entry via ``unzip -l`` + ``exec-out unzip -p`` so we
        don't pull tens of MB of APK to disk per package; the disk hop is the
        big win for repeat sessions, without which the package dropdown
        re-pays the full adb cost every time.

        Supports both PNG and WebP launcher resources. Returns ``None`` when
        the APK has no raster launcher (XML-only adaptive icons), the device
        lacks ``unzip``, or any adb step fails — callers should fall back to a
        placeholder icon.
        """
        if bundle_id:
            cached = _icon_cache_read(bundle_id)
            if cached is not None:
                return cached
        if not apk_path:
            _log.debug("icon: no apk_path for %s", bundle_id)
            return None
        apk_q = shlex.quote(apk_path)
        try:
            listing = adb.shell(f"unzip -l {apk_q}", serial=serial)
        except adb.AdbError as exc:
            _log.debug("icon: unzip -l failed for %s: %s", bundle_id, exc)
            return None
        icon_path = _pick_launcher_icon(_parse_unzip_listing(listing))
        if icon_path is None:
            _log.debug(
                "icon: no raster launcher in %s (XML-only adaptive icon?)",
                bundle_id,
            )
            return None
        try:
            data = adb.exec_out(
                f"unzip -p {apk_q} {shlex.quote(icon_path)}",
                serial=serial,
            )
        except adb.AdbError as exc:
            _log.debug("icon: exec-out failed for %s (%s): %s", bundle_id, icon_path, exc)
            return None
        if not _is_supported_image(data):
            _log.debug(
                "icon: %s yielded %d bytes from %s but format is not PNG/WebP",
                bundle_id,
                len(data),
                icon_path,
            )
            return None
        if bundle_id:
            # Persist for the next session.
            _icon_cache_write(bundle_id, data)
        return data

    def fetch_app_label(
        self,
        serial: str,
        apk_path: Optional[str],
        bundle_id: Optional[str] = None,
    ) -> Optional[str]:
        """Resolve an installed app's user-visible label (the name shown under
        its launcher icon).

        Android exposes no shell command that returns the label *string* —
        ``dumpsys package`` only gives the ``labelRes`` id — so parse it out of
        the APK: read ``application@android:label`` from the binary
        AndroidManifest.xml, then, when it is a resource reference, resolve the
        string from resources.arsc (see :mod:`mpi.android.axml`).

        Three-tier like :meth:`fetch_app_icon_bytes`: disk cache → manifest-only
        (inline label) → manifest + resources.arsc. Returns ``None`` (caller
        shows the bundle id) when the label can't be resolved or any adb step
        fails. The manifest is small (tens of KB); resources.arsc is only pulled
        when the label is a reference, and the resolved string is cached so a
        repeat session never re-pays the extraction.
        """
        if bundle_id:
            cached = _label_cache_read(bundle_id)
            if cached is not None:
                return cached
        if not apk_path:
            return None
        apk_q = shlex.quote(apk_path)
        try:
            manifest = adb.exec_out(
                f"unzip -p {apk_q} AndroidManifest.xml", serial=serial
            )
        except adb.AdbError as exc:
            _log.debug("label: AndroidManifest extract failed for %s: %s", bundle_id, exc)
            return None

        label = axml.parse_application_label(manifest)
        if isinstance(label, str):
            text = _clean_label(label)
            if text and bundle_id:
                _label_cache_write(bundle_id, text)
            return text or None
        if not isinstance(label, int):
            _log.debug("label: no application@label in manifest for %s", bundle_id)
            return None

        # Label is a @string/... reference; resolve it from resources.arsc.
        try:
            arsc = adb.exec_out(
                f"unzip -p {apk_q} resources.arsc", serial=serial, timeout=60.0
            )
        except adb.AdbError as exc:
            _log.debug("label: resources.arsc extract failed for %s: %s", bundle_id, exc)
            return None
        resolved = axml.resolve_resource_string(arsc, label)
        resolved = _clean_label(resolved) if resolved else ""
        if not resolved:
            _log.debug(
                "label: resid 0x%08x unresolved in resources.arsc for %s",
                label,
                bundle_id,
            )
            return None
        if bundle_id:
            _label_cache_write(bundle_id, resolved)
        return resolved

    def capture(
        self,
        serial: str,
        package: PackageInfo,
        duration_sec: float = 10.0,
    ) -> MetricsSnapshot:
        device, enriched = self.prepare_capture(serial, package)
        return self.capture_sample(serial, device, enriched, duration_sec)

    def prepare_capture(
        self, serial: str, package: PackageInfo
    ) -> Tuple[DeviceInfo, PackageInfo]:
        """Resolve expensive per-trace metadata once for streaming captures."""
        # Device metadata and package metadata use independent adb commands.
        # Resolve them together so a cold start pays the slower branch, not the
        # sum. Warm starts normally hit both session caches immediately.
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="capture-meta") as pool:
            device_future = pool.submit(self._resolve_device, serial)
            package_future = pool.submit(self._enrich_package, serial, package)
            return device_future.result(), package_future.result()

    def capture_sample(
        self,
        serial: str,
        device: DeviceInfo,
        package: PackageInfo,
        duration_sec: float,
    ) -> MetricsSnapshot:
        """Single capture cycle assuming device+package are already resolved."""
        bundle_id = package.bundle_id

        self._reset_gfxinfo(serial, bundle_id)
        self._safe_shell(serial, "dumpsys SurfaceFlinger --timestats -enable")
        self._safe_shell(serial, "dumpsys SurfaceFlinger --timestats -clear")
        time.sleep(duration_sec)

        with ThreadPoolExecutor(max_workers=6) as pool:
            f_gfx = pool.submit(
                self._safe_shell, serial, f"dumpsys gfxinfo {bundle_id} framestats"
            )
            f_mem = pool.submit(
                self._safe_shell, serial, f"dumpsys meminfo {bundle_id}"
            )
            f_cpu = pool.submit(self._capture_cpu, serial, bundle_id)
            f_battery = pool.submit(self._safe_shell, serial, "dumpsys battery")
            f_sf = pool.submit(
                self._safe_shell,
                serial,
                "dumpsys SurfaceFlinger --timestats -dump",
            )
            f_network = pool.submit(self._safe_shell, serial, "cat /proc/net/dev")
            gfxinfo = f_gfx.result()
            meminfo = f_mem.result()
            pidof, pid, top = f_cpu.result()
            battery_raw = f_battery.result()
            sf_timestats = f_sf.result()
            network_raw = f_network.result()

        raw = {
            "gfxinfo": gfxinfo,
            "meminfo": meminfo,
            "pidof": pidof,
            "top": top,
            "battery": battery_raw,
            "sf_timestats": sf_timestats,
            "network": network_raw,
        }

        frames = parsers.parse_gfxinfo(gfxinfo)
        _merge_surfaceflinger_frame_stats(
            frames,
            sf_timestats=sf_timestats,
            bundle_id=bundle_id,
        )
        _finalize_frame_fps(
            frames,
            sf_timestats=sf_timestats,
            bundle_id=bundle_id,
            duration_sec=duration_sec,
            ceiling_hz=self._detect_max_refresh_hz(serial) or _DEFAULT_ANDROID_FPS_CEILING,
        )
        memory = parsers.parse_meminfo(meminfo)
        cpu = parsers.parse_top(top, pid=pid) if pid else parsers.parse_top("", pid=0)
        battery = parsers.parse_battery(battery_raw)
        network_rx, network_tx = parsers.parse_proc_net_dev(network_raw)

        return MetricsSnapshot(
            device=device,
            package=package,
            captured_at=datetime.now(timezone.utc).isoformat(),
            duration_sec=duration_sec,
            frames=frames,
            memory=memory,
            cpu=cpu,
            network=NetworkStats(rx_bytes=network_rx, tx_bytes=network_tx),
            battery=battery,
            raw=raw,
        )

    def _capture_cpu(self, serial: str, bundle_id: str) -> Tuple[str, int, str]:
        pidof = self._safe_shell(serial, f"pidof {bundle_id}")
        pid = self._parse_pid(pidof)
        top = self._safe_shell(serial, f"top -b -n 1 -p {pid}") if pid else ""
        return pidof, pid, top

    def open_streaming(
        self, serial: str, package: PackageInfo
    ) -> "StreamingCaptureSession":
        """Prepare a streaming session: resolve device, enrich package, cache
        pid + CLK_TCK + MemTotal + CPU core count + thermal-zone layout once so
        per-sample work is minimal.
        """
        device, enriched = self.prepare_capture(serial, package)
        try:
            pid = self._resolve_pid(serial, enriched.bundle_id, enriched.uid)
        except adb.AdbError as exc:
            # Transport/device failure — not evidence about the app. Surface it
            # as what it is instead of a misleading "launch the app" notice.
            raise adb.AdbError(
                f"could not check the app process state on {serial}: {exc}"
            ) from exc
        # Focused-app measurement: refuse to start when the selected app has no
        # running process. Without this the capture would run to completion
        # silently reporting 0 for every app-scoped metric. The stable
        # "app-not-running" sentinel is localized by the desktop.
        if not pid:
            raise AppNotRunningError(
                f"app-not-running: {enriched.bundle_id} has no running process "
                f"on {serial}. Launch the app on the device, then start measuring."
            )

        # Static probes are independent of SurfaceFlinger enablement. Run those
        # branches together; the static side is cached for later captures on the
        # same connected transport.
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="capture-open") as pool:
            static_future = pool.submit(self._load_static_metadata, serial)
            sf_future = pool.submit(self._enable_surfaceflinger_timestats, serial)
            memory_context_future = pool.submit(self._capture_memory_context, serial, enriched.bundle_id, device.android_memory_form_factor)
            (
                hz,
                mem_total,
                core_count,
                thermal_map,
                max_freq_total_mhz,
                fps_ceiling_hz,
            ) = static_future.result()
            sf_ok = sf_future.result()
            memory_category, memory_form_factor = memory_context_future.result()

        return StreamingCaptureSession(
            backend=self,
            serial=serial,
            device=device,
            package=enriched,
            pid=pid,
            hz=hz,
            mem_total_kb=mem_total,
            sf_timestats_enabled=sf_ok,
            core_count=core_count,
            thermal_map=thermal_map,
            max_freq_total_mhz=max_freq_total_mhz,
            fps_ceiling_hz=fps_ceiling_hz,
            memory_category=memory_category,
            memory_form_factor=memory_form_factor,
            proc_stream=ProcStream(serial),
        )

    def _capture_memory_context(self, serial: str, package: str, form_factor: Optional[str]):
        boundary = "\n_MPI_MEM_CONTEXT_\n"
        command = f"dumpsys package {shlex.quote(package)}"
        # Default/nosdcard characteristics are not a proof of a phone. Allow
        # a positive telephony feature only when other Android form factors
        # are absent from this device's package manager feature list.
        if form_factor in (None, "unknown"):
            command += "; printf '\\n_MPI_MEM_CONTEXT_\\n'; pm list features"
        try:
            output = adb.shell(command, serial=serial, timeout=1.5)
        except adb.AdbError:
            return (None, None), form_factor
        dump, _, features = output.partition(boundary)
        return chart_memory.package_category(dump, package), (
            form_factor if form_factor not in (None, "unknown") else chart_memory.form_factor("", features)
        )

    def create_temperature_monitor(
        self,
        serial: str,
        package: Optional[PackageInfo] = None,
        threshold_c: float = 25.0,
        required_hits: int = 2,
        stop_event: Optional[threading.Event] = None,
    ) -> AndroidBatteryTemperatureMonitor:
        """Create the app-independent 1 Hz guard source.

        Cadence and threshold counting are owned by :mod:`mpi.api`;
        the extra arguments keep this seam identical to the iOS prepared-DVT
        monitor, which needs the target app and threshold before it starts.
        """
        del package, threshold_c, required_hits
        if stop_event is not None and stop_event.is_set():
            raise RuntimeError("temperature_guard_cancelled")
        # Fail creation immediately for a disconnected transport rather than
        # returning a monitor whose first sample fails ambiguously.
        self._resolve_device(serial)
        return AndroidBatteryTemperatureMonitor(self, serial)

    def create_temperature_preview_monitor(
        self,
        serial: str,
        package: Optional[PackageInfo] = None,
        *,
        stop_event: Optional[threading.Event] = None,
    ) -> AndroidBatteryTemperatureMonitor:
        """Create the same app-independent source without any launch contract."""
        return self.create_temperature_monitor(
            serial,
            package=package,
            stop_event=stop_event,
        )

    def cold_start_and_open_streaming(
        self,
        serial: str,
        package: PackageInfo,
        *,
        stop_event: Optional[threading.Event] = None,
    ) -> "StreamingCaptureSession":
        """Attach to an existing app, or launch it only when absent.

        Temperature-triggered measurement must preserve a target that is
        already running. Check the process state before touching activity
        management: an existing process is handed straight to
        :meth:`open_streaming`, with no force-stop, launcher resolution, or
        foreground transition. Only an absent process takes the launch path.
        A failed process-state query is deliberately not treated as absence.

        The guard never force-stops a package during failure or cancellation:
        after a launch, ownership cannot be proven if the user restarts the app
        before cleanup.
        """
        _, enriched = self.prepare_capture(serial, package)
        bundle_id = enriched.bundle_id
        if not _ANDROID_PACKAGE_RE.fullmatch(bundle_id):
            raise ValueError(f"invalid Android package id: {bundle_id!r}")

        def check_cancelled() -> None:
            if stop_event is not None and stop_event.is_set():
                raise RuntimeError("temperature_guard_cancelled")

        def attach_existing() -> "StreamingCaptureSession":
            check_cancelled()
            session = self.open_streaming(serial, enriched)
            if stop_event is not None and stop_event.is_set():
                closer = getattr(session, "close", None)
                if callable(closer):
                    closer()
                raise RuntimeError("temperature_guard_cancelled")
            return session

        def strict_pid() -> int:
            try:
                return self._resolve_pid(serial, bundle_id, enriched.uid, strict=True)
            except adb.AdbError as exc:
                raise adb.AdbError(
                    f"could not check the app process state on {serial}: {exc}"
                ) from exc

        check_cancelled()
        if strict_pid():
            return attach_existing()

        check_cancelled()
        component = self._resolve_launcher_activity(serial, bundle_id)
        # Resolving a launcher can be slow enough for the user or system to
        # start the target in the gap. Recheck immediately before activity
        # management so that race still attaches without changing focus.
        check_cancelled()
        if strict_pid():
            return attach_existing()

        check_cancelled()
        start_output = adb.shell(
            f"am start -W -n {shlex.quote(component)}", serial=serial
        )
        lowered = start_output.lower()
        if "error:" in lowered or "exception occurred" in lowered:
            raise RuntimeError(
                f"app_launch_failed: {bundle_id}: {start_output.strip()}"
            )

        deadline = time.monotonic() + 10.0
        pid = 0
        foreground = False
        while time.monotonic() < deadline:
            check_cancelled()
            pid = self._resolve_pid(serial, bundle_id, enriched.uid)
            foreground = self._is_app_foreground(serial, bundle_id)
            if pid and foreground:
                break
            if stop_event is not None:
                if stop_event.wait(0.1):
                    check_cancelled()
            else:
                time.sleep(0.1)
        if not pid:
            raise RuntimeError(
                f"app_launch_failed: no pid for {bundle_id} after am start -W"
            )
        if not foreground:
            raise RuntimeError(
                f"app_launch_failed: {bundle_id} did not reach the foreground"
            )

        return attach_existing()

    def _force_stop_app(
        self, serial: str, bundle_id: str, *, ignore_errors: bool
    ) -> None:
        try:
            adb.shell(f"am force-stop {shlex.quote(bundle_id)}", serial=serial)
        except adb.AdbError:
            if not ignore_errors:
                raise

    def terminate_app(self, serial: str, bundle_id: str) -> None:
        """Stop a target app as an explicit capture-teardown action.

        This remains separate from temperature-guard launch/cancellation,
        which never force-stops a package. Manual capture stop deliberately
        does not call it.
        """
        self._force_stop_app(serial, bundle_id, ignore_errors=False)

    def _resolve_launcher_activity(self, serial: str, bundle_id: str) -> str:
        package_q = shlex.quote(bundle_id)
        commands = (
            "cmd package resolve-activity --brief "
            "-a android.intent.action.MAIN -c android.intent.category.LAUNCHER "
            f"{package_q}",
            "pm resolve-activity --brief "
            "-a android.intent.action.MAIN -c android.intent.category.LAUNCHER "
            f"{package_q}",
        )
        prefix = f"{bundle_id}/"
        last_error: Optional[BaseException] = None
        for command in commands:
            try:
                output = adb.shell(command, serial=serial)
            except adb.AdbError as exc:
                last_error = exc
                continue
            for line in reversed(output.splitlines()):
                candidate = line.strip()
                if candidate.startswith(prefix) and not any(
                    char.isspace() for char in candidate
                ):
                    return candidate
        detail = f": {last_error}" if last_error is not None else ""
        raise RuntimeError(
            "launcher_activity_unavailable: no launcher activity for "
            f"{bundle_id}{detail}"
        )

    def _is_app_foreground(self, serial: str, bundle_id: str) -> bool:
        output = adb.shell(
            "dumpsys activity activities | "
            "grep -E 'mResumedActivity|topResumedActivity|ResumedActivity' || true",
            serial=serial,
        )
        return re.search(rf"(?<![\w.]){re.escape(bundle_id)}/", output) is not None

    def _static_lock_for(self, serial: str) -> threading.Lock:
        with self._cache_lock:
            lock = self._static_locks.get(serial)
            if lock is None:
                lock = threading.Lock()
                self._static_locks[serial] = lock
            return lock

    def _load_static_metadata(
        self, serial: str
    ) -> Tuple[int, int, int, List[Tuple[str, int]], float, float]:
        lock = self._static_lock_for(serial)
        with lock:
            with self._cache_lock:
                cached = self._static_cache.get(serial)
            if cached is not None:
                hz, mem_total, cores, thermal, max_freq, fps_ceiling = cached
                return hz, mem_total, cores, list(thermal), max_freq, fps_ceiling

            # All five branches are read-only and independent. The CPU branch
            # internally resolves the core count before reading its max-frequency
            # files, preserving that dependency while overlapping the other I/O.
            with ThreadPoolExecutor(max_workers=5, thread_name_prefix="android-static") as pool:
                hz_future = pool.submit(self._read_clock_ticks, serial)
                mem_future = pool.submit(self._read_mem_total, serial)
                cpu_future = pool.submit(self._read_cpu_metadata, serial)
                thermal_future = pool.submit(self._discover_thermal_zones, serial)
                fps_future = pool.submit(self._detect_max_refresh_hz, serial)
                hz = hz_future.result()
                mem_total = mem_future.result()
                core_count, max_freq_total_mhz = cpu_future.result()
                thermal_map = thermal_future.result()
                fps_ceiling_hz = fps_future.result() or _DEFAULT_ANDROID_FPS_CEILING

            cached = (
                hz,
                mem_total,
                core_count,
                list(thermal_map),
                max_freq_total_mhz,
                fps_ceiling_hz,
            )
            with self._cache_lock:
                self._static_cache[serial] = cached
            return hz, mem_total, core_count, list(thermal_map), max_freq_total_mhz, fps_ceiling_hz

    def _read_clock_ticks(self, serial: str) -> int:
        try:
            hz = int(adb.shell("getconf CLK_TCK", serial=serial).strip())
            return hz if hz > 0 else 0
        except (adb.AdbError, ValueError):
            return 0

    def _read_mem_total(self, serial: str) -> int:
        return parsers.parse_proc_meminfo_total_kb(
            self._safe_shell(serial, "cat /proc/meminfo")
        )

    def _read_cpu_metadata(self, serial: str) -> Tuple[int, float]:
        core_count = self._count_cpu_cores(serial)
        return core_count, self._read_max_freq_total_mhz(serial, core_count)

    def _read_max_freq_total_mhz(self, serial: str, core_count: int) -> float:
        files = [
            f"/sys/devices/system/cpu/cpu{i}/cpufreq/cpuinfo_max_freq"
            for i in range(core_count)
        ]
        total_khz = self._sum_khz(serial, files)
        if total_khz <= 0:
            files = [
                f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_max_freq"
                for i in range(core_count)
            ]
            total_khz = self._sum_khz(serial, files)
        return total_khz / 1000.0

    def _sum_khz(self, serial: str, files: List[str]) -> int:
        if not files:
            return 0
        out = self._safe_shell(serial, _positional_sysfs_read_command(files))
        return _complete_khz_total(out, len(files))

    def _enable_surfaceflinger_timestats(self, serial: str) -> bool:
        try:
            adb.shell("dumpsys SurfaceFlinger --timestats -enable", serial=serial)
            return True
        except adb.AdbError:
            return False

    def _count_cpu_cores(self, serial: str) -> int:
        try:
            present = adb.shell(
                "cat /sys/devices/system/cpu/present", serial=serial
            )
        except adb.AdbError:
            return 0
        return _parse_cpu_list_count(present)

    def _discover_thermal_zones(self, serial: str) -> List[Tuple[str, int]]:
        """Return a list of (label, thermal_zone_idx) pairs to sample per cycle.

        Labels follow the convention ``<group>:<id>`` so the session can group
        by prefix for aggregation. The chart later takes the max per group.
        """
        try:
            listing = adb.shell(
                "for i in $(seq 0 150); do "
                "T=/sys/class/thermal/thermal_zone$i/type; "
                "[ -f $T ] && echo $i:$(cat $T 2>/dev/null); "
                "done; true",
                serial=serial,
            )
        except adb.AdbError:
            return []

        zones: List[Tuple[str, int]] = []
        for line in listing.splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            idx_str, name = line.split(":", 1)
            try:
                idx = int(idx_str)
            except ValueError:
                continue
            name = name.strip()
            if name.startswith("cpu-hw-trip"):
                continue  # trip point, not a live reading
            if name.startswith("cpu-") or name.startswith("cpullc-"):
                zones.append((f"cpu:{name}", idx))
            elif name.startswith("gpuss-"):
                zones.append((f"gpu:{name}", idx))
            elif name == "battery":
                zones.append(("battery", idx))
            elif name == "sys-therm-0":
                zones.append(("skin", idx))
            elif name == "ddr":
                zones.append(("ddr", idx))
        return zones

    def _discover_battery_thermal_zone(self, serial: str) -> Optional[int]:
        """Index of the exact ``battery`` thermal zone, if one exists."""
        for label, idx in self._discover_thermal_zones(serial):
            if label == "battery":
                return idx
        return None

    # --- helpers ----------------------------------------------------------

    def _getprop(self, serial: str, key: str) -> Optional[str]:
        try:
            return adb.shell(f"getprop {key}", serial=serial).strip() or None
        except adb.AdbError:
            return None

    def _resolve_device(self, serial: str) -> DeviceInfo:
        with self._cache_lock:
            cached = self._device_cache.get(serial)
        if cached is not None:
            return cached
        for d in self.list_devices():
            if d.serial == serial:
                return d
        connection_type = "wireless" if ":" in serial else "usb"
        return DeviceInfo(serial=serial, platform=Platform.ANDROID, connection_type=connection_type)

    def _enrich_package(self, serial: str, package: PackageInfo) -> PackageInfo:
        key = (serial, package.bundle_id)
        cached_label = _label_cache_read(package.bundle_id)
        with self._cache_lock:
            detailed = self._package_details_cache.get(key)
            listed = self._package_cache.get(key)
        if detailed is not None:
            if detailed.label or not cached_label:
                return detailed
            # Metadata may have been resolved lazily by the package selector
            # after this details entry was cached.  Upgrade it rather than
            # returning the earlier label-less snapshot forever.
            detailed = PackageInfo(
                bundle_id=detailed.bundle_id,
                label=cached_label,
                version_name=detailed.version_name,
                version_code=detailed.version_code,
                apk_path=detailed.apk_path,
                uid=detailed.uid,
            )
            with self._cache_lock:
                self._package_cache[key] = detailed
                self._package_details_cache[key] = detailed
            return detailed

        # The list call knows the APK path while start_capture only carries the
        # bundle id. Preserve all already-known fields before adding dumpsys
        # version/uid details.
        base = PackageInfo(
            bundle_id=package.bundle_id,
            label=package.label or (listed.label if listed else None) or cached_label,
            version_name=package.version_name or (listed.version_name if listed else None),
            version_code=package.version_code or (listed.version_code if listed else None),
            apk_path=package.apk_path or (listed.apk_path if listed else None),
            uid=package.uid or (listed.uid if listed else None),
        )
        try:
            info = adb.shell(
                f"dumpsys package {package.bundle_id}", serial=serial
            )
        except adb.AdbError:
            return base

        vname = _VERSION_NAME.search(info)
        vcode = _VERSION_CODE.search(info)
        uid = _USER_ID.search(info)

        enriched = PackageInfo(
            bundle_id=base.bundle_id,
            label=base.label,
            version_name=vname.group(1) if vname else base.version_name,
            version_code=vcode.group(1) if vcode else base.version_code,
            apk_path=base.apk_path,
            uid=uid.group(1) if uid else base.uid,
        )
        with self._cache_lock:
            self._package_cache[key] = enriched
            self._package_details_cache[key] = enriched
        return enriched

    def _reset_gfxinfo(self, serial: str, bundle_id: str) -> None:
        try:
            adb.shell(f"dumpsys gfxinfo {bundle_id} reset", serial=serial)
        except adb.AdbError:
            pass

    def _safe_shell(self, serial: str, command: str) -> str:
        try:
            return adb.shell(command, serial=serial)
        except adb.AdbError as exc:
            return f"(adb error: {exc})"

    def _detect_max_refresh_hz(self, serial: str) -> Optional[float]:
        """Panel's maximum supported refresh (Hz), or None when undetectable.

        Reads the stable supported-mode list (``dumpsys display`` ``fps=``,
        falling back to ``dumpsys SurfaceFlinger`` ``Hz``) and returns the max.
        Used as the FPS ceiling so a short-window count never reports above the
        panel rate. Detected once per session (refresh rate is a static
        per-device constant)."""
        hz = _parse_max_refresh_hz(self._safe_shell(serial, "dumpsys display"))
        if hz is not None:
            return hz
        return _parse_max_refresh_hz(self._safe_shell(serial, "dumpsys SurfaceFlinger"))

    def _resolve_pid(
        self,
        serial: str,
        bundle_id: str,
        uid: Optional[str],
        *,
        strict: bool = False,
        raise_on_error: bool = False,
    ) -> int:
        """Resolve the selected app's running pid; 0 when it has no process.

        When package metadata provides a uid, ``ps`` is authoritative so a
        work-profile or Secure Folder clone with the same bundle id cannot be
        mistaken for the selected user. Without a uid, exact-name ``pidof`` is
        the best available fallback. Strict guard checks also reject ambiguous
        shared-uid/custom-process results instead of treating them as permission
        to launch over an app that may already be running.

        The ``|| true`` on the uid-less fallback is load-bearing: ``pidof``
        exits 1 on "no match", which adb shell-v2 otherwise propagates as a
        command failure indistinguishable from a transport error.
        """
        # A package name is installed per Android user/profile. When metadata
        # identifies that user, pidof alone could point to a clone with the same
        # name, so only a matching ps UID is authoritative.
        if uid and uid.isdigit():
            return self._pid_by_uid(serial, bundle_id, uid, strict=strict, raise_on_error=raise_on_error)
        pidof = adb.shell(f"pidof {bundle_id} || true", serial=serial)
        pid = self._parse_pid(pidof)
        if pid or not strict:
            return pid
        raise adb.AdbError(
            "could not determine target process state: package uid unavailable"
        )

    def _pid_by_uid(
        self,
        serial: str,
        bundle_id: str,
        uid: Optional[str],
        *,
        strict: bool = False,
        raise_on_error: bool = False,
    ) -> int:
        """Custom-``android:process`` fallback: find a process owned by the
        package's uid (parsed from ``dumpsys package`` ``userId=`` at enrich
        time; see :meth:`_enrich_package`).

        The ordinary capture path is best effort and returns 0 when ``ps`` is
        unusable. Strict temperature-guard checks instead propagate transport
        failures and reject a selected uid that has only unattributable
        shared-uid/custom-process rows.
        """
        # The guard uses strict mode before deciding whether it may launch: a
        # failed status query must not be interpreted as an absent process.
        if not uid or not uid.isdigit():
            return 0
        try:
            ps_out = adb.shell("ps -A -o PID,UID,NAME", serial=serial)
        except adb.AdbError:
            if strict or raise_on_error:
                raise
            return 0
        if strict:
            target_pid, saw_uid = _pick_strict_target_pid_by_uid(
                ps_out, bundle_id, uid
            )
            if target_pid:
                return target_pid
            if saw_uid:
                raise adb.AdbError(
                    "could not determine target process state: selected UID "
                    "has no target-owned process"
                )
            return 0
        return _pick_pid_by_uid(ps_out, bundle_id, uid)

    @staticmethod
    def _parse_pid(pidof_output: str) -> int:
        token = pidof_output.strip().split()
        if not token:
            return 0
        try:
            return int(token[0])
        except ValueError:
            return 0


class StreamingCaptureSession:
    """Live Android metrics sampled on a monotonic clock.

    Each probe has at most one adb operation in flight. A slow device service
    can miss its own sample without holding up the other metrics.
    """

    def __init__(
        self,
        backend: "AndroidBackend",
        serial: str,
        device: DeviceInfo,
        package: PackageInfo,
        pid: int,
        hz: int,
        mem_total_kb: int,
        sf_timestats_enabled: bool = False,
        core_count: int = 0,
        thermal_map: Optional[List[Tuple[str, int]]] = None,
        max_freq_total_mhz: Optional[float] = None,
        fps_ceiling_hz: Optional[float] = None,
        memory_category: Tuple[Optional[str], Optional[str]] = (None, None),
        memory_form_factor: Optional[str] = None,
        proc_stream=None,
    ):
        self._backend = backend
        self._serial = serial
        self.device = device
        self.package = package
        self._memory_category = memory_category
        self._memory_form_factor = memory_form_factor or device.android_memory_form_factor
        self.proc_stream = proc_stream
        self._pool = ThreadPoolExecutor(max_workers=9, thread_name_prefix="capture-adb")
        self._pending: Dict[str, Future] = {}
        self._observation_generation = 0
        self._closed = threading.Event()
        self._close_future = Future()
        self._pool_lock = threading.Lock()
        self._reset_locks = {"gfxinfo": threading.Lock(), "sf": threading.Lock()}
        self._last_deadline: Optional[float] = None
        self._gpu_retry_at = 0.0
        self._frame_baseline: Dict[str, Optional[float]] = {}
        self.pid = pid
        self._hz = max(0, hz)
        self._mem_total_kb = mem_total_kb
        self._sf_enabled = sf_timestats_enabled
        self._core_count = core_count
        self._thermal_map = thermal_map or []
        self._sysfs_script = self._build_sysfs_script()
        # Static per-core max frequencies (sum, MHz) — read once and reused to
        # frequency-normalize app CPU% each sample. 0 when unreadable (norm off).
        self._max_freq_total_mhz = (
            max_freq_total_mhz
            if max_freq_total_mhz is not None
            else self._read_max_freq_total_mhz(serial)
        )
        # Panel max refresh (Hz) — read once and used as the FPS ceiling so a
        # short-window frame count can't report above the panel rate (the 65/125
        # overshoot). Falls back to the sanity bound when undetectable.
        self._fps_ceiling_hz = (
            fps_ceiling_hz
            if fps_ceiling_hz is not None
            else self._backend._detect_max_refresh_hz(serial) or _DEFAULT_ANDROID_FPS_CEILING
        )
        self._last_jiffies: Optional[int] = None
        self._last_wall_ns: Optional[int] = None
        self._last_starttime: Optional[int] = None
        self._frame_stream_id = uuid.uuid4().hex
        self._last_frame_timestamp: Dict[str, int] = {}
        # Consecutive samples where /proc/<pid>/stat was unreadable AND pidof
        # confirmed the package has no process — the app-exited detector.
        self._gone_streak = 0
        # Consecutive samples where the resumed-activity dump did NOT list the
        # measured package — the app-backgrounded detector (home button, app
        # switch, screen off). ~3 s at the 0.5 s interval, tolerant of brief
        # transitions and transient system dialogs.
        self._background_streak = 0

        # Seed CPU baseline so the first real sample already has data to diff.
        if pid:
            started_ns = time.monotonic_ns()
            stat = self._backend._safe_shell(serial, f"cat /proc/{pid}/stat")
            observed_ns = (started_ns + time.monotonic_ns()) // 2
            j = parsers.parse_proc_stat_jiffies(stat)
            if j is not None:
                self._last_jiffies = j
                self._last_wall_ns = observed_ns
                self._last_starttime = parsers.parse_proc_stat_starttime(stat)

        # Seed the system-wide CPU baseline from /proc/stat so the first real
        # sample can already report total (all-core) CPU% as a delta.
        sys_stat = self._backend._safe_shell(serial, "cat /proc/stat")
        self._last_core_ticks = parsers.parse_proc_stat_cpu_cores(sys_stat)
        self._cpu_width = max(core_count, max(self._last_core_ticks, default=-1) + 1)
        self._last_sys_ticks = parsers.parse_proc_stat_cpu_ticks(sys_stat)
        self._last_net_rx_bytes: Optional[int] = None
        self._last_net_tx_bytes: Optional[int] = None
        self._last_net_wall_ns: Optional[int] = None

        # Prime frame counters once at attach so even the first regular tick
        # can return an interval. Both services support dump+reset afterward.
        self._frame_sources = ("gfxinfo", "sf") if self._sf_enabled else ("gfxinfo",)
        seed_deadline = time.monotonic() + 1.5
        seeds = {
            source: self._submit_probe(source, seed_deadline, self._frame_counter, source, False)
            for source in self._frame_sources
        }
        for source, seed in seeds.items():
            if seed is not None:
                (_, midpoint), _, _, _ = seed.result()
                self._frame_baseline[source] = midpoint
                del self._pending[source]

    def reset_observation_window(self) -> None:
        """Exclude outstanding reads and delta intervals across pause or reattach."""
        self._observation_generation += 1
        self._frame_baseline.clear()
        self._last_jiffies = self._last_wall_ns = self._last_starttime = None
        self._last_sys_ticks = None
        self._last_core_ticks = {}
        self._last_net_rx_bytes = self._last_net_tx_bytes = self._last_net_wall_ns = None
        self._background_streak = self._gone_streak = 0

    def _build_sysfs_script(self) -> str:
        """One-shot shell read of every sysfs file in a fixed order.

        This remains one adb command and uses the shell's builtin ``read`` for
        each path. Unlike a multi-file ``cat``, it emits an explicit placeholder
        when a middle path cannot be read, so later CPU and thermal fields never
        shift positions. The parser reads the same fixed order established here:

          line 1             : gpu_busy_percentage  ("99 %")
          line 2             : clock_mhz            ("422")
          lines 3..2+N_cores : cpu scaling_cur_freq (kHz)
          remaining          : thermal_zone temps   (millidegrees)

        Runtime read failures are retained as placeholders and make frequency
        normalization unavailable for that sample.
        """
        files = [
            "/sys/class/kgsl/kgsl-3d0/gpu_busy_percentage",
            "/sys/class/kgsl/kgsl-3d0/clock_mhz",
        ]
        for i in range(self._core_count):
            files.append(
                f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_cur_freq"
            )
        for _, idx in self._thermal_map:
            files.append(f"/sys/class/thermal/thermal_zone{idx}/temp")
        return _positional_sysfs_read_command(files)

    def _read_max_freq_total_mhz(self, serial: str) -> float:
        """Sum of per-core max CPU frequencies in MHz (static, read once).

        Reads ``cpuinfo_max_freq`` (falling back to ``scaling_max_freq``) for
        every core in one position-preserving shell command. Values are kHz.
        Returns 0.0 unless every core yields a positive value, which disables
        frequency normalization for incomplete evidence.
        """
        files = [
            f"/sys/devices/system/cpu/cpu{i}/cpufreq/cpuinfo_max_freq"
            for i in range(self._core_count)
        ]
        total_khz = self._sum_khz(serial, files)
        if total_khz <= 0:
            files = [
                f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_max_freq"
                for i in range(self._core_count)
            ]
            total_khz = self._sum_khz(serial, files)
        return total_khz / 1000.0

    def _sum_khz(self, serial: str, files: List[str]) -> int:
        if not files:
            return 0
        out = self._backend._safe_shell(
            serial, _positional_sysfs_read_command(files)
        )
        return _complete_khz_total(out, len(files))

    def _aggregate_thermal(
        self, thermal_raw: Dict[str, float]
    ) -> Dict[str, float]:
        """Reduce per-sensor readings into chart-friendly labels.

        Takes max of ``cpu:*`` and ``gpu:*`` groups (hottest sensor in each
        cluster); passes ``battery``/``skin``/``ddr`` through.
        """
        aggregated: Dict[str, float] = {}
        cpu_vals: List[float] = []
        gpu_vals: List[float] = []
        for label, val in thermal_raw.items():
            if label.startswith("cpu:"):
                cpu_vals.append(val)
            elif label.startswith("gpu:"):
                gpu_vals.append(val)
            else:
                aggregated[label] = val
        if cpu_vals:
            aggregated["cpu_max"] = max(cpu_vals)
        if gpu_vals:
            aggregated["gpu_max"] = max(gpu_vals)
        return aggregated

    def _check_app_alive(self, new_pid: int) -> None:
        """Apply a completed PID lookup on the sample thread."""
        if new_pid and new_pid != self.pid:
            # App restarted under a new pid — follow it instead of ending.
            self.pid = new_pid
            self.reset_observation_window()
            _log.warning(
                "measured app %s restarted; re-attached to pid %d",
                self.package.bundle_id,
                new_pid,
            )
            return
        if new_pid:
            # Same pid still alive — the /proc read was a transient failure.
            self._gone_streak = 0
            return
        self._gone_streak += 1
        if self._gone_streak >= 2:
            raise AppExitedError(
                f"app-exited: {self.package.bundle_id} (pid {self.pid}) is no "
                f"longer running on {self._serial}; measurement stopped."
            )

    # Consecutive foreground misses (at the 0.5 s interval) before the capture
    # ends with the app-backgrounded notice — ~3 s, long enough to ride out
    # activity transitions and brief system dialogs.
    _BACKGROUND_SAMPLES = 6

    def _check_app_foreground(self, focus_raw: str) -> None:
        """End the capture when the measured package leaves the foreground.

        ``focus_raw`` is the ResumedActivity line(s) of ``dumpsys activity
        activities`` — e.g. ``mResumedActivity: ActivityRecord{... u0
        com.pkg/.GameActivity t42}`` (plus ``topResumedActivity=`` on newer
        Androids). The measured app is foreground iff one of those records
        names its package. Home button, app switch, and screen-off all drop
        the package from the resumed set. adb transport failures are ignored
        (a device problem is not evidence about focus).
        """
        if focus_raw.startswith("(adb error:"):
            return
        if f"{self.package.bundle_id}/" in focus_raw:
            self._background_streak = 0
            return
        self._background_streak += 1
        if self._background_streak >= self._BACKGROUND_SAMPLES:
            raise AppBackgroundedError(
                f"app-backgrounded: {self.package.bundle_id} is no longer in the "
                f"foreground on {self._serial}; measurement stopped."
            )

    def _read_chart_proc(self, pid: int):
        try:
            raw = adb.shell(chart_memory.proc_command(pid), serial=self._serial)
        except adb.AdbError:
            return None, None, None, None
        return chart_memory.read_capture_proc(raw, pid, self.package.bundle_id)

    def close(self) -> None:
        with self._pool_lock:
            if not self._closed.is_set():
                self._closed.set()
                self._close_future.set_result(None)
                self._pool.shutdown(wait=False, cancel_futures=True)
        if self.proc_stream is not None:
            self.proc_stream.close()
        # A reset already issued by this session must finish before a new
        # capture attaches. Read-only commands can wind down independently.
        for lock in self._reset_locks.values():
            with lock:
                pass

    def _submit_probe(self, name, deadline, operation, *args, **kwargs) -> Optional[Future]:
        generation = self._observation_generation

        def run():
            token = adb.shell_deadline.set(deadline)
            try:
                begin_ns = time.monotonic_ns()
                value = operation(*args, **kwargs)
                observed_ns = (begin_ns + time.monotonic_ns()) // 2
                return value, time.monotonic(), observed_ns, generation
            finally:
                adb.shell_deadline.reset(token)

        with self._pool_lock:
            if self._closed.is_set():
                return None
            previous = self._pending.get(name)
            if previous is not None:
                return previous
            future = self._pool.submit(run)
            self._pending[name] = future
            return future

    def _frame_counter(self, source: str, dump: bool) -> Tuple[str, Optional[float]]:
        # HWUI and SurfaceFlinger both write the current stats before applying
        # their reset flag. One round trip gives contiguous counting windows.
        if source == "gfxinfo":
            command = f"dumpsys gfxinfo {self.package.bundle_id} reset"
            if dump:
                command += " framestats"
        else:
            command = "dumpsys SurfaceFlinger --timestats -clear"
            if dump:
                command += " -dump"
        with self._reset_locks[source]:
            if self._closed.is_set():
                return "", None
            started = time.monotonic()
            raw = self._backend._safe_shell(self._serial, command)
            if raw.startswith("(adb error:"):
                return "", None
            # The device performed both actions inside this round trip.
            return raw, (started + time.monotonic()) / 2

    def _read_window(self, duration_sec: float, sample_pid: int):
        now = time.monotonic()
        deadline = None
        if duration_sec > 0:
            # Fixed deadlines absorb parsing/IPC time. After a missed period
            # begin a fresh full window, never emit a burst of catch-up rows.
            last = self._last_deadline
            deadline = (last if last is not None else now) + duration_sec
            if deadline <= now:
                deadline = now + duration_sec
            self._last_deadline = deadline
        # A row has a delivery deadline. Device commands have independent,
        # bounded lifetimes: a 650ms dumpsys still produces a real reading
        # for the next 500ms row. Keep a single in-flight request per service.
        probe_deadline = now + max(12.0, duration_sec)
        active = {future: name for name, future in self._pending.items()}
        readings = {}
        observation_ns = {}
        requests = {}

        def submit(name, operation, *args, **kwargs):
            if name in readings:
                return
            requests[name] = (operation, args, kwargs)
            future = self._submit_probe(name, probe_deadline, operation, *args, **kwargs)
            if future is not None:
                active[future] = name

        def shell(name, command):
            submit(name, self._backend._safe_shell, self._serial, command)

        def parse_sysfs(value):
            fields = parsers.parse_sysfs_bundle(
                value, [label for label, _ in self._thermal_map], self._core_count
            )
            readings["sysfs_fields"] = fields
            needs_fallback = fields["gpu_busy_percent"] is None or fields["gpu_clock_mhz"] is None
            if needs_fallback and time.monotonic() >= self._gpu_retry_at:
                shell("gpu", _GPU_FALLBACK_COMMAND)

        system_command = "cat /proc/stat /proc/meminfo /proc/net/dev 2>/dev/null"
        if self.proc_stream is not None:
            # /proc/stat's per-interrupt counters can be much larger than all
            # CPU lines combined. Only the leading cpu* lines are consumed.
            system_command = (
                "sed -n '/^cpu/!q;p' /proc/stat 2>/dev/null; "
                "cat /proc/meminfo /proc/net/dev 2>/dev/null"
            )
        proc_command = system_command
        if self.proc_stream is None:
            if sample_pid:
                separator = "; echo; echo _MPI_PROC_BOUNDARY_; "
                proc_command = separator.join((
                    system_command, f"cat /proc/{sample_pid}/status 2>/dev/null",
                    f"cat /proc/{sample_pid}/stat 2>/dev/null",
                ))
                submit("memory", self._read_chart_proc, sample_pid)
            shell("proc", proc_command + "; true")
            shell("sysfs", self._sysfs_script)
        else:
            separator = "; echo; echo _MPI_FAST_SECTION_; "
            command = separator.join((
                chart_memory.proc_command(sample_pid) if sample_pid else "true",
                proc_command, self._sysfs_script,
            ))
            submit("proc", self.proc_stream.read, command)
        for source in self._frame_sources:
            submit(source, self._frame_counter, source, True)
        shell("battery", "dumpsys battery")
        shell("focus", "dumpsys activity activities | grep -E ResumedActivity || true")

        while active:
            remaining = max(0.0, deadline - time.monotonic()) if deadline is not None else None
            done, _ = wait((*active, self._close_future), remaining, FIRST_COMPLETED)
            if self._close_future in done or not done:
                break
            for future in done:
                name = active.pop(future)
                try:
                    value, ended, observed, generation = future.result()
                except adb.AdbError:
                    # A failed PID query says nothing about whether the app exited.
                    del self._pending[name]
                    continue
                except Exception:
                    del self._pending[name]
                    raise
                if deadline is not None and ended > deadline:
                    # Complete, but belongs to the next delivery window.
                    continue
                del self._pending[name]
                if generation != self._observation_generation:
                    # The request started before a pause transition or PID change.
                    if name in requests:
                        operation, args, kwargs = requests[name]
                        submit(name, operation, *args, **kwargs)
                    continue
                observation_ns[name] = observed
                readings[name] = value
                if name in ("gfxinfo", "sf"):
                    raw, midpoint = value
                    baseline = self._frame_baseline.get(name)
                    self._frame_baseline[name] = midpoint
                    # Both counter resets completed. Use their true interval
                    # even if one command took longer than a delivery period.
                    if midpoint is not None and baseline is not None:
                        readings[name] = raw, midpoint - baseline
                    else:
                        readings[name] = "", 0.0
                elif name == "proc":
                    if self.proc_stream is not None:
                        fast_sections = value.split("\n_MPI_FAST_SECTION_\n")
                        if len(fast_sections) != 3:
                            continue
                        memory_raw, value, sysfs_raw = fast_sections
                        sections = [value]
                        if sample_pid:
                            readings["memory"] = chart_memory.read_capture_proc(
                                memory_raw, sample_pid, self.package.bundle_id
                            )
                            observation_ns["memory"] = observed
                            # The verified memory read already captured status
                            # and stat. Share its exact strings without extra
                            # device reads or a second copy over the wire.
                            sections.extend(chart_memory.process_stat_and_status(memory_raw, sample_pid))
                        readings["sysfs"] = sysfs_raw
                        observation_ns["sysfs"] = observed
                        parse_sysfs(sysfs_raw)
                    else:
                        sections = value.split("\n_MPI_PROC_BOUNDARY_\n")
                    if len(sections) != (3 if sample_pid else 1):
                        continue
                    system = readings["sysinfo"] = sections[0]
                    if sample_pid:
                        readings["status"], readings["stat"] = sections[1:]
                        readings["jiffies"] = parsers.parse_proc_stat_jiffies(sections[2])
                        if readings["jiffies"] is None:
                            submit(
                                "pid", self._backend._resolve_pid, self._serial,
                                self.package.bundle_id, self.package.uid, raise_on_error=True,
                            )
                    readings["core_ticks"] = parsers.parse_proc_stat_cpu_cores(system)
                    readings["sys_ticks"] = parsers.parse_proc_stat_cpu_ticks(system)
                    if not readings["core_ticks"] and readings["sys_ticks"] is None:
                        shell("cpuinfo", "dumpsys cpuinfo")
                elif name == "sysfs":
                    parse_sysfs(value)
        if deadline is not None:
            self._closed.wait(max(0.0, deadline - time.monotonic()))
        if self._closed.is_set():
            raise RuntimeError("capture session closed")
        return readings, observation_ns

    def sample(self, duration_sec: float) -> MetricsSnapshot:
        bundle_id = self.package.bundle_id
        sample_pid = self.pid
        readings, observation_ns = self._read_window(duration_sec, sample_pid)
        now_ns = observation_ns.get("proc")
        if readings.get("jiffies") is not None:
            self._gone_streak = 0
        elif "pid" in readings:
            self._check_app_alive(readings["pid"])
            if self.pid != sample_pid:
                for key in ("status", "stat", "memory", *self._frame_sources):
                    readings.pop(key, None)
        gfxinfo, gfx_window = readings.get("gfxinfo", ("", 0.0))
        sf_timestats, sf_window = readings.get("sf", ("", 0.0))
        sysfs_raw = readings.get("sysfs", "")
        battery_raw = readings.get("battery", "")
        sysinfo_raw = readings.get("sysinfo", "")
        proc_status = readings.get("status", "")
        proc_stat = readings.get("stat", "")
        anon_kib, swap_kib, mem_state, mem_start = readings.get("memory", (None, None, None, None))
        if "focus" in readings:
            self._check_app_foreground(readings["focus"])

        frames = parsers.parse_gfxinfo(gfxinfo)
        fresh_events = []
        current_frame_timestamp = {}
        for event in frames.raw_events:
            stream = f"{self._frame_stream_id}:{self.pid}:{event['stream_id']}"
            timestamp = event["timestamp_ns"]
            if timestamp <= self._last_frame_timestamp.get(stream, -1):
                continue
            event["stream_id"] = stream
            fresh_events.append(event)
            current_frame_timestamp[stream] = max(timestamp, current_frame_timestamp.get(stream, -1))
        self._last_frame_timestamp.update(current_frame_timestamp)
        frames.raw_events = fresh_events
        # A view can be recreated often; keep the session's dedup map bounded.
        if len(self._last_frame_timestamp) > 128:
            self._last_frame_timestamp = dict(list(self._last_frame_timestamp.items())[-128:])
        _merge_surfaceflinger_frame_stats(
            frames,
            sf_timestats=sf_timestats,
            bundle_id=bundle_id,
        )
        _finalize_frame_fps(
            frames,
            sf_timestats=sf_timestats,
            bundle_id=bundle_id,
            duration_sec=sf_window if frames.source == "android_surfaceflinger_timestats" else gfx_window,
            ceiling_hz=self._fps_ceiling_hz,
        )

        rss_kb = parsers.parse_proc_status_rss_kb(proc_status)
        memory = MemStats(
            pss_total_kb=None, rss_total_kb=rss_kb,
            memory_source=chart_memory.SOURCE,
            anon_rss_kib=anon_kib, swap_kib=swap_kib,
            anon_rss_plus_swap_kib=anon_kib + swap_kib if anon_kib is not None and swap_kib is not None else None,
            play_memory_state=mem_state,
            play_memory_state_source="measured_oom_score_adj" if mem_state else None,
            play_memory_category=self._memory_category[0],
            play_memory_category_source=self._memory_category[1],
            play_memory_ram_kib=self._mem_total_kb or None,
            play_memory_sdk_int=self.device.android_sdk_int,
            play_memory_form_factor=self._memory_form_factor,
            play_memory_pid=sample_pid if mem_start is not None else None,
            play_memory_start_ticks=mem_start,
        )

        cpu = CpuStats(pid=self.pid, cpu_percent=None)
        jiffies = readings.get("jiffies")
        cpu_window = None
        if jiffies is not None:
            starttime = parsers.parse_proc_stat_starttime(proc_stat)
            same_process = self._last_starttime is None or starttime == self._last_starttime
            if same_process and self._last_jiffies is not None and self._last_wall_ns is not None:
                dj = jiffies - self._last_jiffies
                dns = now_ns - self._last_wall_ns
                if dns > 0 and dj >= 0 and self._hz > 0:
                    cpu_window = dns / 1e9
                    cpu.cpu_percent = (dj / self._hz) / cpu_window * 100.0
            self._last_jiffies = jiffies
            self._last_wall_ns = now_ns
            self._last_starttime = starttime
        elif "stat" in readings:
            # An actual unreadable stat breaks process identity continuity.
            # An unfinished request leaves the last observed baseline intact.
            self._last_jiffies = None
            self._last_wall_ns = None
            self._last_starttime = None
        cpu.pid = self.pid
        if self._mem_total_kb > 0 and rss_kb is not None:
            cpu.mem_percent = rss_kb / self._mem_total_kb * 100.0

        # The displayed total is the arithmetic mean of individually measured
        # core usages, not a weighted aggregate that can hide hotplug changes.
        core_ticks = readings.get("core_ticks", {})
        self._cpu_width = max(self._cpu_width, max(core_ticks, default=-1) + 1)
        cpu.logical_core_count = self._core_count or None
        cpu.per_core_percent = [None] * self._cpu_width
        for index, ticks in core_ticks.items():
            previous = self._last_core_ticks.get(index)
            if previous is not None:
                cpu.per_core_percent[index] = parsers.cpu_ticks_percent(previous, ticks)
        measured = [value for value in cpu.per_core_percent if value is not None]
        cpu.total_status = "unavailable"
        if core_ticks:
            cpu.total_source = "proc_stat_per_core"
            if measured:
                cpu.total_percent = sum(measured) / len(measured)
                cpu.total_status = "measured" if len(measured) == self._cpu_width else "partial"
            else:
                cpu.total_status = "warming_up"

        # Older/restricted devices may expose only the aggregate row. Preserve
        # that separately identified fallback rather than inventing per-core data.
        sys_ticks = readings.get("sys_ticks")
        if sys_ticks is not None:
            if not core_ticks:
                cpu.total_source = "proc_stat_aggregate"
                cpu.total_status = "warming_up"
            if not core_ticks and self._last_sys_ticks is not None:
                cpu.total_percent = parsers.cpu_ticks_percent(self._last_sys_ticks, sys_ticks)
                if cpu.total_percent is not None:
                    cpu.total_status = "measured"
        cpuinfo_raw = ""
        if not core_ticks and sys_ticks is None:
            # Android SELinux may hide /proc/stat from the shell while the
            # system service still exposes its measured whole-device total.
            cpuinfo_raw = readings.get("cpuinfo", "")
            fallback_total = parsers.parse_cpuinfo_total(cpuinfo_raw)
            if fallback_total is not None:
                cpu.total_percent = fallback_total
                cpu.total_source = "dumpsys_cpuinfo"
                cpu.total_status = "measured"
        if "sysinfo" in readings:
            self._last_sys_ticks = sys_ticks
            self._last_core_ticks = core_ticks

        # System-wide RAM: used = MemTotal - MemAvailable.
        sys_mem_total, sys_mem_avail = parsers.parse_proc_meminfo(sysinfo_raw)
        if sys_mem_total:
            memory.sys_total_kb = sys_mem_total
            if sys_mem_avail is not None:
                memory.sys_used_kb = max(0, sys_mem_total - sys_mem_avail)

        network_rx, network_tx = parsers.parse_proc_net_dev(sysinfo_raw)
        network = NetworkStats(rx_bytes=network_rx, tx_bytes=network_tx)
        if network_rx is not None and network_tx is not None:
            if (
                self._last_net_rx_bytes is not None
                and self._last_net_tx_bytes is not None
                and self._last_net_wall_ns is not None
            ):
                elapsed_sec = (now_ns - self._last_net_wall_ns) / 1e9
                delta_rx = network_rx - self._last_net_rx_bytes
                delta_tx = network_tx - self._last_net_tx_bytes
                # Counter reset (e.g. after a network-interface restart) is not
                # zero throughput. Leave this one sample unavailable and reseed.
                if elapsed_sec > 0 and delta_rx >= 0 and delta_tx >= 0:
                    network.rx_kbps = delta_rx / 1024.0 / elapsed_sec
                    network.tx_kbps = delta_tx / 1024.0 / elapsed_sec
            self._last_net_rx_bytes = network_rx
            self._last_net_tx_bytes = network_tx
            self._last_net_wall_ns = now_ns

        sysfs_parsed = readings.get("sysfs_fields")
        if sysfs_parsed is None:
            sysfs_parsed = parsers.parse_sysfs_bundle("", [], self._core_count)
        cpu.per_core_freq_mhz = sysfs_parsed["cpu_freqs_mhz"]

        # Frequency-normalized app CPU% (0–100). app cpu_percent sums all threads
        # (can exceed 100% on multi-core), so divide by cores to a 0–100 basis,
        # then scale by how hard the cores are actually clocked right now:
        #   norm = min(100, app% / cores) * (Σ current freq / Σ max freq)
        frequency_ns = observation_ns.get("sysfs")
        if (now_ns is not None and frequency_ns is not None
                and (duration_sec <= 0 or abs(frequency_ns - now_ns) <= duration_sec * 1e9)):
            cpu.cpu_norm = _frequency_normalized_app_cpu(
                cpu.cpu_percent, self._core_count, cpu.per_core_freq_mhz,
                bool(sysfs_parsed["cpu_freqs_complete"]), self._max_freq_total_mhz,
            )

        gpu = GpuStats(
            busy_percent=sysfs_parsed["gpu_busy_percent"],
            clock_mhz=sysfs_parsed["gpu_clock_mhz"],
        )
        gpu_fallback_raw = ""
        if gpu.busy_percent is None or gpu.clock_mhz is None:
            gpu_fallback_raw = readings.get("gpu", "")
            gpu_fallback = parsers.parse_gpu_fallback(gpu_fallback_raw)
            if "gpu" in readings and not gpu_fallback_raw.startswith("(adb error:") and all(
                value is None for value in gpu_fallback.values()
            ):
                # GPU nodes are vendor-specific. Avoid a shell/glob scan every
                # tick when none report counters, and retry after a GPU wake.
                self._gpu_retry_at = time.monotonic() + 3.0
            if gpu.busy_percent is None:
                gpu.busy_percent = gpu_fallback["busy_percent"]
            if gpu.clock_mhz is None:
                gpu.clock_mhz = gpu_fallback["clock_mhz"]

        thermal = ThermalStats(
            zones_c=self._aggregate_thermal(sysfs_parsed["thermal_c"])
        )
        battery_temp_in_sysfs = "battery" in thermal.zones_c
        _add_dumpsys_battery_thermal_fallback(thermal.zones_c, battery_raw)

        battery = parsers.parse_battery(battery_raw)

        # A disconnected or completely stalled transport must not produce a
        # row of synthetic zeroes or start an automatic measurement timer.
        if not any((
            frames.source, jiffies is not None,
            cpu.total_source, rss_kb is not None, mem_start is not None, sys_mem_total,
            network_rx is not None, gpu.busy_percent is not None, gpu.clock_mhz is not None,
            any(cpu.per_core_freq_mhz), thermal.zones_c,
            any(value is not None for value in vars(battery).values()),
        )):
            raise adb.AdbError("no Android metrics available for this sample")

        raw = {
            "gfxinfo": gfxinfo,
            "proc_status": proc_status,
            "proc_stat": proc_stat,
            "cpuinfo": cpuinfo_raw,
            "gpu_fallback": gpu_fallback_raw,
            "sf_timestats": sf_timestats,
            "sysfs": sysfs_raw,
            "battery": battery_raw,
            "network": {"rx_bytes": network_rx, "tx_bytes": network_tx},
        }

        # Rows can include newly received observations from an earlier window.
        # Record their age and the actual interval used for a cumulative rate.
        emitted_ns = time.monotonic_ns()
        metric_status = {}

        def record(key, probe, source, scope, value, window=None):
            at = observation_ns.get(probe)
            if at is None or value is None:
                return
            age = max(0, emitted_ns - at) / 1e9
            status = {
                "source": source, "scope": scope,
                "status": "held" if duration_sec > 0 and age > duration_sec else "fresh",
                "age_sec": round(age, 3),
            }
            if window is not None:
                status["window_sec"] = round(window, 4)
            metric_status[key] = status

        sf_frames = frames.source == "android_surfaceflinger_timestats"
        record("fps", "sf" if sf_frames else "gfxinfo", frames.source, frames.scope,
               frames.fps_estimate, sf_window if sf_frames else gfx_window)
        record("cpu", "proc", "proc_pid_stat", "selected_process", cpu.cpu_percent, cpu_window)
        record("cpu_norm", "proc", "proc_pid_stat", "selected_process", cpu.cpu_norm, cpu_window)
        record("cpu_total", "cpuinfo" if cpu.total_source == "dumpsys_cpuinfo" else "proc",
               cpu.total_source, "system_device", cpu.total_percent)
        record("mem_bytes", "memory", chart_memory.SOURCE, "selected_process", memory.anon_rss_plus_swap_kib)
        record("gpu", "gpu" if sysfs_parsed["gpu_busy_percent"] is None else "sysfs",
               "android_sysfs", "system_device", gpu.busy_percent)
        record("temperature", "sysfs" if battery_temp_in_sysfs else "battery",
               "android_sysfs" if battery_temp_in_sysfs else "dumpsys_battery",
               "system_device", thermal.zones_c.get("battery"))
        for key, value in (("battery_current", battery.current_ma), ("battery_voltage", battery.voltage_mv),
                           ("battery_level", battery.level_percent)):
            record(key, "battery", "dumpsys_battery", "system_device", value)

        return MetricsSnapshot(
            device=self.device,
            package=self.package,
            captured_at=datetime.now(timezone.utc).isoformat(),
            duration_sec=duration_sec,
            frames=frames,
            memory=memory,
            cpu=cpu,
            network=network,
            gpu=gpu,
            thermal=thermal,
            battery=battery,
            raw=raw,
            metric_status=metric_status,
        )
