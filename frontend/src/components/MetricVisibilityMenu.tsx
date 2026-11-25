import { Check, ChevronDown, ChevronRight, SlidersHorizontal, X } from 'lucide-react'
import { useEffect, useId, useState } from 'react'
import { createPortal } from 'react-dom'
import { useMessages } from '../lib/i18n'
import type { TemperatureZone } from '../lib/thermalMetrics'
import { useAnchoredPanel } from './useAnchoredPanel'
import {
  isSeriesVisible,
  localizedLiveMetrics,
  type LiveMetricDef,
  type MetricVisibility,
} from '../lib/liveMetrics'

interface Props {
  visibility: MetricVisibility
  temperatureZone?: TemperatureZone
  onChange(next: MetricVisibility): void
}

export function MetricVisibilityMenu({ visibility, onChange, temperatureZone = 'battery' }: Props) {
  const t = useMessages()
  const metrics = localizedLiveMetrics(t.charts, temperatureZone)
  const [open, setOpen] = useState(false)
  // Major keys whose minor (중분류) items are expanded.
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  const panelId = useId()
  const headingId = useId()
  const { triggerRef, panelRef } = useAnchoredPanel(open, () => setOpen(false), { minWidth: 288, align: 'end' })

  useEffect(() => {
    if (!open) return
    panelRef.current?.querySelector<HTMLButtonElement>('button')?.focus()
    function onKey(event: KeyboardEvent) {
      if (event.key !== 'Escape') return
      event.preventDefault()
      setOpen(false)
      triggerRef.current?.querySelector<HTMLButtonElement>('button')?.focus()
    }
    function onFocus(event: FocusEvent) {
      const target = event.target as Node
      if (!panelRef.current?.contains(target) && !triggerRef.current?.contains(target)) setOpen(false)
    }
    document.addEventListener('keydown', onKey)
    document.addEventListener('focusin', onFocus)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.removeEventListener('focusin', onFocus)
    }
  }, [open, panelRef, triggerRef])

  function close() {
    setOpen(false)
    triggerRef.current?.querySelector<HTMLButtonElement>('button')?.focus()
  }

  function toggleMetric(m: LiveMetricDef) {
    onChange({ ...visibility, [m.key]: !isSeriesVisible(visibility, m.key) })
  }

  function toggleSub(subKey: string) {
    onChange({ ...visibility, [subKey]: !isSeriesVisible(visibility, subKey) })
  }

  return (
    <div ref={triggerRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        className={[
          'inline-flex items-center gap-1.5 whitespace-nowrap rounded-[10px] border px-3.5 py-2.5 text-sm font-medium transition-colors',
          open
            ? 'border-accent-line bg-accent-soft text-ink'
            : 'border-line bg-surface text-ink hover:border-accent-line',
        ].join(' ')}
      >
        <SlidersHorizontal size={15} aria-hidden="true" className="text-muted" />
        {t.metricMenu.triggerLabel}
      </button>

      {open && createPortal(
        <div ref={panelRef} id={panelId} role="region" aria-labelledby={headingId}
          className="z-[70] flex flex-col overflow-hidden rounded-2xl border border-line bg-surface-hi p-3 shadow-xl">
          <div className="mb-2 flex flex-none items-center justify-between gap-2">
            <span id={headingId} className="text-xs font-semibold text-muted">
              {t.metricMenu.menuHeading}
            </span>
            <button
              type="button"
              onClick={close}
              className="mpi-icon-button text-muted hover:bg-hover hover:text-ink"
              aria-label={t.common.close}
            >
              <X size={16} aria-hidden="true" />
            </button>
          </div>
          <div className="flex min-h-0 flex-col gap-0.5 overflow-y-auto overscroll-contain">
            {metrics.map((m) => {
              const on = isSeriesVisible(visibility, m.key)
              const hasSubs = !!m.subs && m.subs.length > 0
              const isExpanded = !!expanded[m.key]
              return (
                <div key={m.key}>
                  <div className="flex items-center">
                    <button
                      type="button"
                      onClick={() => toggleMetric(m)}
                      aria-pressed={on}
                      className={`flex min-h-10 min-w-0 flex-1 items-center gap-2.5 rounded-lg px-2 py-2 text-start text-sm font-medium transition-colors ${
                        on ? 'bg-accent-soft text-ink' : 'text-muted hover:bg-hover'
                      }`}
                    >
                      <span
                        aria-hidden="true"
                        className="flex h-[19px] w-[19px] flex-none items-center justify-center rounded-md border text-on-accent"
                        style={{
                          borderColor: on ? 'var(--color-accent)' : 'var(--color-control-line)',
                          backgroundColor: on ? 'var(--color-accent)' : 'transparent',
                        }}
                      >
                        {on && <Check size={12} strokeWidth={3} />}
                      </span>
                      <span
                        aria-hidden="true"
                        className="h-[9px] w-[9px] flex-none rounded-[3px]"
                        style={{ backgroundColor: m.accent }}
                      />
                      <span className="min-w-0 flex-1 break-words">{m.label}</span>
                    </button>
                    {hasSubs && (
                      <button
                        type="button"
                        onClick={() => setExpanded((p) => ({ ...p, [m.key]: !p[m.key] }))}
                        className="ms-0.5 flex min-h-10 min-w-10 flex-none items-center justify-center rounded-lg text-muted hover:bg-hover hover:text-ink"
                        aria-label={`${m.label} ${isExpanded ? t.metricMenu.collapse : t.metricMenu.expand}`}
                        aria-expanded={isExpanded}
                        aria-controls={isExpanded ? `${panelId}-${m.key}` : undefined}
                      >
                        {isExpanded ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
                      </button>
                    )}
                  </div>

                  {hasSubs && isExpanded && (
                    <div id={`${panelId}-${m.key}`} className="mb-1 ms-[26px] flex flex-col gap-0.5 border-s border-line ps-2">
                      {m.subs!.map((sub) => {
                        const subOn = isSeriesVisible(visibility, sub.key)
                        return (
                          <button
                            key={sub.key}
                            type="button"
                            onClick={() => toggleSub(sub.key)}
                            aria-pressed={subOn}
                            className={`flex min-h-10 items-center gap-2.5 rounded-lg px-2 py-1.5 text-start text-[13px] transition-colors ${subOn ? 'text-ink' : 'text-muted hover:bg-hover'}`}
                          >
                            <span
                              aria-hidden="true"
                              className="flex h-[16px] w-[16px] flex-none items-center justify-center rounded border text-on-accent"
                              style={{
                                borderColor: subOn ? 'var(--color-accent)' : 'var(--color-control-line)',
                                backgroundColor: subOn ? 'var(--color-accent)' : 'transparent',
                              }}
                            >
                              {subOn && <Check size={10} strokeWidth={3} />}
                            </span>
                            <span className="min-w-0 flex-1 break-words">{sub.label}</span>
                          </button>
                        )
                      })}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </div>,
        document.body,
      )}
    </div>
  )
}
