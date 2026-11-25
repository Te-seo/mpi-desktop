from __future__ import annotations

import math
from pathlib import Path

from mpi.android.parsers import (
    fps_from_histogram,
    jank_from_histogram,
    parse_gfxinfo,
    parse_battery_temperature_c,
    parse_meminfo,
    parse_proc_net_dev,
    parse_sf_timestats_rate_hints,
    parse_sf_timestats_refresh_hz,
    parse_sf_timestats_global,
    parse_top,
    percentiles_from_histogram,
)
from mpi.android.backend import _finalize_frame_fps
from mpi.schema import FrameStats


FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_gfxinfo_totals():
    stats = parse_gfxinfo(_read("gfxinfo_sample.txt"))
    assert stats.total_frames == 35466
    assert stats.janky_frames == 1167
    assert stats.jank_percent == 3.29


def test_battery_temperature_parses_exact_deci_celsius_field():
    text = """Current Battery Service state:
      charger temperature: 455
      temperature status: 3
      temperature: 399
    """

    assert parse_battery_temperature_c(text) == 39.9


def test_battery_temperature_ignores_similarly_named_fields():
    text = """Current Battery Service state:
      charger temperature: 455
      batteryTemperature: 410
    """

    assert parse_battery_temperature_c(text) is None


def test_gfxinfo_percentiles():
    stats = parse_gfxinfo(_read("gfxinfo_sample.txt"))
    assert stats.p50_ms == 5.0
    assert stats.p90_ms == 7.0
    assert stats.p95_ms == 11.0
    assert stats.p99_ms == 25.0


def test_gfxinfo_counters():
    stats = parse_gfxinfo(_read("gfxinfo_sample.txt"))
    assert stats.missed_vsync == 141
    assert stats.slow_ui_thread == 314
    assert stats.frame_deadline_missed == 470


def test_gfxinfo_fps_estimate():
    stats = parse_gfxinfo(_read("gfxinfo_sample.txt"))
    assert stats.fps_estimate is not None
    assert 55 <= stats.fps_estimate <= 65


def test_gfxinfo_empty_returns_zeros():
    stats = parse_gfxinfo("")
    assert stats.total_frames == 0
    assert stats.janky_frames == 0
    assert stats.jank_percent == 0.0
    assert stats.p50_ms == 0.0
    assert stats.fps_estimate is None


def test_meminfo_totals():
    stats = parse_meminfo(_read("meminfo_sample.txt"))
    assert stats.pss_total_kb == 98869
    assert stats.rss_total_kb == 157963


def test_meminfo_app_summary():
    stats = parse_meminfo(_read("meminfo_sample.txt"))
    assert stats.java_heap_kb == 9368
    assert stats.native_heap_kb == 30408
    assert stats.graphics_kb == 30000
    assert stats.code_kb == 7200
    assert stats.stack_kb == 512


def test_meminfo_empty_returns_zeros():
    stats = parse_meminfo("")
    assert stats.pss_total_kb == 0
    assert stats.rss_total_kb == 0
    assert stats.java_heap_kb == 0


def test_proc_net_dev_sums_non_loopback_interfaces_and_rejects_bad_lines():
    text = """\
Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo: 999 0 0 0 0 0 0 0 777 0 0 0 0 0 0 0
 wlan0: 1200 0 0 0 0 0 0 0 300 0 0 0 0 0 0 0
 rmnet0: 400 0 0 0 0 0 0 0 900 0 0 0 0 0 0 0
 bad0: nope 0 0 0 0 0 0 0 10 0 0 0 0 0 0 0
"""
    assert parse_proc_net_dev(text) == (1600, 1200)
    assert parse_proc_net_dev("lo: 1 0 0 0 0 0 0 0 1") == (None, None)


def test_top_matches_pid():
    stats = parse_top(_read("top_sample.txt"), pid=2094)
    assert stats.pid == 2094
    assert stats.cpu_percent == 45.6
    assert stats.mem_percent == 15.2


def test_top_unknown_pid_returns_unavailable_cpu():
    stats = parse_top(_read("top_sample.txt"), pid=99999)
    assert stats.pid == 99999
    assert stats.cpu_percent is None
    assert stats.mem_percent == 0.0


def test_top_empty_returns_unavailable_cpu():
    stats = parse_top("", pid=2094)
    assert stats.pid == 2094
    assert stats.cpu_percent is None
    assert stats.mem_percent == 0.0


# --- SurfaceFlinger timestats global (jank fallback) ---


