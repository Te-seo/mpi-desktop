import { describe, expect, it, vi, beforeEach } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { renderWithLocale } from '../test/utils'
import { makeAnalysis, makeListItem } from '../test/fixtures'
import { formatSessionRangeTime, parseSessionRangeTime } from '../lib/sessionRangeTime'
import { LocaleProvider } from '../lib/i18n'
import type { ImportResponse, ListSessionsResponse } from '../lib/types'

// Isolate the dashboard's selection/library logic from the sidecar bridge and
// from recharts (which needs real layout dimensions jsdom doesn't provide).
vi.mock('../lib/ipc', () => ({
  listSavedSessions: vi.fn(),
  deleteSavedSessions: vi.fn(),
  loadSessionAnalyses: vi.fn(),
  importSessionFiles: vi.fn(),
  openFiles: vi.fn(),
  getDroppedFilePaths: vi.fn(() => []),
  onEvent: vi.fn(),
}))
vi.mock('../components/dashboard/DashboardCharts', () => ({
  DashboardCharts: () => null,
}))

import * as ipc from '../lib/ipc'
import { AnalysisDashboard } from './AnalysisDashboard'

const mockedList = vi.mocked(ipc.listSavedSessions)
const mockedDelete = vi.mocked(ipc.deleteSavedSessions)
const mockedLoad = vi.mocked(ipc.loadSessionAnalyses)

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(ipc.onEvent).mockReturnValue(vi.fn())
})

describe('session range time', () => {
  it.each([
    [0, '0:00'],
    [59, '0:59'],
    [60, '1:00'],
    [3600, '60:00'],
    [3661.25, '61:01.25'],
    [90.123456, '1:30.123456'],
  ])('formats %s seconds as %s', (seconds, expected) => {
    expect(formatSessionRangeTime(seconds)).toBe(expected)
  })

  it.each([
    ['0:00', 0],
    ['0:59', 59],
    ['1:00', 60],
    ['60:00', 3600],
    ['1:30.25', 90.25],
    [' 1:02.5 ', 62.5],
  ])('parses %s as %s seconds', (value, expected) => {
    expect(parseSessionRangeTime(value)).toBe(expected)
  })

  it.each(['', '59', '0:5', '0:60', '-1:00', '1:-01', '1:00:00', '1.5:00', 'not-a-time'])('rejects malformed time %s', (value) => {
    expect(parseSessionRangeTime(value)).toBeNull()
  })
})

