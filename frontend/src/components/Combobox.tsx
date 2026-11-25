import { ChevronDown, Search } from 'lucide-react'
import { useEffect, useMemo, useRef, useState, type RefObject } from 'react'
import { createPortal } from 'react-dom'
import { useMessages } from '../lib/i18n'
import { useAnchoredPanel } from './useAnchoredPanel'

export interface ComboboxItem {
  value: string
  label: string
  iconUrl?: string | null
  meta?: string
}

interface Props {
  items: ComboboxItem[]
  value: string | null
  onChange(value: string | null): void
  placeholder?: string
  searchPlaceholder?: string
  disabled?: boolean
  emptyText?: string
  className?: string
  // Fired the first time an item's row enters the visible viewport (or, on
  // older browsers without IntersectionObserver, immediately on mount). Used
  // by the package selector to fetch only the icons the user can actually
  // see — populating a 200-item dropdown shouldn't hammer adb for all 200.
  onItemVisible?(value: string): void
}

export function Combobox({
  items,
  value,
  onChange,
  placeholder,
  searchPlaceholder,
  disabled = false,
  emptyText,
  className = '',
  onItemVisible,
}: Props) {
  const t = useMessages()
  const placeholderText = placeholder ?? t.combobox.placeholder
  const searchPlaceholderText = searchPlaceholder ?? t.combobox.searchPlaceholder
  const emptyTextResolved = emptyText ?? t.combobox.emptyText
  const [open, setOpen] = useState(false)
  const [filter, setFilter] = useState('')

  const close = () => {
    setOpen(false)
    setFilter('')
  }
  const { triggerRef, panelRef } = useAnchoredPanel(open, close)

  const selected = useMemo(
    () => items.find((i) => i.value === value) ?? null,
    [items, value]
  )

  const filtered = useMemo(() => {
    const q = filter.trim().toLowerCase()
    if (!q) return items
    return items.filter(
      (i) =>
        i.label.toLowerCase().includes(q) ||
        i.value.toLowerCase().includes(q) ||
        (i.meta?.toLowerCase().includes(q) ?? false)
    )
  }, [items, filter])

  return (
    <div ref={triggerRef} className={`relative w-full ${className}`}>
      <button
        type="button"
        onClick={() => !disabled && setOpen((v) => !v)}
        disabled={disabled}
        className="flex w-full items-center gap-3 rounded-[11px] border border-line bg-surface px-3 py-2.5 text-left shadow-[0_1px_2px_rgba(0,0,0,0.2)] transition-colors hover:border-accent-line disabled:opacity-50"
      >
        {selected?.iconUrl ? (
          <img
            src={selected.iconUrl}
            alt=""
            className="h-[38px] w-[38px] shrink-0 rounded-[9px] border border-grid"
            draggable={false}
          />
        ) : (
          <span className="flex h-[38px] w-[38px] shrink-0 items-center justify-center rounded-[9px] border border-grid bg-icon font-mono text-base font-bold text-accent">
            {selected?.label?.[0]?.toUpperCase() ?? '·'}
          </span>
        )}
        <span className="min-w-0 flex-1">
          {selected ? (
            <>
              <span className="block truncate text-[15px] font-medium leading-tight text-ink">
                {selected.label}
              </span>
              {selected.meta && (
                <span className="block truncate font-mono text-[11px] text-muted">
                  {selected.meta}
                </span>
              )}
            </>
          ) : (
            <span className="text-[15px] text-faint">{placeholderText}</span>
          )}
        </span>
        <ChevronDown size={15} className="shrink-0 text-muted" />
      </button>

      {open &&
        createPortal(
          <div
            ref={panelRef}
            className="z-[60] flex flex-col overflow-hidden rounded-[11px] border border-line bg-surface-hi shadow-[0_14px_40px_rgba(0,0,0,0.5)]"
          >
            <div className="flex flex-none items-center gap-2 border-b border-grid px-2.5 py-2">
              <Search size={14} className="text-faint" />
              <input
                autoFocus
                type="text"
                placeholder={searchPlaceholderText}
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                className="w-full bg-transparent text-sm text-ink outline-none placeholder:text-faint"
              />
            </div>
            <ComboboxList
              items={filtered}
              value={value}
              emptyText={emptyTextResolved}
              onSelect={(v) => {
                onChange(v)
                close()
              }}
              onItemVisible={onItemVisible}
            />
          </div>,
          document.body
        )}
    </div>
  )
}

function ComboboxList({
  items,
  value,
  emptyText,
  onSelect,
  onItemVisible,
}: {
  items: ComboboxItem[]
  value: string | null
  emptyText: string
  onSelect(value: string): void
  onItemVisible?: (value: string) => void
}) {
  const containerRef = useRef<HTMLDivElement>(null)
  return (
    <div ref={containerRef} className="min-h-0 flex-1 overflow-auto py-1">
      {items.length === 0 ? (
        <div className="px-3 py-4 text-center text-sm text-muted">{emptyText}</div>
      ) : (
        items.map((item) => (
          <ComboboxOption
            key={item.value}
            item={item}
            selected={item.value === value}
            onSelect={onSelect}
            onItemVisible={onItemVisible}
            scrollRootRef={containerRef}
          />
        ))
      )}
    </div>
  )
}

function ComboboxOption({
  item,
  selected,
  onSelect,
  onItemVisible,
  scrollRootRef,
}: {
  item: ComboboxItem
  selected: boolean
  onSelect(value: string): void
  onItemVisible?: (value: string) => void
  scrollRootRef: RefObject<HTMLDivElement | null>
}) {
  const ref = useRef<HTMLButtonElement>(null)
  const firedRef = useRef(false)

  useEffect(() => {
    if (!onItemVisible || firedRef.current) return
    const el = ref.current
    if (!el) return
    if (typeof IntersectionObserver === 'undefined') {
      // No observer — fall back to immediate fire.
      firedRef.current = true
      onItemVisible(item.value)
      return
    }
    const root = scrollRootRef.current
    const obs = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting && !firedRef.current) {
            firedRef.current = true
            onItemVisible(item.value)
            obs.disconnect()
            break
          }
        }
      },
      { root: root ?? undefined, rootMargin: '120px', threshold: 0 }
    )
    obs.observe(el)
    return () => obs.disconnect()
  }, [onItemVisible, item.value, scrollRootRef])

  return (
    <button
      ref={ref}
      type="button"
      onClick={() => onSelect(item.value)}
      className={[
        'flex w-full items-center gap-2.5 px-2.5 py-2 text-left',
        selected ? 'bg-accent-soft' : 'hover:bg-hover',
      ].join(' ')}
    >
      {item.iconUrl ? (
        <img
          src={item.iconUrl}
          alt=""
          className="h-8 w-8 shrink-0 rounded-lg border border-grid"
          draggable={false}
        />
      ) : (
        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-grid bg-icon font-mono text-sm font-bold text-accent">
          {item.label?.[0]?.toUpperCase() ?? '·'}
        </span>
      )}
      <div className="min-w-0 flex-1">
        <div className={`truncate text-sm ${selected ? 'font-semibold text-ink' : 'text-ink'}`}>
          {item.label}
        </div>
        {item.meta && (
          <div className="truncate font-mono text-[11px] text-muted">{item.meta}</div>
        )}
      </div>
    </button>
  )
}