def test_sf_timestats_global_parses_legacy_section():
    stats = parse_sf_timestats_global(_read("sf_timestats_sample.txt"))
    assert stats is not None
    # Live capture had 114 composited frames over ~3 s, with 108 missed.
    assert stats.total_frames == 114
    assert stats.missed_frames == 108
    assert stats.client_composition_frames == 0
    assert stats.display_on_time_ms == 3242
    assert stats.average_frame_duration_ms == 1.0
    # Two non-zero buckets in presentToPresent: 24ms x 64 and 33ms x 47.
    assert (24, 64) in stats.present_to_present_histogram
    assert (33, 47) in stats.present_to_present_histogram


def test_sf_timestats_global_returns_none_for_empty_or_zero():
    assert parse_sf_timestats_global("") is None
    # No legacy section
    assert parse_sf_timestats_global("layerName = foo\ntotalFrames = 99") is None
    # Legacy section but totalFrames=0 (idle device, no signal)
    text = "Legacy stats are as follows:\ntotalFrames = 0\nmissedFrames = 0\n"
    assert parse_sf_timestats_global(text) is None


def test_sf_timestats_global_ignores_per_layer_total_frames():
    """The first totalFrames= match must be the global one, not a per-layer
    block emitted later in the dump."""
    text = (
        "Legacy stats are as follows:\n"
        "totalFrames = 200\n"
        "missedFrames = 30\n"
        "displayOnTime = 1000 ms\n"
        "presentToPresent histogram is as below:\n"
        "8ms=180 16ms=20\n"
        "averageFrameDuration = 1.5 ms\n"
        "\n"
        "layerName = SurfaceView[com.example.app]\n"
        "totalFrames = 99\n"
        "missedFrames = 99\n"
    )
    stats = parse_sf_timestats_global(text)
    assert stats is not None
    assert stats.total_frames == 200, "must not pick up per-layer totalFrames"
    assert stats.missed_frames == 30


def test_jank_steady_30fps_game_on_120hz_display():
    """A game pinned at 30fps on a 120Hz panel: every frame lands at the
    33ms bucket. SF's display-relative `missedFrames` counts 75% as missed,
    but our mode-relative jank threshold should report ~0%."""
    # 36 frames in a 1-second window, all at the 33ms bucket.
    total, janky = jank_from_histogram([(33, 36)])
    assert total == 36
    assert janky == 0


def test_jank_steady_60fps_game_with_one_dropped_frame():
    """60fps mode (16ms bucket) with one outlier at 33ms = real jank."""
    total, janky = jank_from_histogram([(16, 59), (33, 1)])
    assert total == 60
    # threshold = max(16*1.5=24, 18) = 24, so 33ms exceeds.
    assert janky == 1


def test_jank_steady_120fps_uses_min_threshold_floor():
    """At 120fps target (8ms bucket) the mode*1.5 = 12ms threshold would
    flag normal 16ms VRR variance. min_threshold_ms=18 prevents that."""
    total, janky = jank_from_histogram([(8, 100), (16, 5)])
    assert total == 105
    # 16ms <= 18ms floor → not janky.
    assert janky == 0


def test_jank_empty_histogram_is_zero_zero():
    assert jank_from_histogram([]) == (0, 0)
    assert jank_from_histogram([(8, 0), (16, 0)]) == (0, 0)


def test_percentiles_from_histogram_basic():
    # 100 samples: 80 at 8ms, 15 at 16ms, 5 at 33ms.
    pcts = percentiles_from_histogram([(8, 80), (16, 15), (33, 5)])
    assert pcts[50] == 8
    assert pcts[90] == 16
    assert pcts[95] == 16
    assert pcts[99] == 33


def test_percentiles_from_histogram_empty_returns_empty_dict():
    assert percentiles_from_histogram([]) == {}
    assert percentiles_from_histogram([(8, 0), (16, 0)]) == {}


def test_fps_from_histogram_estimates_present_rate():
    fps = fps_from_histogram([(16, 60)])

    assert fps is not None
    assert 62.4 <= fps <= 62.6


def test_fps_from_histogram_corrects_60hz_integer_ms_quantization_with_hint():
    # SurfaceFlinger floors 16.667 ms to 16 ms.  The cadence hint makes that
    # otherwise-ambiguous bucket safe to reconstruct as exactly 60 Hz.
    assert fps_from_histogram([(16, 60)], cadence_hz=60.0) == 60.0
    # A 120 Hz display hint also explains 16 ms as two refresh periods.
    assert fps_from_histogram([(16, 60)], cadence_hz=120.0) == 60.0


def test_fps_from_histogram_preserves_other_common_cadences():
    cases = (
        ([(33, 30)], 60.0, 30.0),
        ([(25, 40)], 120.0, 40.0),
        ([(22, 45)], 90.0, 45.0),
        ([(11, 90)], 90.0, 90.0),
        ([(8, 120)], 120.0, 120.0),
    )
    for histogram, hint, expected in cases:
        actual = fps_from_histogram(histogram, cadence_hz=hint)
        assert actual is not None
        assert math.isclose(actual, expected, abs_tol=1e-9)


