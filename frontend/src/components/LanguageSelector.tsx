import { useEffect, useRef, useState } from 'react'
import { Languages, Check } from 'lucide-react'
import { LOCALES, LOCALE_NAMES, useLocale, useMessages } from '../lib/i18n'

/** Header language switcher (한국어 / English / 中文). Persists via LocaleProvider. */
export function LanguageSelector() {
  const { locale, setLocale } = useLocale()
  const t = useMessages()
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (!open) return
    function onDown(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        setOpen(false)
        triggerRef.current?.focus()
      }
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  return (
    <div ref={ref} className="relative">
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-label={t.localeSelector.label}
        title={t.localeSelector.label}
        aria-expanded={open}
        className="mpi-control inline-flex items-center gap-2 border border-line bg-surface px-3 py-2 text-[13px] font-medium text-ink transition-colors hover:border-accent-line"
      >
        <Languages size={15} className="text-muted" />
        <span className="mpi-language-name font-semibold">{LOCALE_NAMES[locale]}</span>
      </button>
      {open && (
        <ul
          aria-label={t.localeSelector.label}
          className="absolute right-0 z-50 mt-2 min-w-[160px] overflow-hidden rounded-[16px] border border-line bg-surface p-1 shadow-lg"
        >
          {LOCALES.map((l) => (
            <li key={l}>
              <button
                type="button"
                aria-pressed={l === locale}
                onClick={() => {
                  setLocale(l)
                  setOpen(false)
                  triggerRef.current?.focus()
                }}
                className="mpi-control flex w-full items-center justify-between gap-3 px-3 py-2 text-left text-[13px] text-ink hover:bg-accent-soft"
              >
                <span className={l === locale ? 'font-semibold' : ''}>{LOCALE_NAMES[l]}</span>
                {l === locale && <Check size={14} className="text-accent" />}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
