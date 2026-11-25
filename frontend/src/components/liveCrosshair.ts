import { createContext, useContext } from 'react'
import type { LiveMetricKey } from '../lib/liveMetrics'

export type Domain = [number, number] | null

/** A pinned point: a time and the metric card it was placed on. */
export interface Pin {
  t: number
  key: LiveMetricKey
}

/** An active drag region. `mode` distinguishes a summary selection (drag) from a
 *  zoom preview (Shift+drag) so overlays/colours can react differently. */
export interface Selection {
  startT: number
  endT: number
  mode: 'summary' | 'zoom'
}

/** Screen position for the right-click "save range" context menu. */
export interface MenuPos {
  x: number
  y: number
}

export interface CrosshairState {
  hoverT: number | null
  /** Metric card the cursor is currently over — drives the overlay's headline. */
  hoverKey: LiveMetricKey | null
  setHover(t: number | null, key: LiveMetricKey | null): void

  /** All pinned points. Empty = nothing pinned. */
  pins: Pin[]
  /** Plain click: collapse to a single pin here, or clear if it's the only pin. */
  togglePin(t: number, key: LiveMetricKey): void
  /** Ctrl/Cmd+click: add a pin (or remove that exact one if already pinned). */
  addPin(t: number, key: LiveMetricKey): void
  clearPins(): void

  /** Active drag region (summary or zoom preview), or null. */
  selection: Selection | null
  setSelection(s: Selection | null): void

  /** Right-click "save range" menu position, or null when closed. */
  menu: MenuPos | null
  openMenu(pos: MenuPos): void
  closeMenu(): void

  xDomain: Domain
  setXDomain(d: Domain): void
  resetZoom(): void
}

export const CrosshairContext = createContext<CrosshairState | null>(null)

export function useCrosshair(): CrosshairState {
  const ctx = useContext(CrosshairContext)
  if (!ctx) {
    throw new Error('useCrosshair must be inside LiveCrosshairProvider')
  }
  return ctx
}

/** Distinct colours cycled across multiple pins (by pin index). */
export const PIN_COLORS = ['#f0584d', '#22b378', '#f59e0b', '#a855f7', '#06b6d4']
