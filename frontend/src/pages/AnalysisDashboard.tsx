import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertTriangle,
  ChevronDown,
  Inbox,
  Loader2,
  RefreshCw,
  Search,
  Trash2,
  Upload,
  X,
} from 'lucide-react'
import { useMessages } from '../lib/i18n'
import {
  getDroppedFilePaths,
  deleteSavedSessions,
  importSessionFiles,
  listSavedSessions,
  loadSessionAnalyses,
  onEvent,
  openFiles,
} from '../lib/ipc'
import type { ImportResponse, SessionAnalysis, SessionAnalysisRange, SessionListItem } from '../lib/types'
import { SessionAnalysisView } from '../components/dashboard/SessionAnalysisView'
import { RenderPipelinePanel } from '../components/RenderPipelinePanel'
import { SessionCutsEditor } from '../components/dashboard/SessionCutsEditor'
import { ComparisonView } from '../components/dashboard/ComparisonView'
import { DashboardCharts } from '../components/dashboard/DashboardCharts'
import { fmtDate } from '../components/dashboard/statFormat'
import { SESSION_COLORS, SESSION_LABELS } from '../components/dashboard/sessionColors'
import { formatSessionRangeTime, parseSessionRangeTime } from '../lib/sessionRangeTime'

const MAX_SELECTED = 4

