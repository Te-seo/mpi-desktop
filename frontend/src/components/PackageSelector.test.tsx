import { useState } from 'react'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { PackageInfo } from '../lib/types'

const callMock = vi.hoisted(() => vi.fn())

vi.mock('../lib/ipc', () => ({ call: callMock }))
vi.mock('../lib/i18n', () => ({
  useMessages: () => ({
    packageSelector: {
      sectionTitleAndroid: 'Installed apps',
      sectionTitleIos: 'Installed apps',
      placeholderAndroid: 'Choose app',
      placeholderIos: 'Choose app',
      searchPlaceholderAndroid: 'Search apps',
      searchPlaceholderIos: 'Search apps',
      emptyAndroid: 'No apps',
      emptyIos: 'No apps',
      loading: 'Loading',
      refreshAppList: 'Refresh',
      showSystemApps: 'Show system apps',
    },
    errors: {},
  }),
}))
vi.mock('./Combobox', () => ({
  Combobox: ({ items }: { items: Array<{ value: string; label: string; iconUrl?: string | null }> }) => {
    const [query, setQuery] = useState('')
    const matches = items.filter((item) =>
      item.label.toLocaleLowerCase().includes(query.toLocaleLowerCase())
    )
    return (
      <div>
        <input
          aria-label="Search apps"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        {matches.map((item) => (
          <div key={item.value} data-testid={`item-${item.value}`} data-icon={item.iconUrl ?? ''}>
            {item.label}
          </div>
        ))}
      </div>
    )
  },
}))

import { PackageSelector } from './PackageSelector'

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => {
    resolve = done
  })
  return { promise, resolve }
}

const packageEntry = (id: string): PackageInfo => ({
  bundle_id: id, label: null, apk_path: `/data/app/${id}.apk`, uid: '10001',
  version_name: null, version_code: null,
})

