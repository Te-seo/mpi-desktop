import { describe, expect, it } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithLocale } from '../../test/utils'
import { makeAnalysis, makeStats } from '../../test/fixtures'
import { SessionAnalysisView } from './SessionAnalysisView'

describe('SessionAnalysisView', () => {
  it('keeps the user alias separate from recorded iOS model identity', () => {
    renderWithLocale(<SessionAnalysisView analysis={makeAnalysis({
      platform: 'ios', device_model: 'iPhone 18 Pro Max', device_display_name: 'Lab phone',
      device_model_identifier: 'iPhone19,3', device_model_number: 'A3473',
    })} />)
    expect(screen.getByText('Lab phone')).toBeInTheDocument()
    expect(screen.getByText('iPhone 18 Pro Max')).toBeInTheDocument()
    expect(screen.getByText('iPhone19,3')).toBeInTheDocument()
    expect(screen.getByText('A3473')).toBeInTheDocument()
  })
  it('shows the recorded firmware build separately from OS and app versions', () => {
    renderWithLocale(<SessionAnalysisView analysis={makeAnalysis({
      device_build_version: 'S928NKSU3AXJ1', device_build_name: 'UP1A.231005.007.S928NKSU3AXJ1',
    })} />)
    expect(screen.getByText('Build Version')).toBeInTheDocument()
    expect(screen.getByText('S928NKSU3AXJ1')).toBeInTheDocument()
    expect(screen.getByText('UP1A.231005.007.S928NKSU3AXJ1')).toBeInTheDocument()
  })
  it('distinguishes observed session percentiles, worst windows, and raw-event loss', () => {
    const base = makeAnalysis()
    renderWithLocale(<SessionAnalysisView analysis={makeAnalysis({
      frames: { ...base.frames, raw_frame_count: 101, raw_frames_dropped: 2, session_p95_ms: 10, session_p99_ms: 10, worst_window_p95_ms: 100,
        raw_frame_scope: ['system_display'], raw_frame_sources: ['ios_kdebug_display_experimental'] },
      metric_provenance: { fps_sources: ['ios_graphics_core_animation'], fps_scopes: ['system_display'], cpu_total_sources: [], status_counts: {} },
    })} />)
    expect(screen.getByText('Session frame P95 / P99 (ms)')).toBeInTheDocument()
    expect(screen.getByText('Worst-window P95')).toBeInTheDocument()
    expect(screen.getByText('101 / 2')).toBeInTheDocument()
    expect(screen.getByText(/Some raw events were dropped/)).toBeInTheDocument()
    expect(screen.queryByText(/ios_graphics_core_animation/)).toBeNull()
    expect(screen.queryByText(/ios_kdebug_display_experimental|system_display/)).toBeNull()
  })
  it('shows the partial-data warning when FPS coverage < 80%', () => {
    const base = makeAnalysis()
    const a = makeAnalysis({
      metrics: {
        ...base.metrics,
        fps: makeStats({ coverage_percent: 50, valid_count: 5, total_count: 10 }),
      },
    })
    renderWithLocale(<SessionAnalysisView analysis={a} />)
    expect(screen.getByText(/Partial data/i)).toBeInTheDocument()
  })

  it('hides the warning at full coverage and shows the KPI labels', () => {
    renderWithLocale(<SessionAnalysisView analysis={makeAnalysis()} />)
    expect(screen.queryByText(/Partial data/i)).toBeNull()
    expect(screen.getByText('Average FPS')).toBeInTheDocument()
    expect(screen.getByText('Median FPS')).toBeInTheDocument()
    expect(screen.getByText('Peak App Memory')).toBeInTheDocument()
    expect(screen.getByText('App CPU (whole device)')).toBeInTheDocument()
    expect(screen.getByText('System CPU')).toBeInTheDocument()
    expect(screen.getByText('App CPU (normalized)')).toBeInTheDocument()
    expect(screen.getByText(/raw per-core sum by logical CPUs/i)).toBeInTheDocument()
  })

  it('renders an em dash for unavailable statistics', () => {
    const base = makeAnalysis()
    const a = makeAnalysis({
      max_temp_battery: null,
      metrics: {
        ...base.metrics,
        fps: makeStats({ average: null, median: null, p05: null, coverage_percent: 100 }),
      },
    })
    renderWithLocale(<SessionAnalysisView analysis={a} />)
    expect(screen.getAllByText('—').length).toBeGreaterThan(0)
  })
})
