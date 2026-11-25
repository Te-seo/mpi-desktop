import { useEffect, useLayoutEffect, useRef } from 'react'

/**
 * Anchors a portaled dropdown panel to a trigger element using fixed
 * positioning, so the panel floats over the page instead of being clipped by a
 * scrollable / overflow-hidden ancestor (e.g. the Live sidebar). The panel is
 * rendered via createPortal to document.body and positioned here by mutating
 * its style directly (no setState — avoids extra renders and the
 * react-hooks set-state-in-effect rule). It opens downward, flipping up when
 * there isn't enough room below, and caps its height to the available space.
 *
 * Click-outside closes the panel, accounting for the panel living outside the
 * trigger's DOM subtree (it's portaled), so both refs are checked.
 */
export function useAnchoredPanel(open: boolean, onClose: () => void,
  { minWidth = 0, align = 'start' }: { minWidth?: number; align?: 'start' | 'end' } = {}) {
  const triggerRef = useRef<HTMLDivElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)

  useLayoutEffect(() => {
    if (!open) return
    const place = () => {
      const trigger = triggerRef.current
      const panel = panelRef.current
      if (!trigger || !panel) return
      const r = trigger.getBoundingClientRect()
      const gutter = 8
      const viewportHeight = window.innerHeight
      const anchorTop = Math.max(gutter, Math.min(r.top, viewportHeight - gutter))
      const anchorBottom = Math.max(gutter, Math.min(r.bottom, viewportHeight - gutter))
      const spaceBelow = Math.max(0, viewportHeight - anchorBottom - gutter)
      const spaceAbove = Math.max(0, anchorTop - gutter)
      const openUp = spaceBelow < 320 && spaceAbove > spaceBelow
      const maxH = Math.max(0, Math.floor(Math.min(viewportHeight - gutter * 2,
        (openUp ? spaceAbove : spaceBelow) - 6)))
      const width = Math.max(0, Math.min(Math.max(r.width, minWidth), window.innerWidth - gutter * 2))
      const preferredLeft = align === 'end' ? r.right - width : r.left
      const left = Math.max(gutter, Math.min(preferredLeft, window.innerWidth - width - gutter))
      panel.style.position = 'fixed'
      panel.style.left = `${Math.round(left)}px`
      panel.style.width = `${Math.round(width)}px`
      panel.style.maxHeight = `${maxH}px`
      if (openUp) {
        panel.style.top = ''
        panel.style.bottom = `${Math.round(viewportHeight - anchorTop + 6)}px`
      } else {
        panel.style.bottom = ''
        panel.style.top = `${Math.round(anchorBottom + 6)}px`
      }
    }
    place()
    window.addEventListener('scroll', place, true)
    window.addEventListener('resize', place)
    return () => {
      window.removeEventListener('scroll', place, true)
      window.removeEventListener('resize', place)
    }
  }, [open, minWidth, align])

  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      const target = e.target as Node
      if (triggerRef.current?.contains(target)) return
      if (panelRef.current?.contains(target)) return
      onClose()
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open, onClose])

  return { triggerRef, panelRef }
}
