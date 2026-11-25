import { beforeEach, describe, expect, it } from 'vitest'
import { getEndpointUrl, getSendOnSave } from './endpointSettings'

describe('endpoint settings namespace migration', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('migrates and removes a cleared legacy endpoint', () => {
    localStorage.setItem('perfinspect.endpoint.url.v1', '')

    expect(getEndpointUrl()).toBe('')
    expect(localStorage.getItem('mpi.endpoint.url.v1')).toBe('')
    expect(localStorage.getItem('perfinspect.endpoint.url.v1')).toBeNull()
  })

  it('preserves the legacy send-on-save behavior for a configured URL', () => {
    localStorage.setItem('perfinspect.endpoint.url.v1', ' https://example.test/upload ')

    expect(getSendOnSave()).toBe(true)
    expect(localStorage.getItem('mpi.endpoint.url.v1')).toBe(
      ' https://example.test/upload '
    )
    expect(localStorage.getItem('mpi.endpoint.sendOnSave.v1')).toBe('1')
    expect(localStorage.getItem('perfinspect.endpoint.url.v1')).toBeNull()
  })
})
