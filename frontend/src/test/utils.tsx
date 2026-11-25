import { render } from '@testing-library/react'
import type { ReactElement } from 'react'
import { LocaleProvider } from '../lib/i18n'

/** Render inside the locale provider, pinned to English so assertions on
 *  translated text are deterministic regardless of the host navigator locale. */
export function renderWithLocale(ui: ReactElement) {
  try {
    localStorage.setItem('mpi.locale.v1', 'en')
  } catch {
    /* ignore */
  }
  return render(<LocaleProvider>{ui}</LocaleProvider>)
}
