import { expect, it } from 'vitest'
import { decodeTeamDetail, decodeTeamPage } from './team-workspace'
const viewer = '11111111-1111-4111-8111-111111111111'
const team = '22222222-2222-4222-8222-222222222222'
const second = '33333333-3333-4333-8333-333333333333'
const firstRecord = '44444444-4444-4444-8444-444444444444'
const secondRecord = '55555555-5555-4555-8555-555555555555'

it('preserves separate record identities for two members and rejects another viewer or duplicated record', () => {
  const payload = { name: 'A session', time_series: [42] }
  const a = { record_id: firstRecord, owner_id: viewer, source: 'desktop', owner_label: 'First', payload }
  const b = { record_id: secondRecord, owner_id: second, source: 'android_companion', owner_label: 'Second', payload }
  const page = { contract_version: 1, viewer_id: viewer, team_id: team, items: [a, b], next: null }
  expect(decodeTeamPage(page, viewer)?.items.map(row => [row.id, row.ownerId])).toEqual([[firstRecord, viewer], [secondRecord, second]])
  expect(decodeTeamPage(page, second)).toBeNull()
  expect(decodeTeamPage({ ...page, items: [a, { ...b, record_id: firstRecord }] }, viewer)).toBeNull()
  expect(decodeTeamPage(page, viewer)?.items[0]).not.toHaveProperty('time_series')
})

it('accepts details only for the same viewer, team, owner, and record', () => {
  const row = { id: firstRecord, ownerId: second, source: 'desktop' as const, ownerLabel: null, title: 'Original' }
  const value = { contract_version: 1, viewer_id: viewer, team_id: team, record_id: firstRecord, owner_id: second,
    source: 'desktop', payload: { metadata: { label: 'Practice', device_name: 'Phone' }, duration_sec: 90, summary: { averages: { fps: 59.5 } } } }
  expect(decodeTeamDetail(value, viewer, team, row)).toEqual({ title: 'Practice', durationSec: 90, averageFps: 59.5, device: 'Phone' })
  expect(decodeTeamDetail({ ...value, team_id: second }, viewer, team, row)).toBeNull()
  expect(decodeTeamDetail({ ...value, owner_id: viewer }, viewer, team, row)).toBeNull()
  expect(decodeTeamDetail({ ...value, record_id: secondRecord }, viewer, team, row)).toBeNull()
})