export function AnalysisDashboard({ active }: { active: boolean }) {
  const t = useMessages()
  const d = t.dashboard

  const [sessions, setSessions] = useState<SessionListItem[]>([])
  const [loadingList, setLoadingList] = useState(false)
  const [listError, setListError] = useState<string | null>(null)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [comparisonMode, setComparisonMode] = useState(false)
  const [selected, setSelected] = useState<string[]>([]) // file paths, order = baseline order
  const [ranges, setRanges] = useState<Record<string, { start: number; end: number }>>({})
  const [rangeDrafts, setRangeDrafts] = useState<Record<string, { start: string; end: string }>>({})
  const [analyses, setAnalyses] = useState<SessionAnalysis[]>([])
  const [analyzing, setAnalyzing] = useState(false)
  const [analysisError, setAnalysisError] = useState<string | null>(null)
  const [loadErrors, setLoadErrors] = useState<{ path: string; error: string }[]>([])
  const [importing, setImporting] = useState(false)
  const [deletingPath, setDeletingPath] = useState<string | null>(null)
  const [importSummary, setImportSummary] = useState<ImportResponse | null>(null)
  // Nonce that increments each time the 4-session limit is hit, so re-hitting the
  // limit restarts the auto-dismiss timer instead of being swallowed by React's
  // no-op bail-out on setState(true) when already true.
  const [maxNoticeAt, setMaxNoticeAt] = useState(0)
  const [rangeStripOpen, setRangeStripOpen] = useState(true)
  const [dragOver, setDragOver] = useState(false)
  const loadedOnce = useRef(false)
  const importInFlight = useRef(false)
  const deleteInFlight = useRef(false)
  const refreshRequest = useRef(0)

  const refresh = useCallback(async () => {
    const request = ++refreshRequest.current
    setLoadingList(true)
    setListError(null)
    try {
      const res = await listSavedSessions()
      if (request !== refreshRequest.current) return
      const livePaths = new Set(res.sessions.map((session) => session.file_path))
      setSessions(res.sessions)
      setSelected((prev) => prev.every((path) => livePaths.has(path)) ? prev : prev.filter((path) => livePaths.has(path)))
      setRanges((prev) => Object.keys(prev).every((path) => livePaths.has(path)) ? prev : Object.fromEntries(Object.entries(prev).filter(([path]) => livePaths.has(path))))
      setRangeDrafts((prev) => Object.keys(prev).every((path) => livePaths.has(path)) ? prev : Object.fromEntries(Object.entries(prev).filter(([path]) => livePaths.has(path))))
      setAnalyses((prev) => prev.every((analysis) => livePaths.has(analysis.file_path)) ? prev : prev.filter((analysis) => livePaths.has(analysis.file_path)))
      setLoadErrors((prev) => prev.every((error) => livePaths.has(error.path)) ? prev : prev.filter((error) => livePaths.has(error.path)))
    } catch (e) {
      if (request === refreshRequest.current) setListError(e instanceof Error ? e.message : String(e))
    } finally {
      if (request === refreshRequest.current) setLoadingList(false)
    }
  }, [])

  // Load on first visit and refresh whenever the dashboard becomes active again.
  useEffect(() => {
    if (active) {
      loadedOnce.current = true
      // The activation effect starts an external library fetch and its loading state.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      void refresh()
    }
  }, [active, refresh])

  // Keep a visited library current when another capture window saves a session.
  useEffect(() => onEvent((message) => {
    if (message.event === 'session_library_changed' && loadedOnce.current) void refresh()
  }), [refresh])

  // Analyze the current selection (baseline first). When nothing is selected the
  // render falls through to the empty state, so there's no need to clear state
  // synchronously here (which React discourages inside effects).
  useEffect(() => {
    if (selected.length === 0) return
    let cancelled = false
    // Show the loading state while the async analysis fetch is in flight, and
    // clear the previous result so a changed selection never shows the old
    // session's data under the new one while the (possibly slow) fetch runs.
    // This is the canonical data-fetching effect; the rule over-flags the flags.
    /* eslint-disable react-hooks/set-state-in-effect */
    setAnalyzing(true)
    setAnalysisError(null)
    setAnalyses([])
    setLoadErrors([])
    /* eslint-enable react-hooks/set-state-in-effect */
    const appliedRanges: SessionAnalysisRange[] = selected.flatMap((path) => {
      const range = ranges[path]
      return range ? [{ path, start_sec: range.start, end_sec: range.end }] : []
    })
    loadSessionAnalyses(selected, appliedRanges)
      .then((res) => {
        if (cancelled) return
        setAnalyses(res.analyses)
        setLoadErrors(res.errors)
        // Full failure -> error state; partial failure keeps the analyzed
        // sessions and surfaces a warning (see the toolbar chip).
        setAnalysisError(res.errors.length > 0 && res.analyses.length === 0 ? res.errors[0].error : null)
      })
      .catch((e) => {
        if (!cancelled) setAnalysisError(e instanceof Error ? e.message : String(e))
      })
      .finally(() => {
        if (!cancelled) setAnalyzing(false)
      })
    return () => {
      cancelled = true
    }
  }, [selected, ranges])

  // Auto-dismiss the "max 4" hint. Keyed on the nonce so every re-trigger
  // restarts the 2.5s timer.
  useEffect(() => {
    if (maxNoticeAt === 0) return
    const id = setTimeout(() => setMaxNoticeAt(0), 2500)
    return () => clearTimeout(id)
  }, [maxNoticeAt])

  const toggle = useCallback((path: string) => {
    setSelected((prev) => {
      if (prev.includes(path)) return prev.filter((p) => p !== path)
      if (!comparisonMode) return [path]
      if (prev.length >= MAX_SELECTED) {
        setMaxNoticeAt((n) => n + 1)
        return prev
      }
      return [...prev, path]
    })
  }, [comparisonMode])

  const toggleComparisonMode = () => {
    setComparisonMode(!comparisonMode)
    setMaxNoticeAt(0)
    if (comparisonMode) setSelected((prev) => prev.slice(0, 1))
  }

  const removeDeletedPath = useCallback((path: string) => {
    setSessions((prev) => prev.filter((session) => session.file_path !== path))
    setSelected((prev) => prev.filter((selectedPath) => selectedPath !== path))
    setAnalyses((prev) => prev.filter((analysis) => analysis.file_path !== path))
    setLoadErrors((prev) => prev.filter((error) => error.path !== path))
    setRanges((prev) => {
      const next = { ...prev }
      delete next[path]
      return next
    })
    setRangeDrafts((prev) => {
      const next = { ...prev }
      delete next[path]
      return next
    })
  }, [])

  const deleteSession = useCallback(async (item: SessionListItem) => {
    if (deleteInFlight.current) return
    const label = item.app_label?.trim() || item.bundle_id?.trim() || d.unknownApp
    if (!window.confirm(d.library.deleteConfirm(label))) return
    deleteInFlight.current = true
    setDeletingPath(item.file_path)
    setDeleteError(null)
    try {
      const result = await deleteSavedSessions([item.file_path])
      const outcome = result.results.find((entry) => entry.path === item.file_path) ?? result.results[0]
      if (outcome?.status === 'deleted') {
        removeDeletedPath(item.file_path)
        await refresh()
      } else {
        setDeleteError(outcome?.reason || d.library.deleteFailed)
      }
    } catch (error) {
      setDeleteError(error instanceof Error ? error.message : String(error))
    } finally {
      deleteInFlight.current = false
      setDeletingPath((current) => current === item.file_path ? null : current)
    }
  }, [d, refresh, removeDeletedPath])

  const runImport = useCallback(
    async (paths: string[]) => {
      const csvs = paths.filter((p) => p.toLowerCase().endsWith('.csv'))
      if (csvs.length === 0 || importInFlight.current) return
      importInFlight.current = true
      setImporting(true)
      setImportSummary(null)
      try {
        const res = await importSessionFiles(csvs)
        setImportSummary(res)
        if (res.imported > 0) setSearch('')
        await refresh()
      } catch (e) {
        setListError(e instanceof Error ? e.message : String(e))
      } finally {
        importInFlight.current = false
        setImporting(false)
      }
    },
    [refresh]
  )

  const onImportClick = useCallback(async () => {
    const paths = await openFiles({ multi: true })
    if (paths.length > 0) await runImport(paths)
  }, [runImport])

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    if (!q) return sessions
    return sessions.filter((s) =>
      [s.app_label, s.bundle_id, s.device_model, s.platform, s.session_name, s.started_at]
        .filter(Boolean)
        .some((v) => String(v).toLowerCase().includes(q))
    )
  }, [sessions, search])

  const orderIndex = useCallback((path: string) => selected.indexOf(path), [selected])

  const rangeEditorProps = {
    analyses,
    selected,
    sessions,
    ranges,
    drafts: rangeDrafts,
    labels: d.range,
    unknownAppLabel: d.unknownApp,
    onDraft: (path: string, draft: RangeDraft) => setRangeDrafts((prev) => ({ ...prev, [path]: draft })),
    onApply: (path: string, range: RangeValue) => setRanges((prev) => ({ ...prev, [path]: range })),
    onReset: (path: string) => {
      setRanges((prev) => { const next = { ...prev }; delete next[path]; return next })
      setRangeDrafts((prev) => { const next = { ...prev }; delete next[path]; return next })
    },
  }

  return (
    <div className="flex h-full min-w-0 flex-col overflow-y-auto lg:flex-row lg:overflow-hidden">
      {/* ───────── SESSION LIBRARY ───────── */}
      <aside
        aria-label={d.library.title}
        className="relative flex w-full flex-none flex-col border-b border-line bg-surface lg:w-[320px] lg:border-b-0 lg:border-r"
        onDragOver={(e) => {
          e.preventDefault()
          setDragOver(true)
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault()
          setDragOver(false)
          void runImport(getDroppedFilePaths(e.dataTransfer.files))
        }}
      >
        <div className="flex flex-wrap items-center justify-between gap-3 px-5 pb-4 pt-5">
          <div><h2 className="text-base font-semibold tracking-tight text-ink">{d.library.title}</h2><p className="mt-1 text-xs tabular-nums text-muted">{d.sessionCount(filtered.length, sessions.length)}</p></div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => void refresh()}
              disabled={loadingList || deletingPath !== null}
              title={d.library.refresh}
              aria-label={d.library.refresh}
              className="mpi-icon-button text-muted hover:bg-hover hover:text-ink disabled:opacity-50"
            >
              <RefreshCw size={14} className={loadingList ? 'animate-spin' : ''} />
            </button>
            <button
              type="button"
              onClick={() => void onImportClick()}
              disabled={importing}
              className="mpi-control inline-flex items-center gap-2 border border-line bg-surface-hi px-3 text-xs font-medium text-ink hover:border-accent disabled:opacity-50"
            >
              {importing ? <Loader2 size={13} className="animate-spin" /> : <Upload size={13} />}
              {importing ? d.library.importing : d.library.import}
            </button>
          </div>
        </div>

        <div className="px-5 pb-3">
          <div className="relative">
            <Search size={16} aria-hidden="true" className="pointer-events-none absolute start-3 top-1/2 -translate-y-1/2 text-muted" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder={d.library.searchPlaceholder}
              aria-label={d.library.searchPlaceholder}
              className="mpi-control w-full border border-line bg-bg py-2 pe-3 ps-10 text-sm text-ink placeholder:text-faint focus:border-accent"
            />
          </div>
        </div>

        <div className="px-5 pb-4">
          <button
            type="button"
            role="switch"
            aria-checked={comparisonMode}
            onClick={toggleComparisonMode}
            className="mpi-control flex w-full items-center justify-between gap-3 border border-line bg-bg px-3 py-2 text-sm font-medium text-ink hover:border-accent"
          >
            {d.library.comparisonMode}
            <span aria-hidden="true" className={`flex h-4 w-7 flex-none items-center rounded-full px-0.5 transition-colors ${comparisonMode ? 'bg-accent' : 'bg-muted'}`}>
              <span className={`h-3 w-3 rounded-full bg-bg transition-transform ${comparisonMode ? 'translate-x-3' : ''}`} />
            </span>
          </button>
          <p className="mt-2 text-xs leading-relaxed text-muted">
            {comparisonMode ? d.library.comparisonHint : d.library.singleSelectionHint}
          </p>
        </div>

        <div className="max-h-64 min-h-0 flex-1 overflow-y-auto overscroll-contain px-3 pb-4 lg:max-h-none">
          {listError ? (
            <div className="px-2 py-4 text-[12px] text-rec">{listError}</div>
          ) : sessions.length === 0 && !loadingList ? (
            <EmptyLibrary title={d.library.empty} hint={d.library.emptyHint} cta={d.library.emptyCta} onImport={() => void onImportClick()} />
          ) : filtered.length === 0 ? (
            <div className="px-2 py-6 text-center text-[12px] text-faint">{d.library.noMatch}</div>
          ) : (
            <ul className="flex flex-col gap-2">
              {filtered.map((s) => (
                <SessionRow
                  key={s.file_path}
                  item={s}
                  slot={orderIndex(s.file_path)}
                  legacyLabel={d.library.legacyBadge}
                  unknownAppLabel={d.unknownApp}
                  packageIdLabel={d.library.packageId}
                  range={ranges[s.file_path]}
                  onToggle={() => toggle(s.file_path)}
                  deleting={deletingPath === s.file_path}
                  deleteDisabled={deletingPath !== null}
                  deleteLabel={d.library.delete}
                  onDelete={() => void deleteSession(s)}
                />
              ))}
            </ul>
          )}
        </div>

        {/* Notice toasts overlay the list bottom instead of pushing it around. */}
        {(importSummary || deleteError || maxNoticeAt > 0) && (
          <div className="pointer-events-none absolute inset-x-2 bottom-2 z-20 flex flex-col gap-1.5">
            {maxNoticeAt > 0 && (
              <div className="pointer-events-auto rounded-[9px] border border-warn-line bg-warn-soft px-2.5 py-1.5 text-[11px] text-warn shadow-lg backdrop-blur">
                {d.library.maxReached}
              </div>
            )}
            {importSummary && (
              <div className="pointer-events-auto flex items-start justify-between gap-2 rounded-[9px] border border-line bg-surface/95 px-2.5 py-1.5 text-[11px] text-muted shadow-lg backdrop-blur">
                <span>
                  {d.importResult.summary(
                    importSummary.imported,
                    importSummary.duplicates,
                    importSummary.failed
                  )}
                </span>
                <button type="button" onClick={() => setImportSummary(null)} aria-label={d.importResult.dismiss} className="mpi-icon-button flex-none text-muted hover:text-ink">
                  <X size={12} />
                </button>
              </div>
            )}
            {deleteError && (
              <div className="pointer-events-auto flex items-start justify-between gap-2 rounded-[9px] border border-rec/40 bg-rec-soft px-2.5 py-1.5 text-[11px] text-rec shadow-lg backdrop-blur">
                <span>{deleteError}</span>
                <button type="button" onClick={() => setDeleteError(null)} aria-label={d.importResult.dismiss} className="mpi-icon-button flex-none text-rec hover:text-danger">
                  <X size={12} />
                </button>
              </div>
            )}
          </div>
        )}

        {dragOver && (
          <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center bg-bg/80 backdrop-blur-[2px]">
            <div className="rounded-[12px] border-2 border-dashed border-accent-line bg-accent-soft px-4 py-3 text-[12px] font-medium text-accent">
              {d.states.dropHint}
            </div>
          </div>
        )}
      </aside>

      {/* ───────── MAIN ───────── */}
      <section aria-label={d.nav.dashboard} className="flex min-w-0 flex-none flex-col lg:min-h-0 lg:flex-1 lg:overflow-hidden">
        <div className="flex flex-wrap items-center justify-between gap-3 px-5 pb-4 pt-5 sm:px-6">
          <h1 className="font-display text-xl font-semibold tracking-tight text-ink">{d.nav.dashboard}</h1>
          <span className="rounded-lg border border-line bg-surface px-3 py-1.5 text-xs tabular-nums text-muted">{d.library.selected(selected.length)}</span>
        </div>
        {selected.length > 0 && (
          <div className="mx-4 mb-4 flex-none rounded-[18px] border border-line bg-surface px-4 py-3 sm:mx-6">
            <div className="flex min-h-[28px] flex-wrap items-center gap-2">
              <div className="flex items-center gap-1">
                {selected.map((path, i) => (
                  <span
                    key={path}
                    className="flex h-4 w-4 items-center justify-center rounded-[5px] text-[10px] font-bold text-bg"
                    style={{ background: SESSION_COLORS[i] }}
                  >
                    {SESSION_LABELS[i]}
                  </span>
                ))}
              </div>
              <span className="text-[11px] text-muted">{d.library.selected(selected.length)}</span>
              <div className="ms-auto flex flex-wrap items-center gap-2">
                {loadErrors.length > 0 && analyses.length > 0 && (
                  <span className="inline-flex items-center gap-1.5 rounded-full border border-warn-line bg-warn-soft px-2.5 py-1 text-[11px] text-warn">
                    <AlertTriangle size={12} className="flex-none" />
                    {d.states.someFailed(loadErrors.length)}
                  </span>
                )}
                <button
                  type="button"
                  onClick={() => setSelected([])}
                  className="mpi-control px-3 text-xs text-muted hover:bg-hover hover:text-ink"
                >
                  {d.library.clearSelection}
                </button>
                <button
                  type="button"
                  onClick={() => setRangeStripOpen((open) => !open)}
                  aria-expanded={rangeStripOpen}
                  className="mpi-control inline-flex items-center gap-2 border border-line bg-bg px-3 text-xs font-medium text-ink hover:border-accent"
                >
                  {d.range.edit}
                  <ChevronDown size={12} className={rangeStripOpen ? 'rotate-180 transition-transform' : 'transition-transform'} />
                </button>
              </div>
            </div>
            {/* Collapsed via `hidden` so the strip (and its test-visible inputs)
                stays mounted whenever a selection exists. */}
            <div className={rangeStripOpen ? 'mt-2' : 'hidden'}>
              <SessionRangeEditors {...rangeEditorProps} />
            </div>
          </div>
        )}

        <div className="min-h-0 flex-1 px-4 pb-6 lg:overflow-y-auto sm:px-6">
          {selected.length > 0 && <div className="mb-4 grid gap-2">
            {selected.map(path => {
              const session = sessions.find(item => item.file_path === path)
              return session && <SessionCutsEditor key={path} session={session} range={ranges[path]}
                onSaved={cuts => setSessions(previous => previous.map(item => item.file_path === path ? { ...item, cuts } : item))}
                onAnalyze={cut => {
                  setRanges(previous => ({ ...previous, [path]: { start: cut.start_sec, end: cut.end_sec } }))
                  setRangeDrafts(previous => ({ ...previous, [path]: { start: formatSessionRangeTime(cut.start_sec), end: formatSessionRangeTime(cut.end_sec) } }))
                }} />
            })}
          </div>}
          {selected.length === 0 ? (
            <CenterState icon={<Inbox size={24} />} title={d.states.selectPrompt} hint={d.states.selectHint} />
          ) : analyzing && analyses.length === 0 ? (
            <CenterState icon={<Loader2 size={22} className="animate-spin" />} title={d.states.analyzing} />
          ) : analysisError ? (
            <CenterState icon={<AlertTriangle size={22} />} title={d.states.loadError} hint={analysisError} tone="error" />
          ) : analyses.length === 1 ? (
            analyses[0].sample_count === 0 ? (
              <CenterState icon={<AlertTriangle size={22} />} title={d.states.noData} />
            ) : (
              <div className="flex flex-col gap-4">
                <SessionAnalysisView analysis={analyses[0]} />
                <RenderPipelinePanel data={analyses[0].render_pipeline} />
                <DashboardCharts analyses={analyses} selectedPaths={selected} />
              </div>
            )
          ) : analyses.length >= 2 ? (
            <div className="flex flex-col gap-4">
              <ComparisonView analyses={analyses} selectedPaths={selected} />
              <DashboardCharts analyses={analyses} selectedPaths={selected} />
            </div>
          ) : (
            <CenterState icon={<AlertTriangle size={22} />} title={d.states.noData} />
          )}
        </div>
      </section>
    </div>
  )
}

