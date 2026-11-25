import { describe, expect, it } from 'vitest'
import { deriveFrameQualitySeries } from './liveMetrics'

describe('deriveFrameQualitySeries', () => {
  it('derives 1% low, stutter, and smooth from one frame window', () => {
    expect(
      deriveFrameQualitySeries({
        total_frames: 100,
        janky_frames: 4,
        p99_ms: 20,
      })
    ).toEqual({
      fps_1pct_low: 50,
      stutter_percent: 4,
      smooth_percent: 96,
    })
  })

  it('keeps missing or invalid frame data unavailable', () => {
    expect(
      deriveFrameQualitySeries({
        total_frames: 0,
        janky_frames: 0,
        p99_ms: 0,
      })
    ).toEqual({
      fps_1pct_low: null,
      stutter_percent: null,
      smooth_percent: null,
    })
    expect(
      deriveFrameQualitySeries({
        total_frames: 10,
        janky_frames: 1,
        p99_ms: 0,
      })
    ).toEqual({
      fps_1pct_low: null,
      stutter_percent: null,
      smooth_percent: null,
    })
    expect(
      deriveFrameQualitySeries({
        total_frames: 0,
        janky_frames: 0,
        p99_ms: 16,
      })
    ).toEqual({
      fps_1pct_low: null,
      stutter_percent: null,
      smooth_percent: null,
    })
  })

  it('clamps reversed frame counts to the valid percentage range', () => {
    expect(
      deriveFrameQualitySeries({
        total_frames: 10,
        janky_frames: 25,
        p99_ms: 10,
      })
    ).toEqual({
      fps_1pct_low: 100,
      stutter_percent: 100,
      smooth_percent: 0,
    })
    expect(
      deriveFrameQualitySeries({
        total_frames: 10,
        janky_frames: -2,
        p99_ms: 10,
      })
    ).toEqual({
      fps_1pct_low: 100,
      stutter_percent: 0,
      smooth_percent: 100,
    })
  })
})
