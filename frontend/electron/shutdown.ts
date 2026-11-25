export type GracefulShutdownResult = 'graceful' | 'forced'

export interface GracefulShutdownOptions {
  /** Queue stop_capture / stop_temperature_guard for every renderer-owned resource. */
  stopOwnedResources(): void
  /** Queue the backend shutdown command after all stop commands. */
  sendShutdown(): boolean
  /** Resolve true only when the sidecar process has fully closed. */
  waitForExit(): Promise<boolean>
  /** Kill the sidecar process tree when graceful shutdown misses the deadline. */
  forceTerminate(): Promise<void>
  timeoutMs: number
}

function waitWithin(promise: Promise<boolean>, timeoutMs: number): Promise<boolean> {
  return new Promise((resolve) => {
    let settled = false
    const finish = (value: boolean) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      resolve(value)
    }
    const timer = setTimeout(() => finish(false), Math.max(0, timeoutMs))
    void promise.then(finish, () => finish(false))
  })
}

/**
 * Orderly, bounded Electron -> sidecar shutdown.
 *
 * Stop commands and shutdown are written to the same stdin pipe in order. The
 * Python API drains its already-dispatched workers and capture cleanup before
 * exiting. We wait for the process `close` event, not merely the JSON shutdown
 * response, because DVT/Diagnostics child processes may still be tearing down.
 */
export async function runGracefulShutdown(
  options: GracefulShutdownOptions
): Promise<GracefulShutdownResult> {
  try {
    options.stopOwnedResources()
  } catch {
    // Still ask the backend to perform its global cleanup below.
  }

  let shutdownSent = false
  try {
    shutdownSent = options.sendShutdown()
  } catch {
    shutdownSent = false
  }

  if (shutdownSent) {
    let exited = false
    try {
      exited = await waitWithin(options.waitForExit(), options.timeoutMs)
    } catch {
      exited = false
    }
    if (exited) return 'graceful'
  }

  try {
    await options.forceTerminate()
  } catch {
    // Quitting must remain bounded even if the OS tree-kill utility fails.
  }
  return 'forced'
}
