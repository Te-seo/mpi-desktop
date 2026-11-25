import { act, render, waitFor } from '@testing-library/react'
import { useEffect } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AUTO_STOP_STORAGE_KEY } from '../lib/autoStop'
import { LocaleProvider } from '../lib/i18n'
import type { BackendResponse } from '../lib/ipc'
import { useLiveCapture, type LiveCaptureState } from '../lib/liveCaptureState'
import type { DeviceInfo, MetricsSnapshot, PackageInfo } from '../lib/types'
import { LiveCaptureProvider } from './LiveCaptureProvider'

const ipc = vi.hoisted(() => ({
  call: vi.fn(),
  saveFile: vi.fn(),
  cloudCommand: vi.fn(),
  enqueueCloud: vi.fn(),
  listeners: new Set<(message: BackendResponse) => void>(),
}))

vi.mock('../lib/ipc', () => ({
  call: ipc.call,
  onEvent(listener: (message: BackendResponse) => void) {
    ipc.listeners.add(listener)
    return () => ipc.listeners.delete(listener)
  },
  saveFile: ipc.saveFile,
  postToEndpoint: vi.fn(),
}))

vi.mock('../lib/cloud-sync', () => ({
  cloudCommand: ipc.cloudCommand,
  enqueueCloudSession: ipc.enqueueCloud,
  cloudErrorMessage: () => 'Cloud save failed',
}))

const device: DeviceInfo = {
  platform: 'android',
  serial: 'android-1',
  model: 'Pixel',
  os_version: '16',
  connection_type: 'usb',
}

const selectedPackage: PackageInfo = {
  bundle_id: 'com.example.game',
  label: 'Example Game',
  version_name: '1.0',
  version_code: '1',
  apk_path: null,
  uid: null,
}

const snapshot: MetricsSnapshot = {
  device,
  package: selectedPackage,
  captured_at: '2026-07-21T00:00:00Z',
  duration_sec: 0.5,
  frames: {
    total_frames: 30,
    janky_frames: 0,
    small_jank: 0,
    big_jank: 0,
    jank_percent: 0,
    p50_ms: 16.6,
    p90_ms: 16.6,
    p95_ms: 16.6,
    p99_ms: 16.6,
    missed_vsync: 0,
    slow_ui_thread: 0,
    frame_deadline_missed: 0,
    fps_estimate: 60,
  },
  memory: {
    pss_total_kb: 100,
    rss_total_kb: 100,
    java_heap_kb: 0,
    native_heap_kb: 0,
    graphics_kb: 0,
    code_kb: 0,
    stack_kb: 0,
    sys_total_kb: 1000,
    sys_used_kb: 500,
  },
  cpu: {
    pid: 123,
    cpu_percent: 10,
    mem_percent: 1,
    total_percent: 20,
    per_core_freq_mhz: [],
    cpu_norm: 5,
  },
  gpu: { busy_percent: 20, clock_mhz: 500, temp_c: 40 },
  thermal: { zones_c: {} },
  battery: { level_percent: 90, current_ma: -500, status: 'discharging', voltage_mv: 4000 },
  raw: {},
}

let capture: LiveCaptureState

function Harness() {
  const state = useLiveCapture()
  useEffect(() => {
    capture = state
  }, [state])
  return null
}

function renderProvider() {
  render(
    <LocaleProvider>
      <LiveCaptureProvider>
        <Harness />
      </LiveCaptureProvider>
    </LocaleProvider>
  )
}

async function selectTarget() {
  await act(async () => capture.setDeviceSelection(device.serial, device))
  await act(async () => capture.setBundleId(selectedPackage.bundle_id))
}

function dispatch(message: Partial<BackendResponse>) {
  for (const listener of ipc.listeners) listener({ id: null, ok: true, ...message })
}

