import type { ReactNode } from 'react'
import { act, fireEvent, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('recharts', () => {
  const Container = ({ children }: { children?: ReactNode }) => <div>{children}</div>
  const Empty = () => null
  return {
    Area: Empty, CartesianGrid: Empty, Line: Empty, ReferenceArea: Empty, ReferenceLine: Empty, XAxis: Empty, YAxis: Empty,
    ResponsiveContainer: Container,
    ComposedChart: ({ children, onMouseDown, onMouseMove }: {
      children?: ReactNode
      onMouseDown: (state: { activeLabel: number }) => void
      onMouseMove: (state: { activeLabel?: number }) => void
    }) => (
      <div data-testid="plot" onMouseDown={() => onMouseDown({ activeLabel: 0 })} onMouseMove={() => onMouseMove({ activeLabel: 1 })}>
        <button type="button" onClick={() => onMouseMove({})}>Axis area</button>{children}
      </div>
    ),
  }
})

import { LiveCrosshairProvider } from './LiveCrosshairContext'
import { LiveCharts } from './LiveCharts'
import { MetricCard } from './MetricCard'
import { HoverOverlay } from './HoverOverlay'
import { renderWithLocale } from '../test/utils'
import type { MetricsSnapshot } from '../lib/types'

let pending: Map<number, () => void>
let frameId = 0
function flushHover() { act(() => { const callbacks = [...pending.values()]; pending.clear(); callbacks.forEach((callback) => callback()) }) }
function mount() {
  const rows = [{ t: 1, fps: 60, cpu_total: 10, cpu_app: 5, gpu_busy: 20, 'thermal:battery': 30, power_mw: 300 }]
  return renderWithLocale(<LiveCrosshairProvider>
    <MetricCard metricKey="fps" label="FPS" accent="#f59e6b" unit="fps" digits={0} data={rows} primaryKey="fps" />
    <HoverOverlay data={rows} />
  </LiveCrosshairProvider>)
}
function hover() { fireEvent.mouseMove(screen.getByTestId('plot')); flushHover(); expect(screen.getByRole('tooltip')).toBeInTheDocument() }

beforeEach(() => {
  pending = new Map()
  vi.stubGlobal('requestAnimationFrame', (callback: () => void) => { const id = ++frameId; pending.set(id, callback); return id })
  vi.stubGlobal('cancelAnimationFrame', (id: number) => { pending.delete(id) })
})
afterEach(() => vi.unstubAllGlobals())

it('displays measured iOS app memory in MiB on the card, hover and selected range without total RAM', () => {
  const snapshots: MetricsSnapshot[] = [128, 256].map((mib, index) => ({
    device: { serial: 'ios', platform: 'ios', model: 'iPhone', os_version: '18', connection_type: 'usb' },
    package: { bundle_id: 'app', label: null, version_name: null, version_code: null, apk_path: null, uid: null },
    captured_at: new Date(index * 1000).toISOString(), duration_sec: 1, elapsed_sec: index,
    frames: { total_frames: 0, janky_frames: 0, small_jank: 0, big_jank: 0, jank_percent: 0, p50_ms: 0,
      p90_ms: 0, p95_ms: 0, p99_ms: 0, missed_vsync: 0, slow_ui_thread: 0, frame_deadline_missed: 0, fps_estimate: null },
    cpu: { pid: 1, cpu_percent: null, mem_percent: 0, total_percent: null, per_core_freq_mhz: [] },
    memory: { pss_total_kb: null, rss_total_kb: mib * 1024, sys_total_kb: 0, sys_used_kb: 0, java_heap_kb: 0,
      native_heap_kb: 0, graphics_kb: 0, code_kb: 0, stack_kb: 0 },
    gpu: { busy_percent: null, clock_mhz: null, temp_c: null },
    thermal: { zones_c: {} }, battery: { level_percent: null, current_ma: null, status: null, voltage_mv: null }, raw: {},
  }))
  renderWithLocale(<LiveCharts snapshots={snapshots} visibility={{}} order={['mem']} onReorder={() => {}} />)
  expect(screen.getByTestId('metric-current-mem')).toHaveTextContent('256.0MiB')
  fireEvent.mouseDown(screen.getByTestId('plot'))
  hover()
  expect(screen.getByRole('tooltip')).toHaveTextContent('iOS app memory256.0MiB')
  const rangeSummary = screen.getByText(/2 samples/).parentElement!.parentElement!
  expect(rangeSummary).toHaveTextContent('iOS app memory192.0MiB128.0MiB256.0MiB')
})

describe('live chart hover boundaries', () => {
  it('dismisses the tooltip outside the native plot and on invalid active labels', () => {
    mount(); hover()
    fireEvent.mouseLeave(screen.getByTestId('plot').closest('[data-live-chart-plot]')!)
    expect(screen.queryByRole('tooltip')).toBeNull()
    hover(); fireEvent.click(screen.getByRole('button', { name: 'Axis area' }))
    expect(screen.queryByRole('tooltip')).toBeNull()
  })
  it('cancels a queued hover on outside movement so it cannot reopen after leaving', () => {
    mount()
    fireEvent.mouseMove(screen.getByTestId('plot'))
    fireEvent.mouseMove(document.body)
    flushHover()
    expect(screen.queryByRole('tooltip')).toBeNull()
  })
  it('dismisses hover when switching views, scrolling or losing the window', () => {
    mount(); hover(); fireEvent.pointerDown(document.body)
    expect(screen.queryByRole('tooltip')).toBeNull()
    hover(); fireEvent.scroll(window)
    expect(screen.queryByRole('tooltip')).toBeNull()
    hover(); fireEvent.blur(window)
    expect(screen.queryByRole('tooltip')).toBeNull()
    hover(); fireEvent(document, new Event('visibilitychange'))
    expect(screen.queryByRole('tooltip')).toBeNull()
  })
})