describe('AnalysisDashboard', () => {
  it('shows the empty state when the library has no sessions', async () => {
    mockedList.mockResolvedValue({ directory: '/raw', sessions: [] })
    renderWithLocale(<AnalysisDashboard active />)
    await waitFor(() => expect(screen.getByText('No saved sessions')).toBeInTheDocument())
  })

  it('analyzes a session when it is selected', async () => {
    mockedList.mockResolvedValue({
      directory: '/raw',
      sessions: [makeListItem({ file_path: '/raw/a.csv', app_label: 'Game A' })],
    })
    mockedLoad.mockResolvedValue({ analyses: [makeAnalysis({ app_label: 'Game A' })], errors: [], dropped: 0 })
    renderWithLocale(<AnalysisDashboard active />)
    const row = await screen.findByText('Game A')
    fireEvent.click(row)
    await waitFor(() => expect(mockedLoad).toHaveBeenCalledWith(['/raw/a.csv'], []))
  })

  it('selects one session by default and preserves the first comparison when the mode is turned off', async () => {
    mockedList.mockResolvedValue({ directory: '/raw', sessions: [
      makeListItem({ file_path: '/raw/a.csv', app_label: 'Game A' }),
      makeListItem({ file_path: '/raw/b.csv', app_label: 'Game B' }),
    ] })
    mockedLoad.mockResolvedValue({ analyses: [], errors: [], dropped: 0 })
    renderWithLocale(<AnalysisDashboard active />)
    const comparison = screen.getByRole('switch', { name: 'Comparison mode' })
    expect(comparison).toHaveAttribute('aria-checked', 'false')
    fireEvent.click(await screen.findByText('Game A'))
    fireEvent.click(screen.getByText('Game B'))
    await waitFor(() => expect(mockedLoad).toHaveBeenLastCalledWith(['/raw/b.csv'], []))
    fireEvent.click(comparison)
    fireEvent.click(screen.getByText('Game A'))
    await waitFor(() => expect(mockedLoad).toHaveBeenLastCalledWith(['/raw/b.csv', '/raw/a.csv'], []))
    fireEvent.click(comparison)
    await waitFor(() => expect(mockedLoad).toHaveBeenLastCalledWith(['/raw/b.csv'], []))
    expect(comparison).toHaveAttribute('aria-checked', 'false')
    fireEvent.click(within(document.querySelector('aside')!).getByText('Game B'))
    expect(screen.getByText('Select a session to see its analysis')).toBeInTheDocument()
  })

  it('refreshes on each visit and backend changes after the first visit, then unsubscribes', async () => {
    mockedList.mockResolvedValue({ directory: '/raw', sessions: [] })
    const unsubscribe = vi.fn()
    vi.mocked(ipc.onEvent).mockReturnValue(unsubscribe)
    const view = renderWithLocale(<AnalysisDashboard active={false} />)
    const listener = vi.mocked(ipc.onEvent).mock.calls[0][0]
    const changed = { id: null, ok: true, event: 'session_library_changed' }
    await act(async () => listener(changed))
    expect(mockedList).not.toHaveBeenCalled()
    view.rerender(<LocaleProvider><AnalysisDashboard active /></LocaleProvider>)
    await waitFor(() => expect(mockedList).toHaveBeenCalledTimes(1))
    view.rerender(<LocaleProvider><AnalysisDashboard active={false} /></LocaleProvider>)
    await act(async () => listener({ id: null, ok: true, event: 'capture_snapshot' }))
    expect(mockedList).toHaveBeenCalledTimes(1)
    await act(async () => listener(changed))
    expect(mockedList).toHaveBeenCalledTimes(2)
    view.rerender(<LocaleProvider><AnalysisDashboard active /></LocaleProvider>)
    await waitFor(() => expect(mockedList).toHaveBeenCalledTimes(3))
    view.unmount()
    expect(unsubscribe).toHaveBeenCalledTimes(1)
  })

  it('preserves analysis and range drafts when a library event leaves the selection unchanged', async () => {
    const session = makeListItem({ file_path: '/raw/a.csv', app_label: 'Game A', duration_sec: 300 })
    mockedList.mockResolvedValue({ directory: '/raw', sessions: [session] })
    mockedLoad.mockResolvedValue({ analyses: [makeAnalysis({ file_path: session.file_path, app_label: 'Game A' })], errors: [], dropped: 0 })
    renderWithLocale(<AnalysisDashboard active />)
    fireEvent.click(await screen.findByText('Game A'))
    await screen.findByTestId('range-editor-A')
    fireEvent.change(screen.getByLabelText('A Start (m:ss)'), { target: { value: '0:12' } })
    fireEvent.change(screen.getByLabelText('A End (m:ss)'), { target: { value: '1:30' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))
    await waitFor(() => expect(mockedLoad).toHaveBeenLastCalledWith(['/raw/a.csv'], [{ path: '/raw/a.csv', start_sec: 12, end_sec: 90 }]))
    await screen.findByTestId('range-editor-A')
    fireEvent.change(screen.getByLabelText('A Start (m:ss)'), { target: { value: '0:13.25' } })
    const loads = mockedLoad.mock.calls.length
    const listener = vi.mocked(ipc.onEvent).mock.calls[0][0]
    mockedList.mockResolvedValue({ directory: '/raw', sessions: [session, makeListItem({ file_path: '/raw/b.csv', app_label: 'New capture' })] })
    await act(async () => listener({ id: null, ok: true, event: 'session_library_changed' }))
    expect(await screen.findByText('New capture')).toBeInTheDocument()
    expect(mockedLoad).toHaveBeenCalledTimes(loads)
    expect(screen.getByLabelText('A Start (m:ss)')).toHaveValue('0:13.25')
  })

  it('ignores an older refresh after a newer library event removes a session', async () => {
    const session = makeListItem({ file_path: '/raw/a.csv', app_label: 'Game A' })
    mockedList.mockResolvedValue({ directory: '/raw', sessions: [session] })
    mockedLoad.mockResolvedValue({ analyses: [makeAnalysis({ file_path: session.file_path, app_label: 'Game A' })], errors: [], dropped: 0 })
    renderWithLocale(<AnalysisDashboard active />)
    fireEvent.click(await screen.findByText('Game A'))
    await screen.findByTestId('range-editor-A')
    let resolveOlder!: (value: ListSessionsResponse) => void
    mockedList.mockImplementationOnce(() => new Promise((resolve) => { resolveOlder = resolve }))
      .mockResolvedValueOnce({ directory: '/raw', sessions: [] })
    const listener = vi.mocked(ipc.onEvent).mock.calls[0][0]
    const changed = { id: null, ok: true, event: 'session_library_changed' }
    await act(async () => { listener(changed); listener(changed) })
    expect(screen.queryByText('Game A')).not.toBeInTheDocument()
    await act(async () => resolveOlder({ directory: '/raw', sessions: [session] }))
    expect(screen.queryByText('Game A')).not.toBeInTheDocument()
    expect(screen.getByText('Select a session to see its analysis')).toBeInTheDocument()
  })

  it('prevents overlapping imports and clears a stale search after a successful import', async () => {
    mockedList.mockResolvedValue({ directory: '/raw', sessions: [makeListItem({ app_label: 'Existing session' })] })
    let resolveImport!: (value: ImportResponse) => void
    vi.mocked(ipc.importSessionFiles).mockImplementationOnce(() => new Promise((resolve) => { resolveImport = resolve }))
    vi.mocked(ipc.getDroppedFilePaths).mockReturnValue(['/incoming/capture.csv'])
    renderWithLocale(<AnalysisDashboard active />)
    await screen.findByText('Existing session')
    const search = screen.getByRole('textbox')
    fireEvent.change(search, { target: { value: 'no match' } })
    const library = document.querySelector('aside')!
    fireEvent.drop(library, { dataTransfer: { files: [] } })
    fireEvent.drop(library, { dataTransfer: { files: [] } })
    expect(ipc.importSessionFiles).toHaveBeenCalledTimes(1)
    mockedList.mockResolvedValue({ directory: '/raw', sessions: [makeListItem({ app_label: 'Imported session' })] })
    await act(async () => resolveImport({ directory: '/raw', imported: 1, duplicates: 0, failed: 0, results: [] }))
    expect(search).toHaveValue('')
    expect(await screen.findByText('Imported session')).toBeInTheDocument()
    // The in-flight lock is released after the refresh, so a later drop works.
    vi.mocked(ipc.importSessionFiles).mockResolvedValue({ directory: '/raw', imported: 0, duplicates: 1, failed: 0, results: [] })
    fireEvent.change(search, { target: { value: 'Imported' } })
    await act(async () => fireEvent.drop(library, { dataTransfer: { files: [] } }))
    expect(ipc.importSessionFiles).toHaveBeenCalledTimes(2)
    expect(search).toHaveValue('Imported')
  })

  it('uses the app title instead of the bundle id and renders its icon', async () => {
    mockedList.mockResolvedValue({ directory: '/raw', sessions: [makeListItem({ app_label: 'Title App', bundle_id: 'com.hidden.id', app_icon: 'data:image/png;base64,AA==' })] })
    renderWithLocale(<AnalysisDashboard active />)
    expect(await screen.findByText('Title App')).toBeInTheDocument()
    expect(screen.queryByText('com.hidden.id')).not.toBeInTheDocument()
    expect(screen.queryByText('Edit session range')).not.toBeInTheDocument()
    expect(document.querySelector('img[src="data:image/png;base64,AA=="]')).toBeTruthy()
  })

  it('shows a missing title as an explicit package ID instead of Unknown app', async () => {
    mockedList.mockResolvedValue({ directory: '/raw', sessions: [makeListItem({ app_label: null, bundle_id: 'com.example.game' })] })
    mockedLoad.mockResolvedValue({ analyses: [makeAnalysis({ app_label: null, bundle_id: 'com.example.game' })], errors: [], dropped: 0 })
    renderWithLocale(<AnalysisDashboard active />)
    fireEvent.click(await screen.findByText('com.example.game'))
    await waitFor(() => expect(screen.getAllByText('com.example.game').length).toBeGreaterThanOrEqual(2))
    expect(screen.getAllByText('Package ID')).toHaveLength(2)
    expect(screen.queryByText('Unknown app')).not.toBeInTheDocument()
  })

  it('confirms deletion and clears selection plus path-keyed range state', async () => {
    const session = makeListItem({ file_path: '/raw/a.csv', app_label: 'Game A', duration_sec: 300 })
    mockedList.mockResolvedValue({ directory: '/raw', sessions: [session] })
      .mockResolvedValueOnce({ directory: '/raw', sessions: [session] })
      .mockResolvedValueOnce({ directory: '/raw', sessions: [] })
    mockedLoad.mockResolvedValue({ analyses: [makeAnalysis({ file_path: '/raw/a.csv', app_label: 'Game A' })], errors: [], dropped: 0 })
    mockedDelete.mockResolvedValue({
      directory: '/raw', deleted: 1, failed: 0,
      results: [{ path: '/raw/a.csv', file_name: 'a.csv', status: 'deleted' }],
    })
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderWithLocale(<AnalysisDashboard active />)
    fireEvent.click(await screen.findByText('Game A'))
    await screen.findByTestId('range-editor-A')
    fireEvent.change(screen.getByLabelText('A Start (m:ss)'), { target: { value: '0:12' } })
    fireEvent.change(screen.getByLabelText('A End (m:ss)'), { target: { value: '1:30' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))
    await waitFor(() => expect(mockedLoad).toHaveBeenLastCalledWith(['/raw/a.csv'], [{ path: '/raw/a.csv', start_sec: 12, end_sec: 90 }]))

    fireEvent.click(screen.getByRole('button', { name: 'Delete session: Game A' }))
    await waitFor(() => expect(mockedDelete).toHaveBeenCalledWith(['/raw/a.csv']))
    await waitFor(() => expect(screen.queryByText('Game A')).not.toBeInTheDocument())
    expect(screen.getByText('Select a session to see its analysis')).toBeInTheDocument()

    // Adversarial stale-state check: if an external process recreates the same
    // path and Refresh finds it, selecting it must not reuse the deleted range.
    fireEvent.click(screen.getByTitle('Refresh'))
    fireEvent.click(await screen.findByText('Game A'))
    await waitFor(() => expect(mockedLoad).toHaveBeenLastCalledWith(['/raw/a.csv'], []))
    confirm.mockRestore()
  })

  it('keeps the row and surfaces a per-file deletion failure', async () => {
    mockedList.mockResolvedValue({ directory: '/raw', sessions: [makeListItem({ file_path: '/raw/a.csv', app_label: 'Game A' })] })
    mockedDelete.mockResolvedValue({
      directory: '/raw', deleted: 0, failed: 1,
      results: [{ path: '/raw/a.csv', file_name: 'a.csv', status: 'failed', reason: 'file is locked' }],
    })
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderWithLocale(<AnalysisDashboard active />)
    await screen.findByText('Game A')
    fireEvent.click(screen.getByRole('button', { name: 'Delete session: Game A' }))
    expect(await screen.findByText('file is locked')).toBeInTheDocument()
    expect(screen.getByText('Game A')).toBeInTheDocument()
    confirm.mockRestore()
  })

  it('reloads the selected analysis with an applied session range', async () => {
    mockedList.mockResolvedValue({ directory: '/raw', sessions: [makeListItem({ file_path: '/raw/a.csv', app_label: 'Game A', duration_sec: 300 })] })
    mockedLoad.mockResolvedValue({ analyses: [makeAnalysis()], errors: [], dropped: 0 })
    renderWithLocale(<AnalysisDashboard active />)
    fireEvent.click(await screen.findByText('Game A'))
    await screen.findByTestId('range-editor-A')
    expect(screen.getByLabelText('A Start (m:ss)')).toHaveValue('0:00')
    expect(screen.getByLabelText('A End (m:ss)')).toHaveValue('5:00')
    fireEvent.change(screen.getByLabelText('A Start (m:ss)'), { target: { value: '0:12' } })
    fireEvent.change(screen.getByLabelText('A End (m:ss)'), { target: { value: '1:30' } })
    fireEvent.click(screen.getByText('Apply'))
    await waitFor(() => expect(mockedLoad).toHaveBeenLastCalledWith(['/raw/a.csv'], [{ path: '/raw/a.csv', start_sec: 12, end_sec: 90 }]))
  })

  it('rejects malformed, reversed, equal, and out-of-session ranges before applying fractional seconds', async () => {
    mockedList.mockResolvedValue({ directory: '/raw', sessions: [makeListItem({ file_path: '/raw/a.csv', app_label: 'Game A', duration_sec: 90.5 })] })
    mockedLoad.mockResolvedValue({ analyses: [makeAnalysis()], errors: [], dropped: 0 })
    renderWithLocale(<AnalysisDashboard active />)
    fireEvent.click(await screen.findByText('Game A'))
    const editor = await screen.findByTestId('range-editor-A')
    const startInput = within(editor).getByLabelText('A Start (m:ss)')
    const endInput = within(editor).getByLabelText('A End (m:ss)')
    const applyButton = within(editor).getByRole('button', { name: 'Apply' })
    const callsBeforeInvalidEdits = mockedLoad.mock.calls.length

    for (const [start, end] of [
      ['not-a-time', '1:00'],
      ['1:00', '1:00'],
      ['1:01', '1:00'],
      ['0:00', '1:30.501'],
      ['0:60', '1:00'],
    ]) {
      fireEvent.change(startInput, { target: { value: start } })
      fireEvent.change(endInput, { target: { value: end } })
      expect(applyButton).toBeDisabled()
      fireEvent.click(applyButton)
    }
    expect(mockedLoad).toHaveBeenCalledTimes(callsBeforeInvalidEdits)

    fireEvent.change(startInput, { target: { value: '0:00.5' } })
    fireEvent.change(endInput, { target: { value: '1:30.5' } })
    expect(applyButton).toBeEnabled()
    fireEvent.click(applyButton)
    await waitFor(() => expect(mockedLoad).toHaveBeenLastCalledWith(['/raw/a.csv'], [{ path: '/raw/a.csv', start_sec: 0.5, end_sec: 90.5 }]))
  })

  it('applies the B editor range to the second selected session', async () => {
    mockedList.mockResolvedValue({ directory: '/raw', sessions: [
      makeListItem({ session_id: 'a', file_path: '/raw/a.csv', app_label: 'Game A', duration_sec: 300 }),
      makeListItem({ session_id: 'b', file_path: '/raw/b.csv', app_label: 'Game B', duration_sec: 240 }),
    ] })
    mockedLoad.mockImplementation(async (paths) => ({
      analyses: paths.map((path, index) => makeAnalysis({ session_id: index ? 'b' : 'a', file_path: path, app_label: index ? 'Game B' : 'Game A' })),
      errors: [], dropped: 0,
    }))
    renderWithLocale(<AnalysisDashboard active />)
    fireEvent.click(screen.getByRole('switch', { name: 'Comparison mode' }))
    fireEvent.click(await screen.findByText('Game A'))
    fireEvent.click(await screen.findByText('Game B'))
    await screen.findByTestId('range-editor-B')
    fireEvent.change(screen.getByLabelText('B Start (m:ss)'), { target: { value: '0:18' } })
    fireEvent.change(screen.getByLabelText('B End (m:ss)'), { target: { value: '2:00' } })
    fireEvent.click(screen.getByTestId('range-editor-B').querySelector('button:last-child')!)
    await waitFor(() => expect(mockedLoad).toHaveBeenLastCalledWith(
      ['/raw/a.csv', '/raw/b.csv'],
      [{ path: '/raw/b.csv', start_sec: 18, end_sec: 120 }]
    ))
  })

  it('warns (without blocking) when some selected sessions fail to load', async () => {
    mockedList.mockResolvedValue({
      directory: '/raw',
      sessions: [
        makeListItem({ session_id: 's1', file_path: '/raw/a.csv', app_label: 'AA' }),
        makeListItem({ session_id: 's2', file_path: '/raw/b.csv', app_label: 'BB' }),
      ],
    })
    mockedLoad.mockResolvedValue({
      analyses: [makeAnalysis({ file_path: '/raw/a.csv', app_label: 'AA' })],
      errors: [{ path: '/raw/b.csv', error: 'corrupt' }],
      dropped: 0,
    })
    renderWithLocale(<AnalysisDashboard active />)
    fireEvent.click(screen.getByRole('switch', { name: 'Comparison mode' }))
    fireEvent.click(await screen.findByText('AA'))
    fireEvent.click(await screen.findByText('BB'))
    await waitFor(() => expect(screen.getByText(/could not be loaded/i)).toBeInTheDocument())
  })

  it('caps selection at 4 and warns on the 5th', async () => {
    const sessions = Array.from({ length: 5 }, (_, i) =>
      makeListItem({ session_id: `s${i}`, file_path: `/raw/${i}.csv`, app_label: `App ${i}` })
    )
    mockedList.mockResolvedValue({ directory: '/raw', sessions })
    mockedLoad.mockResolvedValue({ analyses: [makeAnalysis()], errors: [], dropped: 0 })
    renderWithLocale(<AnalysisDashboard active />)
    fireEvent.click(screen.getByRole('switch', { name: 'Comparison mode' }))
    await screen.findByText('App 0')
    for (let i = 0; i < 5; i++) fireEvent.click(screen.getByText(`App ${i}`))
    await waitFor(() => expect(screen.getByText('You can select up to 4 sessions')).toBeInTheDocument())
    // loadSessionAnalyses is never called with more than 4 paths.
    for (const call of mockedLoad.mock.calls) {
      expect((call[0] as string[]).length).toBeLessThanOrEqual(4)
    }
  })
})