def test_fps_from_histogram_cadence_keeps_real_drops_below_60():
    # 59 normal periods plus one two-period gap: 60 presents over 61 vsyncs.
    fps = fps_from_histogram([(16, 59), (33, 1)], cadence_hz=60.0)

    assert fps is not None
    assert math.isclose(fps, 60.0 * 60 / 61, abs_tol=1e-9)
    assert fps < 60.0


def test_fps_from_histogram_cadence_handles_unique_coarse_drop_buckets():
    # 57x one-period, then 2/3/4-period gaps.  The AOSP 50 and 66 ms bucket
    # intervals each contain exactly one matching 60 Hz period multiple.
    fps = fps_from_histogram(
        [(16, 57), (33, 1), (50, 1), (66, 1)], cadence_hz=60.0
    )

    assert fps is not None
    assert math.isclose(fps, 60.0 * 60 / 66, abs_tol=1e-9)


def test_fps_from_histogram_rejects_ambiguous_or_stale_cadence_hints():
    # The 200 ms bucket spans several 60 Hz period counts, so keep the raw
    # estimate instead of inventing one exact stall duration.
    ambiguous = [(16, 59), (200, 1)]
    assert fps_from_histogram(ambiguous, cadence_hz=60.0) == fps_from_histogram(
        ambiguous
    )
    # A stale layer vote of 60 must not turn a real 120 Hz bucket into 60.
    assert fps_from_histogram([(8, 120)], cadence_hz=60.0) == 125.0
    # Mixed VRR buckets do not all fit the 120 Hz cadence; preserve the raw
    # histogram rather than snapping it to a tier.
    vrr = [(8, 50), (11, 50)]
    assert fps_from_histogram(vrr, cadence_hz=120.0) == fps_from_histogram(vrr)


def test_sf_timestats_refresh_hz_reads_layer_frame_rate():
    assert parse_sf_timestats_refresh_hz(_read("sf_timestats_sample.txt")) == 120.0


def test_sf_timestats_rate_hints_prioritize_target_layer_without_losing_display():
    text = (
        "displayRefreshRate = 120 fps\n"
        "layerName = SurfaceView[com.example.game]\n"
        "frameRate = 45.00\n"
        "refreshRate = 120.00\n"
        "layerName = SurfaceView[com.other.game]\n"
        "frameRate = 90.00\n"
    )

    assert parse_sf_timestats_rate_hints(text, "com.example.game") == [45.0, 120.0]


def test_android_fps_guard_prefers_display_histogram_over_impossible_fps():
    frames = FrameStats(fps_estimate=1000.0, total_frames=1000)
    sf_timestats = (
        "Legacy stats are as follows:\n"
        "totalFrames = 60\n"
        "presentToPresent histogram is as below:\n"
        "16ms=60\n"
        "\n"
        "layerName = SurfaceView[com.example.game]\n"
        "averageFPS = 900.0\n"
        "frameRate = 60.00\n"
        "totalFrames = 900\n"
    )

    _finalize_frame_fps(
        frames,
        sf_timestats=sf_timestats,
        bundle_id="com.example.game",
        duration_sec=1.0,
    )

    # Rejects the impossible 1000/900 fps bursts and reverses SurfaceFlinger's
    # integer-ms histogram quantization using the layer's 60 Hz cadence hint.
    assert frames.fps_estimate == 60.0


def test_android_fps_guard_corrects_matching_layer_average_fps():
    frames = FrameStats()
    sf_timestats = (
        "Legacy stats are as follows:\n"
        "totalFrames = 60\n"
        "presentToPresent histogram is as below:\n"
        "16ms=60\n"
        "\n"
        "layerName = SurfaceView[com.example.game]\n"
        "averageFPS = 62.500\n"
        "frameRate = 60.00\n"
    )

    _finalize_frame_fps(
        frames,
        sf_timestats=sf_timestats,
        bundle_id="com.example.game",
        duration_sec=1.0,
    )

    assert frames.fps_estimate == 60.0


def test_android_fps_guard_preserves_drops_in_matching_layer_average():
    frames = FrameStats()
    raw_histogram_fps = 60 / 0.977
    sf_timestats = (
        "Legacy stats are as follows:\n"
        "totalFrames = 60\n"
        "presentToPresent histogram is as below:\n"
        "16ms=59 33ms=1\n"
        "\n"
        "layerName = SurfaceView[com.example.game]\n"
        f"averageFPS = {raw_histogram_fps:.6f}\n"
        "frameRate = 60.00\n"
    )

    _finalize_frame_fps(
        frames,
        sf_timestats=sf_timestats,
        bundle_id="com.example.game",
        duration_sec=1.0,
    )

    assert frames.fps_estimate is not None
    assert math.isclose(frames.fps_estimate, 60.0 * 60 / 61, abs_tol=1e-9)


