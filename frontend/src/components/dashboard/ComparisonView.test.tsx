import { describe, expect, it } from 'vitest'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { LocaleProvider } from '../../lib/i18n'
import { renderWithLocale } from '../../test/utils'
import { makeAnalysis, makeStats } from '../../test/fixtures'
import { ComparisonView } from './ComparisonView'
import type { SessionAnalysis } from '../../lib/types'
import { MEMORY_SOURCE } from '../../lib/chartMemory'

function renderCmp(analyses: SessionAnalysis[], selectedPaths?: string[]) {
  try {
    localStorage.setItem('mpi.locale.v1', 'en')
  } catch {
    /* default locale is already English in jsdom */
  }
  const wrap = (a: SessionAnalysis[], sp?: string[]) => (
    <LocaleProvider>
      <ComparisonView analyses={a} selectedPaths={sp} />
    </LocaleProvider>
  )
  const utils = render(wrap(analyses, selectedPaths))
  return { ...utils, rerenderCmp: (a: SessionAnalysis[], sp?: string[]) => utils.rerender(wrap(a, sp)) }
}

function pair() {
  const base = makeAnalysis()
  const a = makeAnalysis({ session_id: 'A', file_path: '/A', app_label: 'App A' })
  const b = makeAnalysis({
    session_id: 'B',
    file_path: '/B',
    app_label: 'App B',
    metrics: { ...base.metrics, fps: makeStats({ average: 45 }) },
  })
  return [a, b]
}

describe('ComparisonView', () => {
  it('compares only peaks measured by the same memory source', () => {
    const typed = makeAnalysis({ file_path: '/A', memory_source: MEMORY_SOURCE, metrics: { anon_rss_plus_swap_mib: makeStats({ max: 100 }) } })
    const other = { ...typed, file_path: '/B', metrics: { anon_rss_plus_swap_mib: makeStats({ max: 150 }) } }
    const legacy = makeAnalysis({ file_path: '/C', peak_app_memory_mb: 500 })
    const ios = makeAnalysis({ file_path: '/D', platform: 'ios', peak_app_memory_mb: 700 })
    renderWithLocale(<ComparisonView analyses={[typed, other, legacy, ios]} />)
    const typedRow = within(screen.getByRole('row', { name: /Peak Anonymous RSS \+ Swap/ }))
    expect(typedRow.getByText('MiB')).toBeInTheDocument()
    expect(typedRow.getByText('+50 (+50%)')).toBeInTheDocument()
    expect(typedRow.queryByText('500')).not.toBeInTheDocument()
    for (const [label, value, unit] of [['Legacy PSS / RSS', '500', 'MB'], ['iOS app memory', '700', 'MiB']]) {
      const row = within(screen.getByText(`Peak ${label}`).closest('tr')!)
      expect(row.getByText(value)).toBeInTheDocument()
      expect(row.getByText(unit)).toBeInTheDocument()
      expect(row.queryByText(/\+/)).not.toBeInTheDocument()
    }
  })
  it('renders a column per session with A as the default baseline', () => {
    renderWithLocale(<ComparisonView analyses={pair()} />)
    expect(screen.getByText('App A')).toBeInTheDocument()
    expect(screen.getByText('App B')).toBeInTheDocument()
    // A is baseline; B offers promotion.
    expect(screen.getByText('Baseline')).toBeInTheDocument()
    expect(screen.getByText('Set as baseline')).toBeInTheDocument()
  })

  it('promotes another session to baseline on click', () => {
    renderWithLocale(<ComparisonView analyses={pair()} />)
    fireEvent.click(screen.getByText('Set as baseline'))
    // B is baseline now; A offers promotion — both labels still present, but the
    // baseline moved (there is exactly one "Baseline" button).
    expect(screen.getAllByText('Baseline')).toHaveLength(1)
    expect(screen.getAllByText('Set as baseline')).toHaveLength(1)
  })

  it('omits the percent delta when the baseline value is 0', () => {
    const base = makeAnalysis()
    const a = makeAnalysis({ file_path: '/A', metrics: { ...base.metrics, power_mw: makeStats({ average: 0 }) } })
    const b = makeAnalysis({ file_path: '/B', metrics: { ...base.metrics, power_mw: makeStats({ average: 100 }) } })
    renderWithLocale(<ComparisonView analyses={[a, b]} />)
    // Absolute delta "+100" shown, with no "%" in that cell.
    const delta = screen.getByText('+100')
    expect(delta).toBeInTheDocument()
    expect(delta.textContent).not.toContain('%')
  })

  it('renders unavailable normalized CPU values and their delta as dashes', () => {
    const base = makeAnalysis()
    const unavailable = makeStats({
      valid_count: 0,
      total_count: 2,
      coverage_percent: 0,
      average: null,
      median: null,
      min: null,
      max: null,
      p05: null,
      p95: null,
      p99: null,
      stddev: null,
    })
    const a = makeAnalysis({
      file_path: '/A',
      metrics: { ...base.metrics, cpu_app: unavailable, cpu_norm: unavailable },
    })
    const b = makeAnalysis({
      file_path: '/B',
      metrics: { ...base.metrics, cpu_app: unavailable, cpu_norm: unavailable },
    })

    renderWithLocale(<ComparisonView analyses={[a, b]} />)

    for (const label of ['App CPU (whole device)', 'App CPU (normalized)']) {
      const row = screen.getByText(label).closest('tr')
      expect(row).not.toBeNull()
      // Baseline value, comparison value, and comparison delta are all N/A.
      expect(within(row!).getAllByText('\u2014')).toHaveLength(3)
    }
  })

  it('warns when the comparison conditions differ', () => {
    const a = makeAnalysis({ file_path: '/A' })
    const b = makeAnalysis({ file_path: '/B', platform: 'ios' })
    renderWithLocale(<ComparisonView analyses={[a, b]} />)
    expect(screen.getByText(/Comparison conditions differ/i)).toBeInTheDocument()
  })

  it('explains how normalized app CPU is derived', () => {
    renderWithLocale(<ComparisonView analyses={pair()} />)
    expect(screen.getByText(/otherwise the normalized value is unavailable/i)).toBeInTheDocument()
  })

  it('keeps each session A–D identity by selection slot when one failed to load', () => {
    // Selected A,B,C,D but B failed → analyses compacted to [A,C,D]. Columns must
    // still read A, C, D (by selection slot), never mislabel C as B.
    const a = makeAnalysis({ file_path: '/A', app_label: 'AA' })
    const c = makeAnalysis({ file_path: '/C', app_label: 'CC' })
    const dd = makeAnalysis({ file_path: '/D', app_label: 'DD' })
    renderWithLocale(
      <ComparisonView analyses={[a, c, dd]} selectedPaths={['/A', '/B', '/C', '/D']} />
    )
    expect(screen.getByText('D')).toBeInTheDocument()
    expect(screen.queryByText('B')).toBeNull()
  })

  it('clamps a stale baseline when the selection shrinks', () => {
    const four = [0, 1, 2, 3].map((i) => makeAnalysis({ file_path: `/${i}`, app_label: `S${i}` }))
    const { rerenderCmp } = renderCmp(four)
    // Promote the last column (index 3) to baseline.
    const promote = screen.getAllByText('Set as baseline')
    fireEvent.click(promote[promote.length - 1])
    // Shrink to 2 sessions; baselineIdx=3 is now out of range but must clamp so
    // exactly one column remains marked as baseline.
    rerenderCmp(four.slice(0, 2))
    expect(screen.getAllByText('Baseline')).toHaveLength(1)
  })
})
