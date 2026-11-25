// Locale identity + persistence primitives. Kept dependency-free (no React, no
// message catalogs) so both localeState.ts and catalog.ts can import it without
// creating an import cycle.

import { migrateLegacyStorageKey } from '../storageMigration'

export const LOCALES = ['ko', 'en', 'zh'] as const
export type Locale = (typeof LOCALES)[number]

/** The app's original language — used as the fallback when nothing is stored
 *  and the OS locale doesn't match a supported language. */
export const DEFAULT_LOCALE: Locale = 'ko'

/** Mirrors the theme key style (mpi.<feature>.v1). */
export const LOCALE_STORAGE_KEY = 'mpi.locale.v1'
const LEGACY_LOCALE_STORAGE_KEY = 'perfinspect.locale.v1'

/** Native names shown in the language switcher, keyed by locale. */
export const LOCALE_NAMES: Record<Locale, string> = {
  ko: '한국어',
  en: 'English',
  zh: '中文',
}

export function isLocale(value: string | undefined | null): value is Locale {
  return !!value && (LOCALES as readonly string[]).includes(value)
}

/** Map a BCP-47 tag (e.g. "zh-CN", "en-US", "ko") to a supported locale, or
 *  null when unsupported. */
function matchLocale(tag: string | undefined): Locale | null {
  if (!tag) return null
  const lower = tag.toLowerCase()
  if (lower.startsWith('ko')) return 'ko'
  if (lower.startsWith('zh')) return 'zh'
  if (lower.startsWith('en')) return 'en'
  return null
}

/** Resolve the initial locale: a previously stored choice wins; otherwise fall
 *  back to the OS/browser language, then to the default (ko). Never throws. */
export function readStoredLocale(): Locale {
  try {
    migrateLegacyStorageKey(
      LOCALE_STORAGE_KEY,
      LEGACY_LOCALE_STORAGE_KEY,
      (value) => isLocale(value)
    )
    const raw = localStorage.getItem(LOCALE_STORAGE_KEY)
    if (isLocale(raw)) return raw
  } catch {
    /* private mode / no storage */
  }
  if (typeof navigator !== 'undefined') {
    const langs = navigator.languages ?? [navigator.language]
    for (const tag of langs) {
      const m = matchLocale(tag)
      if (m) return m
    }
  }
  return DEFAULT_LOCALE
}
