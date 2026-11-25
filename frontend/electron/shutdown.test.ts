import { describe, expect, it, vi } from 'vitest'
import { runGracefulShutdown } from './shutdown'

describe('graceful Electron sidecar shutdown', () => {
  it('queues owned stops before shutdown and waits for full process close', async () => {
    const order: string[] = []
    const forceTerminate = vi.fn(async () => {})

    const result = await runGracefulShutdown({
      stopOwnedResources() {
        order.push('stops')
      },
      sendShutdown() {
        order.push('shutdown')
        return true
      },
      async waitForExit() {
        order.push('close')
        return true
      },
      forceTerminate,
      timeoutMs: 100,
    })

    expect(order).toEqual(['stops', 'shutdown', 'close'])
    expect(result).toBe('graceful')
    expect(forceTerminate).not.toHaveBeenCalled()
  })

  it('falls back to process-tree termination after the bounded deadline', async () => {
    vi.useFakeTimers()
    const forceTerminate = vi.fn(async () => {})
    const resultPromise = runGracefulShutdown({
      stopOwnedResources: vi.fn(),
      sendShutdown: () => true,
      waitForExit: () => new Promise<boolean>(() => {}),
      forceTerminate,
      timeoutMs: 250,
    })

    await vi.advanceTimersByTimeAsync(250)

    await expect(resultPromise).resolves.toBe('forced')
    expect(forceTerminate).toHaveBeenCalledOnce()
    vi.useRealTimers()
  })

  it('forces immediately when the sidecar stdin is already unavailable', async () => {
    const waitForExit = vi.fn(async () => true)
    const forceTerminate = vi.fn(async () => {})

    await expect(
      runGracefulShutdown({
        stopOwnedResources: vi.fn(),
        sendShutdown: () => false,
        waitForExit,
        forceTerminate,
        timeoutMs: 100,
      })
    ).resolves.toBe('forced')
    expect(waitForExit).not.toHaveBeenCalled()
    expect(forceTerminate).toHaveBeenCalledOnce()
  })
})
