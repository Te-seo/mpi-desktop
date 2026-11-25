import { RefreshCcw } from 'lucide-react'
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { call } from '../lib/ipc'
import { friendlyError } from '../lib/friendlyError'
import { useMessages } from '../lib/i18n'
import type { PackageInfo, Platform } from '../lib/types'
import { Combobox, type ComboboxItem } from './Combobox'

interface Props {
  platform?: Platform
  serial: string | null
  value: string | null
  onChange(bundleId: string | null): void
  disabled?: boolean
  connectionRevision?: number
}

/** One entry from the on-device companion's `list_apps` (PackageManager-backed). */
interface CompanionApp {
  package: string
  label: string
  system: boolean
  enabled: boolean
  version_name?: string
  version_code?: number
}

export function PackageSelector({ platform = 'android', serial, value, onChange, disabled = false, connectionRevision = 0 }: Props) {
  const t = useMessages()
  const [packages, setPackages] = useState<PackageInfo[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [icons, setIcons] = useState<Record<string, string | null>>({})
  // Friendly names resolved lazily for Android (the package list there carries
  // only bundle ids). iOS already ships display names on PackageInfo.label.
  const [labels, setLabels] = useState<Record<string, string | null>>({})
  const [showSystem, setShowSystem] = useState(false)
  const [listScope, setListScope] = useState('')
  const fetchedSerialRef = useRef<string | null>(null)
  // True when the current list came from the on-device companion app: labels ship
  // inline and icons come from it too, so the adb label-sweep / APK-icon path and
  // its per-app parsing are skipped.
  const companionRef = useRef(false)
  // Icons and labels are resolved by independent backend calls with very
  // different costs (a label may stream a multi-MB resources.arsc; an icon is a
  // single small PNG, and most modern apps ship XML-only adaptive icons that
  // resolve to null). Tracking their in-flight sets separately is what keeps a
  // missing/slow label from being permanently skipped just because its icon —
  // often null — already resolved. Coupling them is why app names could stay
  // stuck at the bundle id.
  const iconInflightRef = useRef<Set<string>>(new Set())
  const labelInflightRef = useRef<Set<string>>(new Set())
  const packagesRef = useRef<PackageInfo[]>([])
  const iconsRef = useRef<Record<string, string | null>>({})
  const labelsRef = useRef<Record<string, string | null>>({})
  const valueRef = useRef(value)
  const onChangeRef = useRef(onChange)
  const refreshTokenRef = useRef(0)
  const metadataGenerationRef = useRef(0)
  const requestScope = `${platform}:${serial ?? ''}:${connectionRevision}:${showSystem}`
  const requestScopeRef = useRef(requestScope)
  const fetchedPlatformRef = useRef(platform)
  const queueRef = useRef({ icon: new Set<string>(), label: new Set<string>() })
  const flushTimerRef = useRef<number | null>(null)
  const batchGenerationRef = useRef<{ icon: number | null; label: number | null }>({ icon: null, label: null })
  const flushQueueRef = useRef<() => void>(() => undefined)
  const retryTimersRef = useRef<Set<number>>(new Set())
  const retriedRef = useRef<Set<string>>(new Set())

  const resetMetadataWork = useCallback(() => {
    metadataGenerationRef.current += 1
    queueRef.current.icon.clear()
    queueRef.current.label.clear()
    batchGenerationRef.current = { icon: null, label: null }
    if (flushTimerRef.current !== null) window.clearTimeout(flushTimerRef.current)
    flushTimerRef.current = null
    retryTimersRef.current.forEach((timer) => window.clearTimeout(timer))
    retryTimersRef.current.clear()
    retriedRef.current.clear()
    iconInflightRef.current.clear()
    labelInflightRef.current.clear()
  }, [])

  useEffect(() => {
    packagesRef.current = packages
  }, [packages])

  useEffect(() => {
    iconsRef.current = icons
  }, [icons])

  useEffect(() => {
    labelsRef.current = labels
  }, [labels])

  useLayoutEffect(() => {
    valueRef.current = value
  }, [value])

  useLayoutEffect(() => {
    onChangeRef.current = onChange
  }, [onChange])

  useLayoutEffect(() => {
    requestScopeRef.current = requestScope
  }, [requestScope])

  const refresh = useCallback(async () => {
    const token = ++refreshTokenRef.current
    resetMetadataWork()
    const targetScope = requestScope
    if (!serial) {
      packagesRef.current = []
      setPackages([])
      setIcons({})
      setLabels({})
      setLoading(false)
      setError(null)
      iconsRef.current = {}
      labelsRef.current = {}
      iconInflightRef.current.clear()
      labelInflightRef.current.clear()
      fetchedSerialRef.current = null
      companionRef.current = false
      return
    }
    const targetSerial = serial
    setLoading(true)
    setError(null)
    companionRef.current = false

    // Start both paths together, but never make the one-command adb inventory
    // wait for the Play companion bridge. A later companion result
    // upgrades labels and versions in place.
    const companionPromise = platform === 'android'
      ? call<{ apps: CompanionApp[] }>('companion_app_list', { serial: targetSerial, user_only: !showSystem }, 15_000).then(
          (value) => ({ ok: true as const, value }),
          () => ({ ok: false as const })
        )
      : null

    const isCurrent = () =>
      refreshTokenRef.current === token && requestScopeRef.current === targetScope

    const commitBase = (pkgs: PackageInfo[], fromCompanion: boolean) => {
      if (!isCurrent()) return
      resetMetadataWork()
      companionRef.current = fromCompanion
      const oldById = new Map(packagesRef.current.map((pkg) => [pkg.bundle_id, pkg]))
      const sameDevice = fetchedSerialRef.current === targetSerial && fetchedPlatformRef.current === platform
      const unchanged = (pkg: PackageInfo) => {
        const previous = oldById.get(pkg.bundle_id)
        return sameDevice && previous &&
          (!previous.apk_path || !pkg.apk_path || previous.apk_path === pkg.apk_path) &&
          (!previous.version_code || !pkg.version_code || previous.version_code === pkg.version_code)
      }
      const preserve = (cache: Record<string, string | null>) => Object.fromEntries(
        pkgs.filter((pkg) => cache[pkg.bundle_id] && unchanged(pkg))
          .map((pkg) => [pkg.bundle_id, cache[pkg.bundle_id]])
      )
      const nextIcons = preserve(iconsRef.current)
      const nextLabels = preserve(labelsRef.current)
      for (const pkg of pkgs) {
        const previous = oldById.get(pkg.bundle_id)
        if (unchanged(pkg) && previous?.label && previous.label !== pkg.bundle_id) {
          nextLabels[pkg.bundle_id] = previous.label
        }
      }
      packagesRef.current = pkgs
      setPackages([...pkgs])
      setListScope(targetScope)
      setLoading(false)
      setIcons(nextIcons)
      setLabels(nextLabels)
      iconsRef.current = nextIcons
      labelsRef.current = nextLabels
      fetchedSerialRef.current = targetSerial
      fetchedPlatformRef.current = platform
      if (!pkgs.find((p) => p.bundle_id === valueRef.current)) {
        const next = pkgs[0]?.bundle_id ?? null
        valueRef.current = next
        onChangeRef.current(next)
      }
    }

    const fromCompanion = (apps: CompanionApp[], base: PackageInfo[] = []) => {
      apps = apps.filter((app) => showSystem || !app.system)
      const details = new Map(apps.map((app) => [app.package, app]))
      const seen = new Set<string>()
      const merged = base.map((pkg) => {
        seen.add(pkg.bundle_id)
        const app = details.get(pkg.bundle_id)
        if (!app) return pkg
        return {
          ...pkg,
          label: app.label || pkg.label,
          version_name: app.version_name ?? pkg.version_name,
          version_code: app.version_code != null ? String(app.version_code) : pkg.version_code,
        }
      })
      for (const app of apps) {
        if (seen.has(app.package)) continue
        merged.push({
          bundle_id: app.package,
          label: app.label || null,
          version_name: app.version_name ?? null,
          version_code: app.version_code != null ? String(app.version_code) : null,
          apk_path: null,
          uid: null,
        })
      }
      return merged
    }

    try {
      let pkgs: PackageInfo[] | null = null
      let apps: CompanionApp[] | null = null
      let committed = false
      const inventory = call<{ packages: PackageInfo[] }>('list_packages', {
        platform, serial: targetSerial, user_only: !showSystem,
      }).then((result) => {
        pkgs = result.packages
        commitBase(apps ? fromCompanion(apps, pkgs) : pkgs, apps !== null)
        committed = true
      })
      const enrichment = companionPromise?.then((companion) => {
        if (!companion.ok || !isCurrent()) return
        apps = companion.value.apps
        commitBase(fromCompanion(apps, pkgs ?? []), true)
        committed = true
      })
      // Publish either source immediately; the slower source only enriches it.
      const results = await Promise.allSettled([inventory, enrichment])
      if (!committed && results[0].status === 'rejected') throw results[0].reason
    } catch (err) {
      if (isCurrent()) setError(err instanceof Error ? err.message : String(err))
    } finally {
      if (isCurrent()) setLoading(false)
    }
  }, [platform, serial, showSystem, requestScope, resetMetadataWork])

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void refresh()
    }, 0)
    return () => window.clearTimeout(timer)
  }, [refresh])

  // Keep the two lanes independent: APK label extraction must never hold up
  // icons for subsequent rows. Companion icons need no APK parsing, so amortize
  // the bridge setup over 32 icons instead of eight.
  const flushQueue = useCallback(() => {
    flushTimerRef.current = null
    if (!serial) return
    const targetSerial = serial
    const targetScope = requestScope
    const generation = metadataGenerationRef.current
    const isCurrent = () => generation === metadataGenerationRef.current &&
      fetchedSerialRef.current === targetSerial && requestScopeRef.current === targetScope
    const packageById = new Map(packagesRef.current.map((pkg) => [pkg.bundle_id, pkg]))
    const schedule = () => {
      if (isCurrent() && flushTimerRef.current === null) {
        flushTimerRef.current = window.setTimeout(() => flushQueueRef.current(), 0)
      }
    }
    for (const kind of ['icon', 'label'] as const) {
      if (batchGenerationRef.current[kind] === generation) continue
      const queue = queueRef.current[kind]
      const cacheRef = kind === 'icon' ? iconsRef : labelsRef
      const inflight = kind === 'icon' ? iconInflightRef : labelInflightRef
      const selected = valueRef.current
      const queued = Array.from(queue)
      const ordered = selected && queue.has(selected)
        ? [selected, ...queued.filter((id) => id !== selected)] : queued
      const limit = kind === 'icon' && companionRef.current ? 32 : 8
      const ids = ordered.filter((id) => packageById.has(id) &&
        !(id in cacheRef.current) && !inflight.current.has(id)).slice(0, limit)
      // Discard completed/removed entries without dropping work in later batches.
      for (const id of queued) {
        if (!packageById.has(id) || id in cacheRef.current) queue.delete(id)
      }
      if (!ids.length) continue
      ids.forEach((id) => { queue.delete(id); inflight.current.add(id) })
      batchGenerationRef.current[kind] = generation
      const requests = ids.map((id) => ({ package: id, apk_path: packageById.get(id)!.apk_path }))
      const receive = (values: Record<string, string | null>) => {
        if (!isCurrent()) return
        const next = { ...cacheRef.current }
        for (const id of ids) next[id] = kind === 'label' && values[id] === id ? null : values[id] || null
        cacheRef.current = next
        if (kind === 'icon') setIcons(next)
        else setLabels(next)
        const retry = ids.filter((id) => !next[id] && !retriedRef.current.has(`${kind}:${id}`))
        if (!retry.length) return
        retry.forEach((id) => retriedRef.current.add(`${kind}:${id}`))
        const timer = window.setTimeout(() => {
          retryTimersRef.current.delete(timer)
          if (!isCurrent()) return
          cacheRef.current = { ...cacheRef.current }
          for (const id of retry) {
            if (cacheRef.current[id]) continue
            delete cacheRef.current[id]
            queueRef.current[kind].add(id)
          }
          schedule()
        }, 2000)
        retryTimersRef.current.add(timer)
      }
      const request = kind === 'icon'
        ? companionRef.current
          ? call<{ icons: Record<string, string | null> }>('companion_icons', {
              serial: targetSerial, packages: ids,
            }).then((result) => result.icons)
          : call<{ icons: Record<string, string | null> }>('fetch_app_icons', {
              platform, serial: targetSerial, requests,
            }).then((result) => result.icons)
        : call<{ labels: Record<string, string | null> }>('fetch_app_labels', {
            platform, serial: targetSerial, requests,
          }).then((result) => result.labels)
      void request.then(receive, () => receive({})).finally(() => {
        if (!isCurrent()) return
        ids.forEach((id) => inflight.current.delete(id))
        batchGenerationRef.current[kind] = null
        if (queueRef.current[kind].size) schedule()
      })
    }
  }, [platform, serial, requestScope])

  useLayoutEffect(() => {
    flushQueueRef.current = flushQueue
  }, [flushQueue])

  const fetchMeta = useCallback((bundleId: string) => {
    if (!serial || !bundleId || requestScopeRef.current !== requestScope) return
    const pkg = packagesRef.current.find((entry) => entry.bundle_id === bundleId)
    if (!pkg) return
    if (!(bundleId in iconsRef.current) && !iconInflightRef.current.has(bundleId)) {
      queueRef.current.icon.add(bundleId)
    }
    if (platform === 'android' && (!pkg.label || pkg.label === bundleId) &&
      !(bundleId in labelsRef.current) && !labelInflightRef.current.has(bundleId)) {
      queueRef.current.label.add(bundleId)
    }
    if (flushTimerRef.current === null && (queueRef.current.icon.size || queueRef.current.label.size)) {
      flushTimerRef.current = window.setTimeout(() => flushQueueRef.current(), 0)
    }
  }, [serial, platform, requestScope])

  // Cancel any pending flush when the selector unmounts or serial changes.
  useEffect(() => {
    return () => {
      refreshTokenRef.current += 1
      resetMetadataWork()
    }
  }, [platform, serial, connectionRevision, showSystem, resetMetadataWork])

  // Eagerly load the icon (and Android label) for the currently-selected
  // package so the closed dropdown shows them without opening the menu.
  useEffect(() => {
    if (!value) return
    fetchMeta(value)
  }, [value, fetchMeta])

  // Search must not depend on which virtualized rows have become visible.
  // Feed every refreshed package through the existing coalescing queue so names
  // and icons arrive in bulk while preserving the per-kind in-flight tracking.
  useEffect(() => {
    packages.forEach((pkg) => fetchMeta(pkg.bundle_id))
  }, [packages, fetchMeta])

  const sectionTitle = platform === 'ios' ? t.packageSelector.sectionTitleIos : t.packageSelector.sectionTitleAndroid
  const placeholder = platform === 'ios' ? t.packageSelector.placeholderIos : t.packageSelector.placeholderAndroid
  const searchPlaceholder = platform === 'ios' ? t.packageSelector.searchPlaceholderIos : t.packageSelector.searchPlaceholderAndroid
  const emptyText = platform === 'ios' ? t.packageSelector.emptyIos : t.packageSelector.emptyAndroid

  const items = useMemo<ComboboxItem[]>(
    () =>
      (listScope === requestScope ? packages : []).map((p) => {
        const b64 = icons[p.bundle_id]
        const name = (p.label !== p.bundle_id ? p.label : null) || labels[p.bundle_id] || p.bundle_id
        const hasName = name !== p.bundle_id
        return {
          value: p.bundle_id,
          label: name,
          meta: hasName ? p.bundle_id : undefined,
          iconUrl: b64 ? `data:image/png;base64,${b64}` : null,
        }
      }),
    [packages, icons, labels, listScope, requestScope]
  )

  return (
    <div>
      <div className="mb-2 flex items-center justify-between">
        <span className="text-[11px] font-semibold uppercase tracking-[0.12em] text-faint">
          {sectionTitle}
        </span>
        <button
          type="button"
          onClick={refresh}
          disabled={disabled || loading || !serial}
          className="rounded-md p-1 text-muted transition-colors hover:bg-hover hover:text-ink disabled:cursor-not-allowed disabled:opacity-50"
          aria-label={t.packageSelector.refreshAppList}
          title={t.packageSelector.refreshAppList}
        >
          <RefreshCcw size={14} className={loading ? 'animate-spin' : undefined} />
        </button>
      </div>
      <label className="mb-2 flex w-fit cursor-pointer items-center gap-2 text-xs text-muted">
        <input
          type="checkbox"
          checked={showSystem}
          onChange={(event) => setShowSystem(event.target.checked)}
          disabled={disabled || !serial}
          className="h-3.5 w-3.5 accent-accent disabled:cursor-not-allowed"
        />
        {t.packageSelector.showSystemApps}
      </label>
      <Combobox
        items={items}
        value={value}
        onChange={onChange}
        placeholder={placeholder}
        searchPlaceholder={searchPlaceholder}
        disabled={disabled || loading || items.length === 0}
        emptyText={loading ? t.packageSelector.loading : emptyText}
        onItemVisible={fetchMeta}
      />
      {error && (
        <p className="mt-1.5 text-xs text-rec" title={error}>
          {friendlyError(error, t.errors)}
        </p>
      )}
    </div>
  )
}