def test_android_fps_guard_keeps_ambiguous_layer_average_without_rate_hint():
    frames = FrameStats()
    sf_timestats = (
        "Legacy stats are as follows:\n"
        "totalFrames = 60\n"
        "presentToPresent histogram is as below:\n"
        "16ms=60\n"
        "\n"
        "layerName = SurfaceView[com.example.game]\n"
        "averageFPS = 62.500\n"
    )

    _finalize_frame_fps(
        frames,
        sf_timestats=sf_timestats,
        bundle_id="com.example.game",
        duration_sec=1.0,
    )

    assert frames.fps_estimate == 62.5


def test_android_fps_guard_uses_display_refresh_divisor_without_layer_vote():
    frames = FrameStats()
    sf_timestats = (
        "Legacy stats are as follows:\n"
        "totalFrames = 60\n"
        "displayRefreshRate = 120 fps\n"
        "presentToPresent histogram is as below:\n"
        "16ms=60\n"
        "\n"
        "layerName = SurfaceView[com.example.game]\n"
        "averageFPS = 62.500\n"
    )

    _finalize_frame_fps(
        frames,
        sf_timestats=sf_timestats,
        bundle_id="com.example.game",
        duration_sec=1.0,
    )

    assert frames.fps_estimate == 60.0


def test_android_fps_guard_explicit_nonstandard_layer_rate_wins():
    frames = FrameStats()
    sf_timestats = (
        "Legacy stats are as follows:\n"
        "totalFrames = 60\n"
        "displayRefreshRate = 120 fps\n"
        "presentToPresent histogram is as below:\n"
        "16ms=60\n"
        "\n"
        "layerName = SurfaceView[com.example.game]\n"
        "averageFPS = 62.500\n"
        "frameRate = 62.500\n"
    )

    _finalize_frame_fps(
        frames,
        sf_timestats=sf_timestats,
        bundle_id="com.example.game",
        duration_sec=1.0,
    )

    assert frames.fps_estimate == 62.5


def test_android_fps_guard_does_not_override_unrelated_precise_layer_fps():
    frames = FrameStats()
    sf_timestats = (
        "Legacy stats are as follows:\n"
        "totalFrames = 60\n"
        "presentToPresent histogram is as below:\n"
        "16ms=60\n"
        "\n"
        "layerName = SurfaceView[com.example.game]\n"
        "averageFPS = 119.400\n"
        "frameRate = 60.000\n"
    )

    _finalize_frame_fps(
        frames,
        sf_timestats=sf_timestats,
        bundle_id="com.example.game",
        duration_sec=1.0,
    )

    assert frames.fps_estimate == 119.4


def test_android_fps_guard_keeps_direct_63_without_surfaceflinger_signal():
    frames = FrameStats(fps_estimate=63.0)

    _finalize_frame_fps(
        frames,
        sf_timestats="",
        bundle_id="com.example.game",
        duration_sec=1.0,
    )

    assert frames.fps_estimate == 63.0


def test_android_fps_not_pinned_to_misread_refresh():
    """Regression: a ~120 fps game whose SurfaceFlinger dump reports a 60 Hz
    frameRate hint must still report its true rate, not be capped to 60 by a
    refresh-derived ceiling (the "120FPS game graphs at 60" bug)."""
    frames = FrameStats()
    sf_timestats = (
        "layerName = SurfaceView[com.example.game]\n"
        "averageFPS = 119.4\n"
        "frameRate = 60.00\n"
        "totalFrames = 1194\n"
    )

    _finalize_frame_fps(
        frames,
        sf_timestats=sf_timestats,
        bundle_id="com.example.game",
        duration_sec=10.0,
    )

    assert frames.fps_estimate == 119.4


def test_gfxinfo_zero_frames_skips_bogus_percentiles():
    """When gfxinfo has Total frames rendered: 0, the 'Nth percentile: 4950ms'
    lines are noise from an empty histogram. Parser should leave the
    percentile fields at 0 so a SurfaceFlinger fallback can fill them."""
    text = (
        "Stats since: 0ns\n"
        "Total frames rendered: 0\n"
        "Janky frames: 0 (0.00%)\n"
        "50th percentile: 4950ms\n"
        "90th percentile: 4950ms\n"
        "95th percentile: 4950ms\n"
        "99th percentile: 4950ms\n"
    )
    stats = parse_gfxinfo(text)
    assert stats.total_frames == 0
    assert stats.p50_ms == 0.0
    assert stats.p90_ms == 0.0
    assert stats.p95_ms == 0.0
    assert stats.p99_ms == 0.0
