import { createContext, useContext } from 'react'
import type { Locale } from './config'
import { DICTS, type Messages } from './catalog'

export interface LocaleState {
  locale: Locale
  setLocale(locale: Locale): void
  /** The active locale's full message dictionary. */
  messages: Messages
}

export const LocaleContext = createContext<LocaleState | null>(null)

export function useLocale(): LocaleState {
  const ctx = useContext(LocaleContext)
  if (!ctx) {
    throw new Error('useLocale must be used inside LocaleProvider')
  }
  return ctx
}

/** Messages for the active locale. Prefer this in components:
 *  `const t = useMessages()` then `t.deviceSelector.title`. */
export function useMessages(): Messages {
  return useLocale().messages
}

export { DICTS }
export type { Messages }