describe('PackageSelector eager metadata loading', () => {
  beforeEach(() => {
    callMock.mockReset()
  })

  it('loads icons in bounded batches without waiting for slow labels and prioritizes the selected app', async () => {
    const packages = Array.from({ length: 19 }, (_, i) => packageEntry(`app-${i}`))
    const firstLabels = deferred<{ labels: Record<string, string | null> }>()
    let labelCalls = 0
    callMock.mockImplementation((command: string, args: { requests?: Array<{ package: string }> }) => {
      if (command === 'list_packages') return Promise.resolve({ packages })
      if (command === 'companion_app_list') return new Promise(() => undefined)
      const values = Object.fromEntries((args.requests ?? []).map((request) => [request.package, `resolved-${request.package}`]))
      if (command === 'fetch_app_icons') return Promise.resolve({ icons: values })
      if (command === 'fetch_app_labels') return ++labelCalls === 1 ? firstLabels.promise : Promise.resolve({ labels: values })
      throw new Error(command)
    })
    render(<PackageSelector serial="device-1" value="app-18" onChange={() => undefined} />)
    await waitFor(() => expect(labelCalls).toBe(1))
    const first = callMock.mock.calls.find(([command]) => command === 'fetch_app_icons')?.[1].requests
    expect(first).toHaveLength(8)
    expect(first[0].package).toBe('app-18')
    await waitFor(() => expect(callMock.mock.calls.filter(([command]) => command === 'fetch_app_icons')).toHaveLength(3))
    expect(labelCalls).toBe(1)
    firstLabels.resolve({ labels: Object.fromEntries(first.map((request: { package: string }) => [request.package, `resolved-${request.package}`])) })
    await waitFor(() => expect(screen.getByText('resolved-app-17')).toBeInTheDocument())
    const batches = callMock.mock.calls.filter(([command]) => command === 'fetch_app_icons')
    expect(batches.map(([, args]) => args.requests.length)).toEqual([8, 8, 3])
  })

  it('continues loading searchable names while an icon batch is stalled', async () => {
    const packages = Array.from({ length: 19 }, (_, i) => packageEntry(`app-${i}`))
    const icons = deferred<{ icons: Record<string, string | null> }>()
    callMock.mockImplementation((command: string, args: { requests?: Array<{ package: string }> }) => {
      if (command === 'list_packages') return Promise.resolve({ packages })
      if (command === 'companion_app_list') return new Promise(() => undefined)
      if (command === 'fetch_app_icons') return icons.promise
      if (command === 'fetch_app_labels') return Promise.resolve({ labels: Object.fromEntries(
        (args.requests ?? []).map((request) => [request.package, `resolved-${request.package}`])
      ) })
      throw new Error(command)
    })
    render(<PackageSelector serial="device-1" value="app-0" onChange={() => undefined} />)
    await waitFor(() => expect(screen.getByText('resolved-app-18')).toBeInTheDocument())
    expect(callMock.mock.calls.filter(([command]) => command === 'fetch_app_icons')).toHaveLength(1)
    expect(callMock.mock.calls.filter(([command]) => command === 'fetch_app_labels')).toHaveLength(3)
  })

  it('shows the companion result before adb finishes and fetches 32 icons per batch', async () => {
    const packages = Array.from({ length: 65 }, (_, i) => packageEntry(`app-${i}`))
    const inventory = deferred<{ packages: typeof packages }>()
    callMock.mockImplementation((command: string, args: { packages?: string[] }) => {
      if (command === 'list_packages') return inventory.promise
      if (command === 'companion_app_list') return Promise.resolve({ apps: packages.map((pkg) => ({
        package: pkg.bundle_id, label: `Companion ${pkg.bundle_id}`, system: false, enabled: true,
      })) })
      if (command === 'companion_icons') return Promise.resolve({ icons: Object.fromEntries(
        (args.packages ?? []).map((id) => [id, `icon-${id}`])
      ) })
      throw new Error(command)
    })
    render(<PackageSelector serial="device-1" value="app-64" onChange={() => undefined} />)
    await waitFor(() => expect(screen.getByTestId('item-app-63')).toHaveAttribute('data-icon', 'data:image/png;base64,icon-app-63'))
    expect(screen.getByText('Companion app-64')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled()
    const batches = callMock.mock.calls.filter(([command]) => command === 'companion_icons')
    expect(batches.map(([, args]) => args.packages.length)).toEqual([32, 32, 1])
    expect(batches[0][1].packages[0]).toBe('app-64')
    inventory.resolve({ packages })
    await waitFor(() => expect(screen.getByText('Companion app-0')).toBeInTheDocument())
    expect(callMock.mock.calls.filter(([command]) => command === 'fetch_app_labels')).toHaveLength(0)
    expect(callMock.mock.calls.filter(([command]) => command === 'companion_icons')).toHaveLength(3)
  })

  it('uses names returned in the inventory without extracting APK labels again', async () => {
    callMock.mockImplementation((command: string) => {
      if (command === 'list_packages') return Promise.resolve({ packages: [{ ...packageEntry('cached'), label: 'Cached name' }] })
      if (command === 'companion_app_list') return new Promise(() => undefined)
      if (command === 'fetch_app_icons') return Promise.resolve({ icons: { cached: 'icon' } })
      throw new Error(command)
    })
    render(<PackageSelector serial="device-1" value="cached" onChange={() => undefined} />)
    await waitFor(() => expect(screen.getByTestId('item-cached')).toHaveAttribute('data-icon', 'data:image/png;base64,icon'))
    expect(screen.getByText('Cached name')).toBeInTheDocument()
    expect(callMock.mock.calls.filter(([command]) => command === 'fetch_app_labels')).toHaveLength(0)
  })

  it.each(['android', 'ios'] as const)('hides system apps by default and toggles their inventory on %s', async (platform) => {
    const user = { ...packageEntry('user.app'), label: 'User App' }
    const system = { ...packageEntry('system.app'), label: 'System App' }
    callMock.mockImplementation((command: string, args: { user_only?: boolean }) => {
      if (command === 'list_packages') return Promise.resolve({ packages: args.user_only ? [user] : [user, system] })
      if (command === 'companion_app_list') return Promise.resolve({ apps: [
        { package: 'user.app', label: 'User App', system: false, enabled: true },
        ...(!args.user_only ? [{ package: 'system.app', label: 'System App', system: true, enabled: true }] : []),
      ] })
      if (command === 'fetch_app_icons' || command === 'companion_icons') return Promise.resolve({ icons: {} })
      throw new Error(command)
    })
    render(<PackageSelector platform={platform} serial="device-1" value="user.app" onChange={() => undefined} />)
    const toggle = screen.getByRole('checkbox', { name: 'Show system apps' })
    expect(toggle).not.toBeChecked()
    await waitFor(() => expect(screen.getByText('User App')).toBeInTheDocument())
    expect(screen.queryByText('System App')).not.toBeInTheDocument()
    await userEvent.click(toggle)
    await waitFor(() => expect(screen.getByText('System App')).toBeInTheDocument())
    expect(toggle).toBeChecked()
    expect(callMock).toHaveBeenCalledWith('list_packages', { platform, serial: 'device-1', user_only: false })
    if (platform === 'android') expect(callMock).toHaveBeenCalledWith('companion_app_list', { serial: 'device-1', user_only: false }, 15_000)
    await userEvent.click(toggle)
    await waitFor(() => expect(screen.getByText('User App')).toBeInTheDocument())
    expect(screen.queryByText('System App')).not.toBeInTheDocument()
  })

  it('ignores a late system-app result after the checkbox has been turned off', async () => {
    const allApps = deferred<{ packages: ReturnType<typeof packageEntry>[] }>()
    const user = { ...packageEntry('user.app'), label: 'User App' }
    callMock.mockImplementation((command: string, args: { user_only?: boolean }) => {
      if (command === 'list_packages') return args.user_only ? Promise.resolve({ packages: [user] }) : allApps.promise
      if (command === 'companion_app_list') return new Promise(() => undefined)
      if (command === 'fetch_app_icons') return Promise.resolve({ icons: { 'user.app': 'icon' } })
      throw new Error(command)
    })
    render(<PackageSelector serial="device-1" value="user.app" onChange={() => undefined} />)
    await waitFor(() => expect(screen.getByText('User App')).toBeInTheDocument())
    const toggle = screen.getByRole('checkbox', { name: 'Show system apps' })
    await userEvent.click(toggle)
    await waitFor(() => expect(callMock).toHaveBeenCalledWith('list_packages', { platform: 'android', serial: 'device-1', user_only: false }))
    await userEvent.click(toggle)
    await waitFor(() => expect(screen.getByText('User App')).toBeInTheDocument())
    allApps.resolve({ packages: [user, packageEntry('system.app')] })
    await Promise.resolve()
    expect(screen.queryByTestId('item-system.app')).not.toBeInTheDocument()
  })

  it('refreshes a returning serial, retries missing metadata and preserves successful cache and selection', async () => {
    const packages = [packageEntry('good'), packageEntry('missing')]
    let connected = false
    const onChange = vi.fn()
    callMock.mockImplementation((command: string, args: { requests?: Array<{ package: string }> }) => {
      if (command === 'list_packages') return Promise.resolve({ packages })
      if (command === 'companion_app_list') return new Promise(() => undefined)
      const values = Object.fromEntries((args.requests ?? []).map((request) => [request.package,
        request.package === 'good' || connected ? `resolved-${request.package}` : null]))
      if (command === 'fetch_app_icons') return Promise.resolve({ icons: values })
      if (command === 'fetch_app_labels') return Promise.resolve({ labels: values })
      throw new Error(command)
    })
    const view = render(<PackageSelector serial="device-1" value="good" onChange={onChange} connectionRevision={0} />)
    await waitFor(() => expect(screen.getByText('resolved-good')).toBeInTheDocument())
    connected = true
    view.rerender(<PackageSelector serial="device-1" value="good" onChange={onChange} connectionRevision={1} />)
    await waitFor(() => expect(screen.getByText('resolved-missing')).toBeInTheDocument())
    const iconCalls = callMock.mock.calls.filter(([command]) => command === 'fetch_app_icons')
    expect(iconCalls[1][1].requests.map((request: { package: string }) => request.package)).toEqual(['missing'])
    expect(onChange).not.toHaveBeenCalled()
  })

  it('automatically retries a transient missing result once without opening the menu', async () => {
    let attempts = 0
    callMock.mockImplementation((command: string) => {
      if (command === 'list_packages') return Promise.resolve({ packages: [packageEntry('retry')] })
      if (command === 'companion_app_list') return new Promise(() => undefined)
      if (command === 'fetch_app_icons') return Promise.resolve({ icons: { retry: 'icon' } })
      if (command === 'fetch_app_labels') return Promise.resolve({ labels: { retry: ++attempts === 1 ? null : 'Recovered name' } })
      throw new Error(command)
    })
    render(<PackageSelector serial="device-1" value="retry" onChange={() => undefined} />)
    await waitFor(() => expect(screen.getByText('Recovered name')).toBeInTheDocument(), { timeout: 3500 })
    expect(attempts).toBe(2)
    expect(callMock.mock.calls.filter(([command]) => command === 'fetch_app_icons')).toHaveLength(1)
  })

  it('falls back to APK labels when a companion list only returns the package identifier', async () => {
    callMock.mockImplementation((command: string) => {
      if (command === 'list_packages') return Promise.resolve({ packages: [packageEntry('unlabelled')] })
      if (command === 'companion_app_list') return Promise.resolve({ apps: [{ package: 'unlabelled', label: 'unlabelled', system: false, enabled: true }] })
      if (command === 'companion_icons') return Promise.resolve({ icons: { unlabelled: 'icon' } })
      if (command === 'fetch_app_labels') return Promise.resolve({ labels: { unlabelled: 'Friendly name' } })
      throw new Error(command)
    })
    render(<PackageSelector serial="device-1" value="unlabelled" onChange={() => undefined} />)
    await waitFor(() => expect(screen.getByText('Friendly name')).toBeInTheDocument())
  })

  it('bulk-loads every app without visibility callbacks and makes resolved names searchable', async () => {
    const packages = [
      {
        bundle_id: 'com.example.selected',
        label: null,
        apk_path: '/data/app/selected.apk',
        uid: '10001',
        version_name: null,
        version_code: null,
      },
      {
        bundle_id: 'com.example.offscreen',
        label: null,
        apk_path: '/data/app/offscreen.apk',
        uid: '10002',
        version_name: null,
        version_code: null,
      },
    ]

    callMock.mockImplementation((command: string) => {
      if (command === 'list_packages') return Promise.resolve({ packages })
      if (command === 'companion_app_list') return new Promise(() => undefined)
      if (command === 'fetch_app_icons') {
        return Promise.resolve({
          icons: {
            'com.example.selected': null,
            'com.example.offscreen': 'offscreen-icon',
          },
        })
      }
      if (command === 'fetch_app_labels') {
        return Promise.resolve({
          labels: {
            'com.example.selected': 'Selected App',
            'com.example.offscreen': 'Hidden Friendly App',
          },
        })
      }
      throw new Error(`Unexpected command: ${command}`)
    })

    render(
      <PackageSelector
        platform="android"
        serial="device-1"
        value="com.example.selected"
        onChange={() => undefined}
      />
    )

    await waitFor(() => {
      expect(callMock).toHaveBeenCalledWith(
        'fetch_app_labels',
        {
          platform: 'android',
          serial: 'device-1',
          requests: packages.map((pkg) => ({
            package: pkg.bundle_id,
            apk_path: pkg.apk_path,
          })),
        }
      )
    })
    expect(callMock).toHaveBeenCalledWith(
      'fetch_app_icons',
      {
        platform: 'android',
        serial: 'device-1',
        requests: packages.map((pkg) => ({
          package: pkg.bundle_id,
          apk_path: pkg.apk_path,
        })),
      }
    )

    const search = screen.getByRole('textbox', { name: 'Search apps' })
    await waitFor(() => expect(screen.getByText('Hidden Friendly App')).toBeInTheDocument())
    await userEvent.type(search, 'Hidden Friendly')

    expect(screen.getByText('Hidden Friendly App')).toBeInTheDocument()
    expect(screen.queryByText('Selected App')).toBeNull()
  })

  it('restarts icon loading through the companion when it enriches an in-flight adb list', async () => {
    const companion = deferred<{
      apps: Array<{
        package: string
        label: string
        system: boolean
        enabled: boolean
      }>
    }>()
    const adbIcons = deferred<{ icons: Record<string, string | null> }>()
    const pkg = {
      bundle_id: 'com.example.app',
      label: null,
      apk_path: '/data/app/app.apk',
      uid: '10001',
      version_name: null,
      version_code: null,
    }

    callMock.mockImplementation((command: string) => {
      if (command === 'list_packages') return Promise.resolve({ packages: [pkg] })
      if (command === 'companion_app_list') return companion.promise
      if (command === 'fetch_app_icons') return adbIcons.promise
      if (command === 'fetch_app_labels') return Promise.resolve({ labels: {} })
      if (command === 'companion_icons') {
        return Promise.resolve({ icons: { 'com.example.app': 'companion-icon' } })
      }
      throw new Error(`Unexpected command: ${command}`)
    })

    render(
      <PackageSelector
        platform="android"
        serial="device-1"
        value="com.example.app"
        onChange={() => undefined}
      />
    )

    await waitFor(() =>
      expect(callMock.mock.calls.some(([command]) => command === 'fetch_app_icons')).toBe(true)
    )
    companion.resolve({
      apps: [{
        package: 'com.example.app',
        label: 'Companion App',
        system: false,
        enabled: true,
      }],
    })

    await waitFor(() => {
      expect(callMock).toHaveBeenCalledWith('companion_icons', {
        serial: 'device-1',
        packages: ['com.example.app'],
      })
    })
    expect(screen.getByTestId('item-com.example.app')).toHaveAttribute(
      'data-icon',
      'data:image/png;base64,companion-icon'
    )

    adbIcons.resolve({ icons: { 'com.example.app': null } })
    await Promise.resolve()
    expect(screen.getByTestId('item-com.example.app')).toHaveAttribute(
      'data-icon',
      'data:image/png;base64,companion-icon'
    )
  })
})
