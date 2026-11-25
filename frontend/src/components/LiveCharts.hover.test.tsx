import type { ReactNode } from 'react'
import { act, fireEvent, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('recharts', () => {
  const Container = ({ children }: { children?: ReactNode }) => <div>{children}</div>
  const Empty = () => null
  return {
    Area: Empty, CartesianGrid: Empty, Line: Empty, ReferenceArea: Empty, ReferenceLine: Empty, XAxis: Empty, YAxis: Empty,
    ResponsiveContainer: Container,
    ComposedChart: ({ children, onMouseMove }: { children?: ReactNode; onMouseMove: (state: { activeLabel?: number }) => void }) => (
      <div data-testid="plot" onMouseMove={() => onMouseMove({ activeLabel: 1 })}>
        <button type="button" onClick={() => onMouseMove({})}>Axis area</button>{children}
      </div>
    ),
  }
})

import { LiveCrosshairProvider } from './LiveCrosshairContext'
import { MetricCard } from './MetricCard'
import { HoverOverlay } from './HoverOverlay'
import { renderWithLocale } from '../test/utils'

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
