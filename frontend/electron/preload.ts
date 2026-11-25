import { clipboard, contextBridge, ipcRenderer, nativeImage, webUtils } from 'electron'

const SEND_CHANNELS = ['toMain', 'send-to-python'] as const
const RECEIVE_CHANNELS = [
  'fromMain',
  'from-python',
  'window-fullscreen-changed',
  'cloud-status-changed',
  'prepare-window-close',
  'prepare-account-change',
] as const
const INVOKE_CHANNELS = [
  'open-play-companion',
  'send-to-python',
  'open-measurement-window',
  'window-fullscreen-get',
  'window-fullscreen-set',
  'open-folder-dialog',
  'open-file-dialog',
  'save-file-dialog',
  'ios-tunnel-status',
  'ios-tunnel-install',
  'post-to-endpoint',
  'cloud-status',
  'cloud-verify-session',
  'cloud-entitlement',
  'cloud-team-workspace',
  'statistics-participation-get',
  'statistics-participation-set',
  'cloud-sign-in-google',
  'cloud-cancel-sign-in',
  'cloud-sign-out',
  'cloud-set-enabled',
  'cloud-retry',
  'cloud-enqueue',
  'window-close-ready',
  'account-view-ready',
  'account-change-ready',
] as const

type Listener = (...args: unknown[]) => void

contextBridge.exposeInMainWorld('api', {
  send(channel: string, data?: unknown) {
    if ((SEND_CHANNELS as readonly string[]).includes(channel)) {
      ipcRenderer.send(channel, data)
    }
  },
  receive(channel: string, listener: Listener) {
    if ((RECEIVE_CHANNELS as readonly string[]).includes(channel)) {
      const wrapped = (_event: Electron.IpcRendererEvent, ...args: unknown[]) =>
        listener(...args)
      ipcRenderer.on(channel, wrapped)
      return () => ipcRenderer.removeListener(channel, wrapped)
    }
    return () => undefined
  },
  invoke(channel: string, data?: unknown) {
    if ((INVOKE_CHANNELS as readonly string[]).includes(channel)) {
      return ipcRenderer.invoke(channel, data)
    }
    return Promise.reject(new Error(`Invalid channel: ${channel}`))
  },
  getPathForFile(file: Parameters<typeof webUtils.getPathForFile>[0]) {
    return webUtils.getPathForFile(file)
  },
  clipboard: {
    async writeText(text: string) {
      clipboard.writeText(text)
    },
    async writeImage(bytes: ArrayBuffer | Uint8Array) {
      const buf = bytes instanceof Uint8Array ? Buffer.from(bytes) : Buffer.from(new Uint8Array(bytes))
      const img = nativeImage.createFromBuffer(buf)
      clipboard.writeImage(img)
    },
  },
})
