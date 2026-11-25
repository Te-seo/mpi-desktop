import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import {
  CrosshairContext,
  type CrosshairState,
  type Domain,
  type MenuPos,
  type Pin,
  type Selection,
} from './liveCrosshair'
import type { LiveMetricKey } from '../lib/liveMetrics'

/** Two pins are "the same" when within this many seconds — clicks land on a
 *  decimated sample, so an exact float match would never toggle a pin off. */
const SAME_PIN_SEC = 0.5

export function LiveCrosshairProvider({ children }: { children: ReactNode }) {
  const [hoverT, setHoverTState] = useState<number | null>(null)
  const [hoverKey, setHoverKeyState] = useState<LiveMetricKey | null>(null)
  const [pins, setPins] = useState<Pin[]>([])
  const [selection, setSelection] = useState<Selection | null>(null)
  const [menu, setMenu] = useState<MenuPos | null>(null)
  const [xDomain, setXDomain] = useState<Domain>(null)

  // Coalesce hover updates to one per animation frame. A high-poll-rate mouse
  // can fire several mousemove events per frame; without this, every one
  // re-renders all metric cards (each an O(n) recharts redraw), so the
  // crosshair fell progressively behind the cursor as samples accumulated.
  const rafRef = useRef<number | null>(null)
  const pendingRef = useRef<{ t: number; key: LiveMetricKey | null } | null>(null)
  const setHover = useCallback((t: number | null, key: LiveMetricKey | null) => {
    if (t === null) {
      pendingRef.current = null
      if (rafRef.current !== null) {
        cancelAnimationFrame(rafRef.current)
        rafRef.current = null
      }
      setHoverTState(null)
      setHoverKeyState(null)
      return
    }
    pendingRef.current = { t, key }
    if (rafRef.current === null) {
      rafRef.current = requestAnimationFrame(() => {
        rafRef.current = null
        const pending = pendingRef.current
        if (pending) {
          setHoverTState(pending.t)
          setHoverKeyState(pending.key)
        }
      })
    }
  }, [])
  useEffect(
    () => () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current)
    },
    []
  )

  // A chart can disappear under the pointer when changing views or scrolling.
  // Native boundaries and focus events also dismiss portaled hover content.
  useEffect(() => {
    const clear = () => setHover(null, null)
    const onPointer = (event: Event) => {
      if (!(event.target instanceof Element) || !event.target.closest('[data-live-chart-plot]')) clear()
    }
    window.addEventListener('mousemove', onPointer, true)
    window.addEventListener('pointermove', onPointer, true)
    window.addEventListener('pointerdown', onPointer, true)
    window.addEventListener('focusin', onPointer, true)
    window.addEventListener('blur', clear)
    window.addEventListener('scroll', clear, true)
    document.addEventListener('visibilitychange', clear)
    return () => {
      window.removeEventListener('mousemove', onPointer, true)
      window.removeEventListener('pointermove', onPointer, true)
      window.removeEventListener('pointerdown', onPointer, true)
      window.removeEventListener('focusin', onPointer, true)
      window.removeEventListener('blur', clear)
      window.removeEventListener('scroll', clear, true)
      document.removeEventListener('visibilitychange', clear)
    }
  }, [setHover])

  // Plain click: if the only pin is here, clear it; otherwise collapse to a
  // single pin at this point (a fresh single selection).
  const togglePin = useCallback((t: number, key: LiveMetricKey) => {
    setPins((prev) => {
      if (prev.length === 1 && Math.abs(prev[0].t - t) <= SAME_PIN_SEC) return []
      return [{ t, key }]
    })
  }, [])

  // Ctrl/Cmd+click: toggle this point within the existing set, so several pins
  // can coexist and any one can be removed without touching the others.
  const addPin = useCallback((t: number, key: LiveMetricKey) => {
    setPins((prev) => {
      const without = prev.filter((p) => Math.abs(p.t - t) > SAME_PIN_SEC)
      if (without.length !== prev.length) return without
      return [...prev, { t, key }]
    })
  }, [])

  const clearPins = useCallback(() => setPins([]), [])
  const openMenu = useCallback((pos: MenuPos) => setMenu(pos), [])
  const closeMenu = useCallback(() => setMenu(null), [])
  const resetZoom = useCallback(() => setXDomain(null), [])

  // ESC cancels the zoom and clears any in-progress selection/menu. (Fullscreen
  // exit is handled separately at the page level.)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setXDomain(null)
        setSelection(null)
        setMenu(null)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const value = useMemo<CrosshairState>(
    () => ({
      hoverT,
      hoverKey,
      setHover,
      pins,
      togglePin,
      addPin,
      clearPins,
      selection,
      setSelection,
      menu,
      openMenu,
      closeMenu,
      xDomain,
      setXDomain,
      resetZoom,
    }),
    [
      hoverT,
      hoverKey,
      setHover,
      pins,
      togglePin,
      addPin,
      clearPins,
      selection,
      menu,
      openMenu,
      closeMenu,
      xDomain,
      resetZoom,
    ]
  )

  return <CrosshairContext.Provider value={value}>{children}</CrosshairContext.Provider>
}
