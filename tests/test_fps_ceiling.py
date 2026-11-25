"""FPS never exceeds the panel's max refresh.

Short (0.5 s) count/window samples can overshoot — a couple of frames captured
during the reset+dump latency get divided by the nominal window, reading e.g. 65
on a 60 Hz panel or 125 on a 120 Hz panel. The fix detects the panel's max
supported refresh and caps the reported fps to it.
"""

from __future__ import annotations

from mpi.android import backend as b
from mpi.schema import FrameStats


def test_parse_max_refresh_from_dumpsys_display():
    text = (
        "mSupportedModes=[{id=1, width=1080, height=2400, fps=120.00002}, "
        "{id=2, width=1080, height=2400, fps=60.000004}, {id=3, fps=90.0}]"
    )
    assert b._parse_max_refresh_hz(text) == 120


def test_parse_max_refresh_from_surfaceflinger_hz():
    text = "Display modes:\n  1 : 1080x2400@120.000 Hz\n  2 : 1080x2400@ 60.000 Hz\n"
    assert b._parse_max_refresh_hz(text) == 120


def test_parse_max_refresh_ignores_garbage_and_empty():
    assert b._parse_max_refresh_hz("(adb error: device offline)") is None
    assert b._parse_max_refresh_hz("") is None
    # Out-of-range values (vsync periods, huge submission rates) are ignored.
    assert b._parse_max_refresh_hz("fps=0.0 fps=999.0") is None


def test_fps_capped_to_panel_120():
    f = FrameStats(total_frames=63, fps_estimate=125.0)
    b._finalize_frame_fps(f, sf_timestats="", bundle_id="x", duration_sec=0.5, ceiling_hz=120)
    assert f.fps_estimate == 120


def test_fps_capped_to_panel_60():
    f = FrameStats(total_frames=33, fps_estimate=65.0)
    b._finalize_frame_fps(f, sf_timestats="", bundle_id="x", duration_sec=0.5, ceiling_hz=60)
    assert f.fps_estimate == 60


def test_fps_under_ceiling_is_exact():
    f = FrameStats(total_frames=0, fps_estimate=117.0)
    b._finalize_frame_fps(f, sf_timestats="", bundle_id="x", duration_sec=0.5, ceiling_hz=120)
    assert f.fps_estimate == 117.0
