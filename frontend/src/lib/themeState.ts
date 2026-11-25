import { createContext, useContext } from 'react'
import { migrateLegacyStorageKey } from './storageMigration'

export type Theme = 'dark' | 'light'

export const THEME_STORAGE_KEY = 'mpi.theme.v1'
const LEGACY_THEME_STORAGE_KEY = 'perfinspect.theme.v1'

export interface ThemeState {
  theme: Theme
  setTheme(theme: Theme): void
  toggleTheme(): void
}

export const ThemeContext = createContext<ThemeState | null>(null)

export function useTheme(): ThemeState {
  const ctx = useContext(ThemeContext)
  if (!ctx) {
    throw new Error('useTheme must be used inside ThemeProvider')
  }
  return ctx
}

export function readStoredTheme(): Theme {
  try {
    migrateLegacyStorageKey(
      THEME_STORAGE_KEY,
      LEGACY_THEME_STORAGE_KEY,
      (value) => value === 'light' || value === 'dark'
    )
    const raw = localStorage.getItem(THEME_STORAGE_KEY)
    if (raw === 'light' || raw === 'dark') return raw
  } catch {
    /* private mode / no storage */
  }
  // Default to dark (the design's primary theme). If nothing is stored yet,
  // honour the class the pre-paint script in index.html already applied.
  if (typeof document !== 'undefined' && document.documentElement.classList.contains('light')) {
    return 'light'
  }
  return 'dark'
}

export function applyTheme(theme: Theme): void {
  if (typeof document === 'undefined') return
  const el = document.documentElement
  if (el.classList.contains(theme)) return
  const transitionOverride = document.createElement('style')
  transitionOverride.textContent = '*,*::before,*::after{transition:none!important}'
  document.head.append(transitionOverride)
  el.classList.remove('dark', 'light')
  el.classList.add(theme)
  void el.offsetHeight
  if (typeof requestAnimationFrame === 'function') {
    requestAnimationFrame(() => requestAnimationFrame(() => transitionOverride.remove()))
  } else {
    transitionOverride.remove()
  }
}
