import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { LOCALE_STORAGE_KEY, readStoredLocale, type Locale } from './config'
import { DICTS } from './catalog'
import { LocaleContext, type LocaleState } from './localeState'

export function LocaleProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(readStoredLocale)

  // Keep <html lang> and the persisted value in sync with state.
  useEffect(() => {
    if (typeof document !== 'undefined') {
      document.documentElement.lang = locale
    }
    try {
      localStorage.setItem(LOCALE_STORAGE_KEY, locale)
    } catch {
      /* ignore quota / private mode */
    }
  }, [locale])

  const setLocale = useCallback((next: Locale) => setLocaleState(next), [])

  const value = useMemo<LocaleState>(
    () => ({ locale, setLocale, messages: DICTS[locale] }),
    [locale, setLocale]
  )

  return <LocaleContext.Provider value={value}>{children}</LocaleContext.Provider>
}