function SessionRow({
  item,
  slot,
  legacyLabel,
  unknownAppLabel,
  packageIdLabel,
  range,
  onToggle,
  deleting,
  deleteDisabled,
  deleteLabel,
  onDelete,
}: {
  item: SessionListItem
  /** Selection-order slot (0-3) or -1 when unselected. */
  slot: number
  legacyLabel: string
  unknownAppLabel: string
  packageIdLabel: string
  range?: { start: number; end: number }
  onToggle(): void
  deleting: boolean
  deleteDisabled: boolean
  deleteLabel: string
  onDelete(): void
}) {
  const appLabel = item.app_label?.trim()
  const packageId = item.bundle_id?.trim()
  const appName = appLabel || packageId || unknownAppLabel
  const selected = slot >= 0
  return (
    <li className="group flex items-stretch gap-1">
      <button
        type="button"
        onClick={onToggle}
        aria-pressed={selected}
        className={[
          'min-w-0 flex-1 rounded-xl border px-3 py-3 text-start transition-colors',
          selected
            ? 'border-accent-line bg-accent-soft'
            : 'border-line bg-bg hover:border-accent-line hover:bg-hover',
        ].join(' ')}
        style={selected ? { boxShadow: `inset 3px 0 0 ${SESSION_COLORS[slot]}` } : undefined}
      >
        <div className="flex items-center gap-2">
          {selected ? (
            <span
              className="flex h-4 w-4 flex-none items-center justify-center rounded-[5px] text-[10px] font-bold text-bg"
              style={{ background: SESSION_COLORS[slot] }}
            >
              {SESSION_LABELS[slot]}
            </span>
          ) : (
            <span className="h-4 w-4 flex-none rounded-[5px] border border-line" />
          )}
          {item.app_icon ? <img src={item.app_icon} alt="" className="h-6 w-6 flex-none rounded-[6px] object-cover" /> : <span className="flex h-6 w-6 flex-none items-center justify-center rounded-[6px] bg-hover text-[11px] font-semibold text-muted">{appName.charAt(0).toUpperCase()}</span>}
          <span className="min-w-0 flex-1 break-words text-sm font-semibold text-ink" title={appName}>
            {appName}
          </span>
          {!appLabel && packageId && <span className="flex-none text-[9px] text-faint">{packageIdLabel}</span>}
          {item.is_legacy && (
            <span className="flex-none rounded-[4px] border border-line px-1 text-[9px] uppercase text-faint">
              {legacyLabel}
            </span>
          )}
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-x-1.5 gap-y-1 ps-6 text-xs">
          <span className="break-words text-muted">
            {item.device_display_name || item.device_model}
          </span>
          <span className="text-faint">·</span>
          <span className="uppercase text-muted">{item.platform}</span>
          <span className="text-faint">·</span>
          <span className="flex-none text-faint">{fmtDate(item.started_at)}</span>
          {range && <><span className="text-faint">·</span><span className="flex-none tabular-nums text-faint">{formatSessionRangeTime(range.start)}–{formatSessionRangeTime(range.end)}</span></>}
        </div>
      </button>
      <button
        type="button"
        onClick={onDelete}
        disabled={deleteDisabled}
        aria-label={`${deleteLabel}: ${appName}`}
        title={deleteLabel}
        className="mpi-icon-button flex-none self-center text-muted transition-colors hover:bg-rec-soft hover:text-danger disabled:cursor-wait"
      >
        {deleting ? <Loader2 size={13} className="animate-spin" /> : <Trash2 size={13} />}
      </button>
    </li>
  )
}

