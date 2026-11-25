import { Moon, Sun } from 'lucide-react'
import { useTheme } from '../lib/themeState'
import { useMessages } from '../lib/i18n'

export function ThemeToggle() {
  const { theme, toggleTheme } = useTheme()
  const t = useMessages()
  const isDark = theme === 'dark'
  const switchLabel = isDark ? t.theme.switchToLight : t.theme.switchToDark

  return (
    <button
      type="button"
      onClick={toggleTheme}
      aria-label={switchLabel}
      title={switchLabel}
      className="mpi-control inline-flex items-center gap-2 border border-line bg-surface px-3 py-2 text-[13px] font-medium text-ink transition-colors hover:border-accent-line"
    >
      {isDark ? <Moon size={15} className="text-muted" /> : <Sun size={15} className="text-muted" />}
      <span className="mpi-theme-label text-muted">{t.theme.label}</span>
      <span className="mpi-theme-name font-semibold">{isDark ? t.theme.dark : t.theme.light}</span>
    </button>
  )
}