describe('LiveCaptureProvider automatic stop', () => {
  it.each([[0, false], [1, false], [3, false], [1, true]] as const)(
    'retains acknowledged pause history for %i pauses (end paused=%s), all saves, and resets for a new session',
    async (pauses, endPaused) => {
      ipc.cloudCommand.mockResolvedValue({signedIn:true,enabled:true,userId:'11111111-1111-4111-8111-111111111111'})
      ipc.call.mockImplementation(async (command: string, args?: {snapshots?: MetricsSnapshot[]}) => {
        if (command==='start_capture') return {capture_id:'capture-1',device,package:selectedPackage,duration_sec:0.5}
        if (command==='build_endpoint_payload') return {metadata:{capture:{pause_used:args?.snapshots?.some(s=>s.pause_used===true)}},summary:{},timeseries:{columns:[],rows:[]}}
        return {}
      })
      ipc.saveFile.mockResolvedValue('session.csv')
      renderProvider(); await selectTarget(); await act(async()=>capture.start())
      act(()=>dispatch({event:'snapshot',capture_id:'capture-1',data:{...snapshot,elapsed_sec:0}}))
      for(let i=0;i<pauses;i++) act(()=>{
        dispatch({event:'capture_state',capture_id:'capture-1',paused:true,pause_used:true,elapsed_sec:0})
        if(!endPaused) dispatch({event:'capture_state',capture_id:'capture-1',paused:false,pause_used:true,elapsed_sec:0})
      })
      act(()=>dispatch({event:'capture_ended',capture_id:'capture-1',pause_used:pauses>0}))
      await waitFor(()=>expect(ipc.enqueueCloud).toHaveBeenCalledTimes(1))
      const saved=ipc.call.mock.calls.find(([cmd])=>cmd==='auto_save_raw_csv')![1]
      expect(saved.snapshots.some((s:MetricsSnapshot)=>s.pause_used===true)).toBe(pauses>0)
      expect(ipc.enqueueCloud.mock.lastCall?.[0].payload.metadata.capture.pause_used).toBe(pauses>0)
      await act(async()=>capture.exportCsv())
      expect(ipc.enqueueCloud.mock.lastCall?.[0].payload.metadata.capture.pause_used).toBe(pauses>0)
      await act(async()=>capture.start())
      act(()=>{
        dispatch({event:'snapshot',capture_id:'capture-1',data:snapshot})
        dispatch({event:'capture_ended',capture_id:'capture-1',pause_used:false})
      })
      expect(ipc.call.mock.calls.filter(([cmd])=>cmd==='auto_save_raw_csv').at(-1)?.[1].snapshots).toEqual([snapshot])
    })

  it('does not mark a failed pause request as pause use',async()=>{
    renderProvider();await selectTarget();await act(async()=>capture.start())
    act(()=>dispatch({event:'snapshot',capture_id:'capture-1',data:snapshot}))
    ipc.call.mockRejectedValueOnce(Error('pause rejected'))
    await act(async()=>capture.togglePause())
    act(()=>dispatch({event:'capture_ended',capture_id:'capture-1',pause_used:false}))
    expect(ipc.call.mock.calls.find(([cmd])=>cmd==='auto_save_raw_csv')![1].snapshots).toEqual([snapshot])
  })

  it('freezes during pause, resumes the same capture, and persists completed/open cuts', async () => {
    renderProvider()
    await selectTarget()
    await act(async () => capture.start())
    act(() => dispatch({ event: 'snapshot', capture_id: 'capture-1', data: { ...snapshot, elapsed_sec: 0 } }))
    act(() => capture.toggleCut())
    expect(capture.activeCut?.start_sec).toBe(0)
    act(() => dispatch({ event: 'snapshot', capture_id: 'capture-1', data: { ...snapshot, elapsed_sec: 2 } }))
    act(() => capture.toggleCut())
    expect(capture.cuts).toEqual([expect.objectContaining({ start_sec: 0, end_sec: 2 })])
    await act(async () => capture.togglePause())
    expect(ipc.call).toHaveBeenLastCalledWith('pause_capture', { capture_id: 'capture-1' })
    act(() => dispatch({ event: 'capture_state', capture_id: 'capture-1', paused: true, elapsed_sec: 2 }))
    expect(capture.isPaused).toBe(true)
    expect(capture.selectionLocked).toBe(true)
    expect(capture.elapsedSec).toBe(2)
    vi.useFakeTimers()
    act(() => vi.advanceTimersByTime(30_000))
    expect(capture.elapsedSec).toBe(2)
    await act(async () => capture.togglePause())
    expect(ipc.call).toHaveBeenLastCalledWith('resume_capture', { capture_id: 'capture-1' })
    act(() => dispatch({ event: 'capture_state', capture_id: 'capture-1', paused: false, elapsed_sec: 2 }))
    expect(capture.captureId).toBe('capture-1')
    act(() => capture.toggleCut())
    act(() => {
      dispatch({ event: 'snapshot', capture_id: 'capture-1', data: { ...snapshot, elapsed_sec: 5 } })
      dispatch({ event: 'capture_ended', capture_id: 'capture-1', elapsed_sec: 5 })
    })
    expect(capture.activeCut).toBeNull()
    expect(capture.isPaused).toBe(false)
    expect(capture.cuts).toHaveLength(2)
    expect(ipc.call).toHaveBeenCalledWith('auto_save_raw_csv', expect.objectContaining({ cuts: [expect.objectContaining({ start_sec: 0, end_sec: 2 }), expect.objectContaining({ start_sec: 2, end_sec: 5 })] }))
    vi.useRealTimers()
    await act(async () => capture.start())
    expect(capture.cuts).toEqual([])
  })

  it('ignores a late pause state from an earlier capture', async () => {
    renderProvider()
    await selectTarget()
    await act(async () => capture.start())
    act(() => dispatch({ event: 'capture_state', capture_id: 'old-capture', paused: true, elapsed_sec: 10 }))
    expect(capture.isPaused).toBe(false)
  })

  it('keeps collecting through a missed sample and clears only its retry notice on recovery', async () => {
    renderProvider()
    await selectTarget()
    await act(async () => capture.start())
    const retry = { event: 'capture_error', capture_id: 'capture-1', recoverable: true, error: 'read timed out' }
    act(() => dispatch(retry))
    expect(capture.phase).toBe('running')
    expect(capture.error).toBeNull()
    expect(capture.sampleRetrying).toBe(true)
    expect(capture.snapshots).toHaveLength(0)

    ipc.call.mockRejectedValueOnce(Error('pause rejected'))
    await act(async () => capture.togglePause())
    act(() => dispatch({ event: 'snapshot', capture_id: 'capture-1', data: snapshot }))
    expect(capture.sampleRetrying).toBe(false)
    expect(capture.snapshots).toEqual([snapshot])
    expect(capture.error).toBe('pause rejected')

    act(() => dispatch(retry))
    act(() => dispatch({ event: 'capture_state', capture_id: 'capture-1', paused: true }))
    act(() => dispatch(retry))
    expect(capture.sampleRetrying).toBe(false)
    expect(capture.phase).toBe('paused')
  })

  it('retains the final error and saved samples when the target exits during a retry', async () => {
    renderProvider()
    await selectTarget()
    await act(async () => capture.start())
    act(() => {
      dispatch({ event: 'snapshot', capture_id: 'capture-1', data: snapshot })
      dispatch({ event: 'capture_error', capture_id: 'capture-1', recoverable: true, error: 'read timed out' })
      dispatch({ event: 'capture_error', capture_id: 'capture-1', error: 'target exited' })
      dispatch({ event: 'capture_ended', capture_id: 'capture-1' })
    })
    expect(capture.phase).toBe('error')
    expect(capture.error).toContain('target exited')
    expect(capture.sampleRetrying).toBe(false)
    expect(ipc.call).toHaveBeenCalledWith('auto_save_raw_csv', expect.objectContaining({ snapshots: [snapshot] }))
  })

  afterEach(() => { vi.useRealTimers() })

  beforeEach(() => {
    localStorage.clear()
    localStorage.setItem('mpi.locale.v1', 'en')
    ipc.call.mockReset()
    ipc.saveFile.mockReset()
    ipc.cloudCommand.mockReset()
    ipc.enqueueCloud.mockReset()
    ipc.cloudCommand.mockResolvedValue({ signedIn: false, enabled: false, userId: null })
    ipc.enqueueCloud.mockResolvedValue({ ok: true, status: { error: null } })
    Object.defineProperty(window, 'api', { configurable: true, value: undefined })
    ipc.listeners.clear()
    ipc.call.mockImplementation(async (command: string) => {
      if (command === 'start_capture') {
        return {
          capture_id: 'capture-1',
          device,
          package: selectedPackage,
          duration_sec: 0.5,
        }
      }
      return {}
    })
  })

  it('retains all samples outside the chart window and isolates consecutive session exports', async () => {
    const ownerId = '11111111-1111-4111-8111-111111111111'
    ipc.cloudCommand.mockResolvedValue({ signedIn: true, enabled: true, userId: ownerId })
    let captureNumber = 0
    ipc.call.mockImplementation(async (command: string) => {
      if (command === 'start_capture') {
        captureNumber += 1
        return { capture_id: `capture-${captureNumber}`, session_id: `stable-${captureNumber}`,
          device, package: selectedPackage, duration_sec: 0.5 }
      }
      if (command === 'build_endpoint_payload') return { metadata: {}, summary: {}, timeseries: { columns: [], rows: [] } }
      return {}
    })
    ipc.saveFile.mockResolvedValue('D:/MPI/build/test-session.csv')
    renderProvider()
    await selectTarget()
    await act(async () => capture.start())
    act(() => {
      for (let i = 0; i < 7201; i += 1)
        dispatch({ id: null, ok: true, event: 'snapshot', capture_id: 'capture-1', data: snapshot })
      dispatch({ id: null, ok: true, event: 'capture_ended', capture_id: 'capture-1' })
    })
    await waitFor(() => expect(ipc.enqueueCloud).toHaveBeenCalledTimes(1))
    expect(capture.snapshots).toHaveLength(7200)
    const saved = ipc.call.mock.calls.find(([command]) => command === 'auto_save_raw_csv')![1]
    expect(saved.snapshots).toHaveLength(7201)
    expect(saved.session_id).toBe('stable-1')
    const originalSnapshots = saved.snapshots
    await act(async () => capture.exportCsv())
    expect(ipc.enqueueCloud.mock.lastCall?.[0]).toMatchObject({ ownerId, sessionId: 'stable-1' })

    await act(async () => capture.start())
    act(() => {
      dispatch({ id: null, ok: true, event: 'snapshot', capture_id: 'capture-2', data: { ...snapshot, captured_at: '2026-07-21T01:00:00Z' } })
      dispatch({ id: null, ok: true, event: 'capture_ended', capture_id: 'capture-2' })
    })
    await waitFor(() => expect(ipc.enqueueCloud).toHaveBeenCalledTimes(3))
    const saves = ipc.call.mock.calls.filter(([command]) => command === 'auto_save_raw_csv')
    expect(saves[1][1].snapshots).toHaveLength(1)
    expect(saves[1][1].session_id).toBe('stable-2')
    expect(originalSnapshots).toHaveLength(7201)
    await act(async () => capture.exportXlsx())
    expect(ipc.enqueueCloud.mock.lastCall?.[0]).toMatchObject({ ownerId, sessionId: 'stable-2' })
  })

  it('keeps a closing window alive until its final sample and durable cloud queue are saved', async () => {
    ipc.cloudCommand.mockResolvedValue({ signedIn: true, enabled: true,
      userId: '11111111-1111-4111-8111-111111111111' })
    let closeListener!: () => void
    const invoke = vi.fn(async () => ({}))
    Object.defineProperty(window, 'api', { configurable: true, value: {
      receive(channel: string, listener: () => void) {
        if (channel === 'prepare-window-close') closeListener = listener
        return () => {}
      }, invoke,
    } })
    let finishDiskWrite!: () => void
    const diskWrite = new Promise<void>((resolve) => { finishDiskWrite = resolve })
    ipc.call.mockImplementation(async (command: string) => {
      if (command === 'start_capture') return { capture_id: 'capture-1', session_id: 'close-session', device, package: selectedPackage, duration_sec: 0.5 }
      if (command === 'stop_capture') {
        dispatch({ id: null, ok: true, event: 'snapshot', capture_id: 'capture-1', data: snapshot })
        dispatch({ id: null, ok: true, event: 'capture_ended', capture_id: 'capture-1' })
      }
      if (command === 'auto_save_raw_csv') await diskWrite
      return {}
    })
    renderProvider()
    await selectTarget()
    await act(async () => capture.start())
    act(() => dispatch({ id: null, ok: true, event: 'snapshot', capture_id: 'capture-1', data: snapshot }))
    await act(async () => closeListener())
    expect(invoke).not.toHaveBeenCalledWith('window-close-ready')
    const raw = ipc.call.mock.calls.find(([command]) => command === 'auto_save_raw_csv')![1]
    expect(raw.snapshots).toHaveLength(2)
    expect(raw.session_id).toBe('close-session')
    await act(async () => finishDiskWrite())
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('window-close-ready'))
    expect(ipc.enqueueCloud).toHaveBeenCalledTimes(1)
    const starts = ipc.call.mock.calls.filter(([command]) => command === 'start_capture').length
    await act(async () => capture.start())
    expect(ipc.call.mock.calls.filter(([command]) => command === 'start_capture')).toHaveLength(starts)
  })

  it('waits for final durable saves before acknowledging an account change', async () => {
    const ownerId = '11111111-1111-4111-8111-111111111111'
    ipc.cloudCommand.mockResolvedValue({ signedIn: true, enabled: true, userId: ownerId })
    let changeAccount!: () => void
    const invoke = vi.fn(async () => ({}))
    Object.defineProperty(window, 'api', { configurable: true, value: {
      receive(channel: string, listener: () => void) {
        if (channel === 'prepare-account-change') changeAccount = listener
        return () => {}
      }, invoke,
    } })
    let finishDisk!: () => void
    const disk = new Promise<void>(resolve => { finishDisk = resolve })
    ipc.call.mockImplementation(async (command: string) => {
      if (command === 'start_capture') return { capture_id: 'capture-1', session_id: 'original-account-session', device, package: selectedPackage, duration_sec: 0.5 }
      if (command === 'stop_capture') dispatch({ id: null, ok: true, event: 'capture_ended', capture_id: 'capture-1' })
      if (command === 'auto_save_raw_csv') await disk
      return {}
    })
    renderProvider(); await selectTarget(); await act(async () => capture.start())
    act(() => dispatch({ id: null, ok: true, event: 'snapshot', capture_id: 'capture-1', data: snapshot }))
    await act(async () => changeAccount())
    expect(invoke).toHaveBeenCalledWith('account-view-ready')
    expect(invoke).not.toHaveBeenCalledWith('account-change-ready')
    const starts = ipc.call.mock.calls.filter(([command]) => command === 'start_capture').length
    await act(async () => capture.start())
    expect(ipc.call.mock.calls.filter(([command]) => command === 'start_capture')).toHaveLength(starts)
    await act(async () => finishDisk())
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('account-change-ready'))
    expect(invoke).not.toHaveBeenCalledWith('window-close-ready')
    expect(ipc.enqueueCloud).toHaveBeenCalledWith(expect.objectContaining({ ownerId, sessionId: 'original-account-session' }))
  })

  it('does not let a delayed close fallback overwrite final cloud or raw samples', async () => {
    ipc.cloudCommand.mockResolvedValue({ signedIn: true, enabled: true,
      userId: '11111111-1111-4111-8111-111111111111' })
    let closeListener!: () => void
    const invoke = vi.fn(async () => ({}))
    Object.defineProperty(window, 'api', { configurable: true, value: {
      receive(channel: string, listener: () => void) {
        if (channel === 'prepare-window-close') closeListener = listener
        return () => {}
      }, invoke,
    } })
    let finishPartialPayload!: () => void
    const partialPayload = new Promise<void>((resolve) => { finishPartialPayload = resolve })
    let finishPartialRaw!: () => void
    const partialRaw = new Promise<void>((resolve) => { finishPartialRaw = resolve })
    const rawWrites = new Map<string, number>()
    ipc.call.mockImplementation(async (command: string, args?: { snapshots?: MetricsSnapshot[]; close_backup?: boolean }) => {
      if (command === 'start_capture') return { capture_id: 'capture-1', session_id: 'close-session', device, package: selectedPackage, duration_sec: 0.5 }
      if (command === 'stop_capture') await new Promise<void>(() => {})
      const count = args?.snapshots?.length ?? 0
      if (command === 'auto_save_raw_csv') {
        if (count === 1) await partialRaw
        rawWrites.set(args?.close_backup ? 'recovery' : 'complete', count)
      }
      if (command === 'build_endpoint_payload') {
        if (count === 1) await partialPayload
        return { metadata: {}, summary: { count }, timeseries: { columns: [], rows: [] } }
      }
      return {}
    })
    renderProvider()
    await selectTarget()
    await act(async () => capture.start())
    act(() => dispatch({ id: null, ok: true, event: 'snapshot', capture_id: 'capture-1', data: snapshot }))
    vi.useFakeTimers()
    await act(async () => closeListener())
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
    expect(ipc.call.mock.calls.filter(([command]) => command === 'auto_save_raw_csv')).toHaveLength(1)
    expect(ipc.enqueueCloud).not.toHaveBeenCalled()
    act(() => {
      dispatch({ id: null, ok: true, event: 'snapshot', capture_id: 'capture-1', data: snapshot })
      dispatch({ id: null, ok: true, event: 'capture_ended', capture_id: 'capture-1' })
    })
    await act(async () => {})
    expect(ipc.enqueueCloud).toHaveBeenCalledTimes(1)
    expect(invoke).not.toHaveBeenCalledWith('window-close-ready')
    await act(async () => finishPartialRaw())
    await act(async () => finishPartialPayload())
    expect.soft(ipc.enqueueCloud).toHaveBeenCalledTimes(1)
    expect.soft(ipc.enqueueCloud.mock.lastCall?.[0].payload.summary.count).toBe(2)
    // Same timestamp filenames must live in separate complete/recovery folders.
    expect.soft(rawWrites.get('complete')).toBe(2)
    expect.soft(rawWrites.get('recovery')).toBe(1)
    expect(invoke).toHaveBeenCalledWith('window-close-ready')
  })

  it('backs up received samples at the close deadline when the engine fails to end', async () => {
    ipc.cloudCommand.mockResolvedValue({ signedIn: true, enabled: true,
      userId: '11111111-1111-4111-8111-111111111111' })
    let closeListener!: () => void
    const invoke = vi.fn(async () => ({}))
    Object.defineProperty(window, 'api', { configurable: true, value: {
      receive(channel: string, listener: () => void) {
        if (channel === 'prepare-window-close') closeListener = listener
        return () => {}
      }, invoke,
    } })
    ipc.call.mockImplementation(async (command: string) => {
      if (command === 'start_capture') return { capture_id: 'capture-1', session_id: 'failed-session', device, package: selectedPackage, duration_sec: 0.5 }
      if (command === 'stop_capture') throw new Error('engine-not-running')
      return {}
    })
    renderProvider()
    await selectTarget()
    await act(async () => capture.start())
    act(() => dispatch({ id: null, ok: true, event: 'snapshot', capture_id: 'capture-1', data: snapshot }))
    vi.useFakeTimers()
    await act(async () => closeListener())
    await act(async () => { await vi.advanceTimersByTimeAsync(9_999) })
    expect(invoke).not.toHaveBeenCalledWith('window-close-ready')
    expect(ipc.enqueueCloud).not.toHaveBeenCalled()
    await act(async () => { await vi.advanceTimersByTimeAsync(1) })
    expect(ipc.call).toHaveBeenCalledWith('auto_save_raw_csv', expect.objectContaining({
      session_id: 'failed-session', snapshots: [snapshot], close_backup: true,
    }))
    expect(ipc.enqueueCloud).toHaveBeenCalledTimes(1)
    expect(invoke).toHaveBeenCalledWith('window-close-ready')
  })

  it('dispatches the final raw save while a close backup write is stalled', async () => {
    let closeListener!: () => void
    const invoke = vi.fn(async () => ({}))
    Object.defineProperty(window, 'api', { configurable: true, value: {
      receive(channel: string, listener: () => void) {
        if (channel === 'prepare-window-close') closeListener = listener
        return () => {}
      }, invoke,
    } })
    let finishBackup!: () => void
    const backupWrite = new Promise<void>((resolve) => { finishBackup = resolve })
    ipc.call.mockImplementation(async (command: string, args?: { snapshots?: MetricsSnapshot[] }) => {
      if (command === 'start_capture') return { capture_id: 'capture-1', session_id: 'close-session', device, package: selectedPackage, duration_sec: 0.5 }
      if (command === 'stop_capture') await new Promise<void>(() => {})
      if (command === 'auto_save_raw_csv' && args?.snapshots?.length === 1) await backupWrite
      return {}
    })
    renderProvider()
    await selectTarget()
    await act(async () => capture.start())
    act(() => dispatch({ id: null, ok: true, event: 'snapshot', capture_id: 'capture-1', data: snapshot }))
    vi.useFakeTimers()
    await act(async () => closeListener())
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
    await act(async () => {
      dispatch({ id: null, ok: true, event: 'snapshot', capture_id: 'capture-1', data: snapshot })
      dispatch({ id: null, ok: true, event: 'capture_ended', capture_id: 'capture-1' })
    })
    // Main's 30s deadline can destroy the renderer before the backup responds.
    // Dispatch the complete save independently while the window still exists.
    const fullSave = ipc.call.mock.calls.find(([command, args]) => command === 'auto_save_raw_csv' && args.snapshots.length === 2)
    expect.soft(fullSave).toBeDefined()
    expect(invoke).not.toHaveBeenCalledWith('window-close-ready')
    await act(async () => finishBackup())
    expect(invoke).toHaveBeenCalledWith('window-close-ready')
  })

  it('does not overwrite a completed cloud session with an export opened during capture', async () => {
    ipc.cloudCommand.mockResolvedValue({ signedIn: true, enabled: true,
      userId: '11111111-1111-4111-8111-111111111111' })
    let resolveDialog!: (value: string) => void
    ipc.saveFile.mockImplementation(() => new Promise<string>((resolve) => { resolveDialog = resolve }))
    renderProvider()
    await selectTarget()
    await act(async () => capture.start())
    act(() => dispatch({ id: null, ok: true, event: 'snapshot', capture_id: 'capture-1', data: snapshot }))
    let exportPromise!: Promise<void>
    await act(async () => { exportPromise = capture.exportCsv() })
    act(() => {
      dispatch({ id: null, ok: true, event: 'snapshot', capture_id: 'capture-1', data: snapshot })
      dispatch({ id: null, ok: true, event: 'capture_ended', capture_id: 'capture-1' })
    })
    await waitFor(() => expect(ipc.enqueueCloud).toHaveBeenCalledTimes(1))
    const automatic = ipc.call.mock.calls.find(([command]) => command === 'build_endpoint_payload')![1]
    expect(automatic.snapshots).toHaveLength(2)
    await act(async () => { resolveDialog('D:/MPI/build/delayed-export.csv'); await exportPromise })
    expect(ipc.enqueueCloud).toHaveBeenCalledTimes(1)
  })

  it('waits for an earlier session save when a later session finishes before window close', async () => {
    const ownerId = '11111111-1111-4111-8111-111111111111'
    ipc.cloudCommand.mockResolvedValue({ signedIn: true, enabled: true, userId: ownerId })
    let closeListener!: () => void
    const invoke = vi.fn(async () => ({}))
    Object.defineProperty(window, 'api', { configurable: true, value: {
      receive(channel: string, listener: () => void) {
        if (channel === 'prepare-window-close') closeListener = listener
        return () => {}
      }, invoke,
    } })
    let finishFirstPayload!: () => void
    const firstPayload = new Promise<void>((resolve) => { finishFirstPayload = resolve })
    let captureNumber = 0
    ipc.call.mockImplementation(async (command: string, args?: { session_id?: string }) => {
      if (command === 'start_capture') {
        captureNumber += 1
        return { capture_id: `capture-${captureNumber}`, session_id: `stable-${captureNumber}`,
          device, package: selectedPackage, duration_sec: 0.5 }
      }
      if (command === 'build_endpoint_payload' && args?.session_id === 'stable-1') await firstPayload
      return {}
    })
    renderProvider()
    await selectTarget()
    await act(async () => capture.start())
    act(() => {
      dispatch({ id: null, ok: true, event: 'snapshot', capture_id: 'capture-1', data: snapshot })
      dispatch({ id: null, ok: true, event: 'capture_ended', capture_id: 'capture-1' })
    })
    await act(async () => capture.start())
    act(() => {
      dispatch({ id: null, ok: true, event: 'snapshot', capture_id: 'capture-2', data: snapshot })
      dispatch({ id: null, ok: true, event: 'capture_ended', capture_id: 'capture-2' })
    })
    // Later captures can persist concurrently while the first payload is still being built.
    await waitFor(() => expect(ipc.enqueueCloud).toHaveBeenCalledTimes(1))
    expect(ipc.enqueueCloud.mock.calls[0][0]).toMatchObject({ ownerId, sessionId: 'stable-2' })
    await act(async () => closeListener())
    expect(invoke).not.toHaveBeenCalledWith('window-close-ready')
    await act(async () => finishFirstPayload())
    await waitFor(() => expect(invoke).toHaveBeenCalledWith('window-close-ready'))
    expect(ipc.enqueueCloud).toHaveBeenCalledTimes(2)
    expect(ipc.enqueueCloud.mock.calls[1][0]).toMatchObject({ ownerId, sessionId: 'stable-1' })
  })

  it('omits the deadline while OFF and sends it while ON', async () => {
    renderProvider()
    await selectTarget()
    await act(async () => capture.start())

    expect(ipc.call).toHaveBeenCalledWith('start_capture', {
      platform: 'android',
      serial: 'android-1',
      package: 'com.example.game',
      duration_sec: 0.5,
      // Android targets always carry the (here disabled) on-device HUD config.
      overlay: { enabled: false, metrics: ['elapsed', 'fps', 'cpu', 'gpu', 'skin', 'net'] },
    })

    act(() => {
      dispatch({ id: null, ok: true, event: 'capture_ended', capture_id: 'capture-1' })
    })
    await waitFor(() => expect(capture.captureId).toBeNull())

    await act(async () => capture.setAutoStopEnabled(true))
    await act(async () => capture.setAutoStopAfterSec(2))
    await act(async () => capture.start())

    expect(ipc.call).toHaveBeenLastCalledWith('start_capture', {
      platform: 'android',
      serial: 'android-1',
      package: 'com.example.game',
      duration_sec: 0.5,
      auto_stop_after_sec: 2,
      overlay: { enabled: false, metrics: ['elapsed', 'fps', 'cpu', 'gpu', 'skin', 'net'] },
    })
  })

  it('persists the last snapshot and freezes at the backend deadline', async () => {
    localStorage.setItem(
      AUTO_STOP_STORAGE_KEY,
      JSON.stringify({ enabled: true, afterSec: 2 })
    )
    renderProvider()
    await selectTarget()
    await act(async () => capture.start())

    act(() => {
      dispatch({
        id: null,
        ok: true,
        event: 'snapshot',
        capture_id: 'capture-1',
        data: snapshot,
      })
      dispatch({
        id: null,
        ok: true,
        event: 'capture_ended',
        capture_id: 'capture-1',
        reason: 'auto_stop',
        elapsed_sec: 2,
      })
    })

    await waitFor(() => expect(capture.captureId).toBeNull())
    expect(capture.elapsedSec).toBe(2)
    expect(ipc.call).toHaveBeenCalledWith(
      'auto_save_raw_csv',
      expect.objectContaining({
        capture_id: 'capture-1',
        snapshots: [snapshot],
      })
    )
  })
})