type RangeValue = { start: number; end: number }
type RangeDraft = { start: string; end: string }

/** Compact per-session range strip rendered inside the analysis toolbar. Each
 *  editor is a single-line pill: slot chip, app name, start–end inputs, then
 *  Reset and Apply. */
function SessionRangeEditors({ analyses, selected, sessions, ranges, drafts, labels, unknownAppLabel, onDraft, onApply, onReset }: { analyses: SessionAnalysis[]; selected: string[]; sessions: SessionListItem[]; ranges: Record<string, RangeValue>; drafts: Record<string, RangeDraft>; labels: { edit: string; start: string; end: string; apply: string; reset: string; invalid: string }; unknownAppLabel: string; onDraft(path: string, draft: RangeDraft): void; onApply(path: string, range: RangeValue): void; onReset(path: string): void }) {
  const sessionByPath = new Map(sessions.map((session) => [session.file_path, session]))
  const analysisByPath = new Map(analyses.map((analysis) => [analysis.file_path, analysis]))
  return <div aria-label={labels.edit} className="grid grid-cols-1 gap-1.5 xl:grid-cols-2">
      {selected.map((path, slot) => {
        const analysis = analysisByPath.get(path)
        const session = sessionByPath.get(path)
        if (!session) return null
        const fullDuration = session.duration_sec
        const range = ranges[path]
        const draft = drafts[path] ?? {
          start: formatSessionRangeTime(range?.start ?? 0),
          end: formatSessionRangeTime(range?.end ?? fullDuration),
        }
        const start = parseSessionRangeTime(draft.start)
        const end = parseSessionRangeTime(draft.end)
        const validRange = start !== null && end !== null && end > start && end <= fullDuration
          ? { start, end }
          : null
        const badge = SESSION_LABELS[slot]
        const appName = session.app_label?.trim() || analysis?.app_label?.trim() || session.bundle_id?.trim() || unknownAppLabel
        return <div
          key={path}
          data-testid={`range-editor-${badge}`}
          className={[
            'flex flex-wrap items-center gap-2 rounded-xl border bg-bg p-3',
            range ? 'border-accent-line' : 'border-line',
          ].join(' ')}
        >
          <span className="flex h-4 w-4 flex-none items-center justify-center rounded-[5px] text-[10px] font-bold text-bg" style={{ background: SESSION_COLORS[slot] }}>{badge}</span>
          <span className="min-w-0 flex-1 truncate text-[11px] font-medium text-ink" title={appName}>{appName}</span>
          <input aria-label={`${badge} ${labels.start}`} title={labels.start} type="text" inputMode="text" spellCheck={false} placeholder="0:00" value={draft.start} onChange={(e) => onDraft(path, { ...draft, start: e.target.value })} className="mpi-control w-[76px] border border-line bg-surface px-2 text-center font-mono text-xs tabular-nums text-ink focus:border-accent" />
          <span className="text-faint">–</span>
          <input aria-label={`${badge} ${labels.end}`} title={labels.end} type="text" inputMode="text" spellCheck={false} placeholder="0:00" value={draft.end} onChange={(e) => onDraft(path, { ...draft, end: e.target.value })} className="mpi-control w-[76px] border border-line bg-surface px-2 text-center font-mono text-xs tabular-nums text-ink focus:border-accent" />
          {/* Contract: Apply stays the last <button> inside this testid container,
              and no other descendant button may be a :last-child of its parent —
              a test clicks querySelector('button:last-child'). */}
          <div className="flex items-center gap-1">
            <button type="button" onClick={() => onReset(path)} className="mpi-control px-3 text-xs text-muted hover:text-ink">{labels.reset}</button>
            <button type="button" disabled={!validRange} onClick={() => validRange && onApply(path, validRange)} className="mpi-control border border-accent-line bg-accent-soft px-3 text-xs font-semibold text-accent disabled:opacity-40">{labels.apply}</button>
          </div>
          {!validRange && <div className="basis-full pt-0.5 text-[10px] text-rec">{labels.invalid}</div>}
        </div>
      })}
  </div>
}

