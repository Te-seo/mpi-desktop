import type { ReactNode } from 'react'
import { fireEvent, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

vi.mock('recharts', () => {
  const Container = ({ children }: { children?: ReactNode }) => <div>{children}</div>
  const Empty = () => null
  return {
    CartesianGrid: Empty,
    Line: ({
      name,
      stroke,
      strokeDasharray,
    }: {
      name?: string
      stroke?: string
      strokeDasharray?: string
    }) => (
      <div
        data-testid="chart-line"
        data-name={name}
        data-stroke={stroke}
        data-dash={strokeDasharray ?? ''}
      />
    ),
    LineChart: ({ children, onMouseMove }: { children?: ReactNode; onMouseMove: (state: { activeLabel?: number }) => void }) => (
      <div data-testid="analysis-chart" onMouseMove={() => onMouseMove({ activeLabel: 2 })}>{children}</div>
    ),
    ResponsiveContainer: Container,
    Tooltip: ({ active, content }: { active?: boolean; content: (props: { active: boolean; payload: { payload: { t: number } }[] }) => ReactNode }) => (
      <div data-testid="analysis-tooltip">{content({ active: active !== false, payload: [{ payload: { t: 2 } }] })}</div>
    ),
    XAxis: Empty,
    YAxis: Empty,
  }
})

import { renderWithLocale } from '../../test/utils'
import { makeAnalysis } from '../../test/fixtures'
import { DashboardCharts } from './DashboardCharts'
import { METRIC_COLORS, SESSION_COLORS } from './sessionColors'

describe('DashboardCharts CPU presentation', () => {
  it('plots system and whole-device app CPU together and retains normalized CPU', () => {
    renderWithLocale(<DashboardCharts analyses={[makeAnalysis()]} />)

    expect(screen.getByText('CPU utilization')).toBeInTheDocument()
    expect(screen.getByText('System CPU')).toBeInTheDocument()
    expect(screen.getByText('App CPU (whole device)')).toBeInTheDocument()
    expect(screen.getByText('App CPU (normalized)')).toBeInTheDocument()

    const names = screen.getAllByTestId('chart-line').map((line) => line.getAttribute('data-name'))
    expect(names).toContain('A · System CPU')
    expect(names).toContain('A · App CPU (whole device)')
    expect(names).toContain('A · App CPU (normalized)')
  })

  it('explains Android clock-data availability on the normalized chart', () => {
    renderWithLocale(<DashboardCharts analyses={[makeAnalysis()]} />)
    expect(screen.getByText(/otherwise the normalized value is unavailable/i)).toBeInTheDocument()
  })

  it('keeps metric colors for a single session', () => {
    renderWithLocale(<DashboardCharts analyses={[makeAnalysis()]} />)

    const lines = screen.getAllByTestId('chart-line')
    const fps = lines.find((line) => line.getAttribute('data-name')?.endsWith('FPS'))
    const systemCpu = lines.find((line) =>
      line.getAttribute('data-name')?.endsWith('System CPU')
    )
    const appCpu = lines.find((line) =>
      line.getAttribute('data-name')?.endsWith('App CPU (whole device)')
    )

    expect(fps).toHaveAttribute('data-stroke', METRIC_COLORS.fps)
    expect(systemCpu).toHaveAttribute('data-stroke', METRIC_COLORS.cpu)
    expect(appCpu).toHaveAttribute('data-stroke', METRIC_COLORS.cpu_app)
  })

  it('preserves A/C/D session slots across partial loads and keys hiding by path', () => {
    const analyses = [
      makeAnalysis({ file_path: '/A.csv' }),
      makeAnalysis({ file_path: '/C.csv' }),
      makeAnalysis({ file_path: '/D.csv' }),
    ]
    renderWithLocale(
      <DashboardCharts
        analyses={analyses}
        selectedPaths={['/A.csv', '/B.csv', '/C.csv', '/D.csv']}
      />
    )

    const findCpuLine = (slot: string, label: string) =>
      screen
        .getAllByTestId('chart-line')
        .find((line) => {
          const name = line.getAttribute('data-name') ?? ''
          return name.startsWith(slot) && name.endsWith(label)
        })

    const aSystem = findCpuLine('A', 'System CPU')
    const cSystem = findCpuLine('C', 'System CPU')
    const cApp = findCpuLine('C', 'App CPU (whole device)')
    const dSystem = findCpuLine('D', 'System CPU')

    expect(aSystem).toHaveAttribute('data-stroke', SESSION_COLORS[0])
    expect(cSystem).toHaveAttribute('data-stroke', SESSION_COLORS[2])
    expect(cApp).toHaveAttribute('data-stroke', SESSION_COLORS[2])
    expect(dSystem).toHaveAttribute('data-stroke', SESSION_COLORS[3])
    expect(cSystem).toHaveAttribute('data-dash', '')
    expect(cApp).toHaveAttribute('data-dash', '5 3')

    fireEvent.click(screen.getByRole('button', { name: 'C' }))
    expect(findCpuLine('C', 'System CPU')).toBeUndefined()
    expect(findCpuLine('A', 'System CPU')).toBeDefined()
    expect(findCpuLine('D', 'System CPU')).toBeDefined()
  })
})

describe('DashboardCharts hover dismissal', () => {
  it('does not retain a tooltip after plot leave or window blur', () => {
    renderWithLocale(<DashboardCharts analyses={[makeAnalysis()]} />)
    const tooltip = screen.getAllByTestId('analysis-tooltip')[0]
    const chart = screen.getAllByTestId('analysis-chart')[0]
    expect(tooltip).toBeEmptyDOMElement()
    fireEvent.mouseMove(chart)
    expect(tooltip).not.toBeEmptyDOMElement()
    fireEvent.mouseLeave(screen.getByTestId('analysis-plot-fps'))
    expect(tooltip).toBeEmptyDOMElement()
    fireEvent.mouseMove(chart)
    fireEvent.blur(window)
    expect(tooltip).toBeEmptyDOMElement()
  })
  it('hides FPS collection source and scope in the analysis chart', () => {
    renderWithLocale(<DashboardCharts analyses={[makeAnalysis({ metric_provenance: {
      fps_sources: ['ios_graphics_core_animation'], fps_scopes: ['system_display'], cpu_total_sources: [], status_counts: {},
    } })]} />)
    expect(screen.queryByText(/ios_graphics_core_animation|system_display/)).toBeNull()
  })
})
