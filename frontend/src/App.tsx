import { useState } from 'react'
import { Activity, ChartNoAxesCombined } from 'lucide-react'
import appIcon from '../build-assets/icon.png'
import { LiveCaptureProvider } from './components/LiveCaptureProvider'
import { EngineStatusBanner } from './components/EngineStatusBanner'
import { LanguageSelector } from './components/LanguageSelector'
import { ThemeProvider } from './components/ThemeProvider'
import { ThemeToggle } from './components/ThemeToggle'
import { SettingsButton } from './components/SettingsButton'
import { AccountGate } from './components/AccountGate'
import { TeamWorkspace } from './components/TeamWorkspace'
import { PlanAllowance } from './components/PlanAllowance'
import { LocaleProvider, useMessages } from './lib/i18n'
import { LiveTracing } from './pages/LiveTracing'
import { AnalysisDashboard } from './pages/AnalysisDashboard'

type Screen = 'live' | 'dashboard' | 'team'

function NavButton({
  active,
  onClick,
  children,
}: {
  active: boolean
  onClick(): void
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-current={active ? 'page' : undefined}
      className={[
        'mpi-nav-button inline-flex min-h-10 items-center justify-center gap-2 rounded-[10px] px-4 py-2 font-semibold transition-colors',
        active ? 'bg-accent-soft text-accent shadow-[inset_0_-2px_0_var(--color-accent)]' : 'text-muted hover:bg-hover hover:text-ink',
      ].join(' ')}
    >
      {children}
    </button>
  )
}

function AppContent({ownerId}: {ownerId:string}) {
  const t = useMessages()
  // Default screen stays on the existing live-measurement view.
  const [screen, setScreen] = useState<Screen>('live')
  return (
    <div className="mpi-app-shell flex h-dvh min-h-0 flex-col">
      <a href="#mpi-workspace" className="mpi-skip-link">{t.app.skipContent}</a>
      <header className="mpi-app-header flex flex-none items-center gap-6 border-b border-line bg-surface px-6 py-3.5">
        <div className="mpi-brand flex min-w-0 items-center gap-3">
          <img src={appIcon} alt="" aria-hidden="true" className="h-10 w-10 flex-none rounded-[12px] object-contain" />
          <div className="min-w-0 leading-snug">
            <div className="flex items-baseline gap-2 font-display text-[20px] font-semibold tracking-tight text-ink">
              {t.app.title}
              <span className="text-[11px] font-medium tracking-[0.08em] text-muted">{t.app.workspace}</span>
            </div>
            <div className="mpi-brand-description text-[12px] text-muted">{t.app.subtitle}</div>
          </div>
        </div>

        <nav aria-label={t.app.navigationLabel} className="mpi-primary-nav flex items-center gap-1 rounded-[15px] bg-bg p-1 text-[13px]">
          <NavButton active={screen === 'live'} onClick={() => setScreen('live')}>
            <Activity size={17} aria-hidden="true" />
            {t.dashboard.nav.live}
          </NavButton>
          <NavButton active={screen === 'dashboard'} onClick={() => setScreen('dashboard')}>
            <ChartNoAxesCombined size={17} aria-hidden="true" />
            {t.dashboard.nav.dashboard}
          </NavButton>
        </nav>

        <div className="mpi-header-tools ml-auto flex flex-none items-center gap-2">
          <LanguageSelector />
          <ThemeToggle />
          <SettingsButton ownerId={ownerId} />
        </div>
      </header>
      <EngineStatusBanner />
      <PlanAllowance onOpenTeam={() => setScreen('team')} />
      <main id="mpi-workspace" tabIndex={-1} aria-label={screen === 'live' ? t.dashboard.nav.live : screen === 'team' ? t.cloud.teamCloud : t.dashboard.nav.dashboard} className="min-h-0 flex-1 overflow-hidden">
        {/* Both screens stay mounted so switching never tears down an in-progress
            capture (its state lives in LiveCaptureProvider) or resets the live
            view's local UI. The inactive screen is hidden, not unmounted. */}
        <div className={screen === 'live' ? 'h-full' : 'hidden'}>
          <LiveTracing />
        </div>
        <div className={screen === 'dashboard' ? 'h-full' : 'hidden'}>
          <AnalysisDashboard active={screen === 'dashboard'} />
        </div>
        {screen === 'team' && <TeamWorkspace viewerId={ownerId} />}
      </main>
    </div>
  )
}

export default function App() {
  return (
    <LocaleProvider>
      <ThemeProvider>
        <AccountGate>{(ownerId) => <LiveCaptureProvider key={ownerId}>
          <AppContent ownerId={ownerId} />
        </LiveCaptureProvider>}</AccountGate>
      </ThemeProvider>
    </LocaleProvider>
  )
}