function EmptyLibrary({ title, hint, cta, onImport }: { title: string; hint: string; cta: string; onImport(): void }) {
  return (
    <div className="flex flex-col items-center gap-2 px-4 py-10 text-center">
      <span className="flex h-10 w-10 items-center justify-center rounded-[12px] border border-line bg-surface text-faint">
        <Inbox size={20} />
      </span>
      <h3 className="text-sm font-semibold text-ink">{title}</h3>
      <p className="text-xs leading-relaxed text-muted">{hint}</p>
      <button
        type="button"
        onClick={onImport}
        className="mpi-control mt-3 inline-flex items-center gap-2 border border-accent-line bg-accent-soft px-4 text-sm font-medium text-accent transition-colors hover:bg-accent/15"
      >
        <Upload size={13} />
        {cta}
      </button>
    </div>
  )
}

function CenterState({
  icon,
  title,
  hint,
  tone = 'muted',
}: {
  icon: React.ReactNode
  title: string
  hint?: string
  tone?: 'muted' | 'error'
}) {
  return (
    <div className="mpi-panel flex h-full min-h-[320px] flex-col items-center justify-center gap-4 px-6 py-10 text-center">
      <span
        className={[
          'flex h-16 w-16 items-center justify-center rounded-[18px] border',
          tone === 'error' ? 'border-rec/30 bg-rec-soft text-danger' : 'border-accent-line bg-accent-soft text-accent',
        ].join(' ')}
      >
        {icon}
      </span>
      <h2 className="max-w-md text-balance text-lg font-semibold tracking-tight text-ink">{title}</h2>
      {hint && <p className="max-w-sm text-sm leading-relaxed text-muted">{hint}</p>}
    </div>
  )
}
