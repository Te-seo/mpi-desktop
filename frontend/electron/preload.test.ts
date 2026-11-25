// @vitest-environment node
import { beforeEach, expect, it, vi } from 'vitest'

const electron = vi.hoisted(() => ({
  contextBridge: { exposeInMainWorld: vi.fn() },
  ipcRenderer: { invoke: vi.fn().mockResolvedValue({}), send: vi.fn(), on: vi.fn(), removeListener: vi.fn() },
  clipboard: {}, nativeImage: {}, webUtils: {},
}))
vi.mock('electron', () => electron)

beforeEach(() => { vi.resetModules(); vi.clearAllMocks() })

it('exposes Google and session controls while rejecting removed credential login IPC', async () => {
  await import('./preload')
  const api = electron.contextBridge.exposeInMainWorld.mock.calls[0][1] as {
    invoke: (channel: string, data?: unknown) => Promise<unknown>
  }
  await expect(api.invoke('cloud-sign-in', { email: 'test@example.test', password: 'synthetic' })).rejects.toThrow('Invalid channel')
  expect(electron.ipcRenderer.invoke).not.toHaveBeenCalled()
  for (const channel of ['cloud-sign-in-google', 'cloud-cancel-sign-in', 'cloud-sign-out', 'cloud-verify-session']) {
    await api.invoke(channel)
    expect(electron.ipcRenderer.invoke).toHaveBeenLastCalledWith(channel, undefined)
  }
})
