import type { ReactNode } from 'react'
import { fireEvent, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

vi.mock('recharts', () => {
  const Container = ({ children }: { children?: ReactNode }) => <div>{children}</div>
  const Empty = () => null
  const Series = ({
    dataKey,
    connectNulls,
    kind,
  }: {
    dataKey?: string
    connectNulls?: boolean
    kind: 'line' | 'area'
  }) => (
    <div
      data-testid={`${kind}-${dataKey}`}
      data-connect-nulls={String(connectNulls)}
    />
  )
  return {
    Area: (props: { dataKey?: string; connectNulls?: boolean }) => <Series {...props} kind="area" />,
    CartesianGrid: Empty,
    ComposedChart: Container,
    Line: (props: { dataKey?: string; connectNulls?: boolean }) => <Series {...props} kind="line" />,
    ReferenceArea: Empty,
    ReferenceLine: Empty,
    ResponsiveContainer: Container,
    XAxis: Empty,
    YAxis: Empty,
  }
})

import { LiveCrosshairProvider } from './LiveCrosshairContext'
import { MetricCard } from './MetricCard'
import { renderWithLocale } from '../test/utils'

function renderCpuCard() {
  return renderWithLocale(
    <LiveCrosshairProvider>
      <MetricCard
        metricKey="cpu"
        label="CPU utilization"
        accent="#5b9cf6"
        unit="%"
        digits={0}
        helpText="CPU formula help"
        data={[
          {
            t: 0,
            cpu_total: 33.3,
            cpu_app: 21,
            'comparison:cpu_total': 31,
            'comparison:cpu_app': 19,
          },
          {
            t: 1,
            cpu_total: null,
            cpu_app: null,
            'comparison:cpu_total': null,
            'comparison:cpu_app': null,
          },
        ]}
        primaryKey="cpu_total"
        primaryLabel="System CPU"
        secondary={[{ key: 'cpu_app', label: 'App CPU', color: '#1d4ed8', axis: 'primary' }]}
        comparisonKey="comparison:cpu_total"
        comparisonLabel="Saved"
        comparisonSecondary={[
          { key: 'comparison:cpu_app', label: 'App CPU', color: '#1d4ed8', axis: 'primary' },
        ]}
        yMin={0}
        yMax={100}
      />
    </LiveCrosshairProvider>
  )
}

describe('MetricCard CPU rendering', () => {
  it('does not revive an expired FPS value or bridge its chart gap', () => {
    renderWithLocale(<LiveCrosshairProvider><MetricCard metricKey="fps" label="FPS" accent="#f59e6b" unit="fps" digits={0}
      data={[{ t: 0, fps: 60 }, { t: 1, fps: null }]} primaryKey="fps"
      statusText="System display · Core Animation · unavailable · 4.0 s" /></LiveCrosshairProvider>)
    expect(screen.getByTestId('metric-current-fps')).toHaveTextContent('—')
    expect(screen.getByTestId('area-fps')).toHaveAttribute('data-connect-nulls', 'false')
    expect(screen.getByText(/System display · Core Animation/)).toBeInTheDocument()
  })
  it('shows the last-row null as unavailable and renders both comparison lines', () => {
    renderCpuCard()

    expect(screen.getByTestId('metric-current-cpu')).toHaveTextContent('—')
    expect(screen.getByTestId('line-comparison:cpu_total')).toBeInTheDocument()
    expect(screen.getByTestId('line-comparison:cpu_app')).toBeInTheDocument()
    expect(screen.getByTestId('line-cpu_app')).toHaveAttribute('data-connect-nulls', 'false')
    expect(screen.getByTestId('area-cpu_total')).toHaveAttribute('data-connect-nulls', 'false')
    expect(screen.getByTestId('line-comparison:cpu_total')).toHaveAttribute('data-connect-nulls', 'false')
    expect(screen.getByTestId('line-comparison:cpu_app')).toHaveAttribute('data-connect-nulls', 'false')
  })

  it('shows the portaled formula help on keyboard focus and hides it on blur', () => {
    renderCpuCard()
    const button = screen.getByRole('button', { name: /CPU formula help/ })

    fireEvent.focus(button)
    expect(screen.getByRole('tooltip')).toHaveTextContent('CPU formula help')

    fireEvent.blur(button)
    expect(screen.queryByRole('tooltip')).toBeNull()
  })
})
