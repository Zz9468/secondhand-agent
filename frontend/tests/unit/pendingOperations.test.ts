import { expect, test } from 'vitest'
import { createOperation, lookupPath, operationPath, OPERATION_LIMIT, OPERATION_TTL,
  PendingOperationStore, storageKey } from '../../src/state/pendingOperations'

function fixture() {
  const values = new Map<string, string>()
  const storage = { getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value) },
    removeItem: (key: string) => { values.delete(key) } }
  return { values, storage, store: new PendingOperationStore(() => storage, () => 1000) }
}
const message = () => createOperation('account-a', '1', 'message', {
  request_id: 'original-request', content: '900自提', client_view_id: 'original-view',
  display_receipt_id: 'original-receipt', draft_id: 'draft', draft_version: 2,
}, 1000)

test('R01 R05 refresh retains complete original payload, view, draft and receipt', () => {
  const { store } = fixture(), op = message()
  store.save(op)
  expect(store.read('account-a').records).toEqual([op])
  expect(lookupPath(op)).toBe('/api/negotiations/1/requests/original-request')
  expect(operationPath(op)).toBe('/api/negotiations/1/messages')
})
test('R06 keys and validation isolate accounts; logout clears only its account', () => {
  const { store, values } = fixture(), op = message()
  store.save(op)
  expect(store.read('account-b').records).toEqual([])
  values.set(storageKey('account-b'), JSON.stringify([op]))
  expect(store.read('account-b').records).toEqual([])
  expect(store.read('account-b').warning).not.toBe('')
  store.clear('account-a')
  expect(store.read('account-a').records).toEqual([])
})
test.each(['{broken', '{}', '[null]', JSON.stringify([{ ...message(), version: 9 }]),
  JSON.stringify([{ ...message(), createdAt: 1000 - OPERATION_TTL - 1 }]),
  JSON.stringify([{ ...message(), payload: { ...message().payload, injected: 'secret' } }]),
])('R07 damaged or expired cache cannot prevent history loading: %s', value => {
  const { store, values } = fixture()
  values.set(storageKey('account-a'), value)
  expect(store.read('account-a').records).toEqual([])
  expect(store.read('account-a').warning).not.toBe('')
})
test('R07 storage failures are explicit; accepted metadata contains no original content', () => {
  const { store, storage, values } = fixture(), op = message()
  store.save(op)
  const accepted = { ...op, status: 'accepted' as const, payload: undefined, runId: 'known-run' }
  store.save(accepted)
  expect(values.get(storageKey('account-a'))).not.toContain('900自提')
  expect(values.get(storageKey('account-a'))).not.toContain('original-receipt')
  expect(lookupPath(accepted)).toBe('/api/negotiations/1/runs/known-run')
  storage.setItem = () => { throw Error('quota') }
  expect(() => store.save(op)).toThrow('本次未发送')
})
test('R07 capacity never silently evicts unresolved identities', () => {
  const { store } = fixture()
  for (let i = 0; i < OPERATION_LIMIT; i++) {
    store.save(createOperation('account-a', '1', 'close', { request_id: `original-${i}`, client_view_id: 'view' }, 1000))
  }
  expect(() => store.save(message())).toThrow('待核对操作过多')
  expect(store.read('account-a').records).toHaveLength(OPERATION_LIMIT)
})
test('R04 blocked retry keeps a queryable identity across refresh without replay payload', () => {
  const { store } = fixture()
  const blocked = { ...message(), payload: undefined, status: 'blocked' as const }
  store.save(blocked)
  expect(store.read('account-a').records).toEqual([blocked])
  expect(lookupPath(blocked)).toBe('/api/negotiations/1/requests/original-request')
})
test('R11 seller targets are encoded and full reasons/revisions survive', () => {
  const { store } = fixture()
  const recovery = createOperation('account-a', 'turn:one/reply', 'recovery', { request_id: 'retry-original', action: 'RETRY', reason: '原因' }, 1000)
  const approval = createOperation('account-a', '23', 'approve', { request_id: 'approve-original', comment: null, expected_effect_revision: 0 }, 1000)
  store.save(recovery); store.save(approval)
  expect(store.read('account-a').records).toEqual([recovery, approval])
  expect(lookupPath(recovery)).toContain('turn%3Aone%2Freply/requests/retry-original')
  expect(lookupPath(approval)).toBe('/api/seller/approvals/23/requests/approve-original')
})
