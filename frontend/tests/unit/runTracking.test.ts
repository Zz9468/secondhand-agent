import { expect, test } from 'vitest'
import type { RunStatus } from '../../src/api/negotiations'
import { runOutcome, shouldPollRun } from '../../src/state/runTracking'

function run(overrides: Partial<RunStatus> = {}): RunStatus {
  return { run_id: 'one', input_seq: 1, task_status: 'PENDING', business_status: 'NOT_COMMITTED',
    business_outcome: null, notification_status: 'NOT_REQUIRED', blocked_by_input_seq: null,
    public_error_category: null, recovery_required: false, ...overrides }
}

test('R10 early NOT_REQUIRED still needs polling', () => {
  expect(shouldPollRun(run())).toBe(true)
})
test('R10 published terminal run can stop polling', () => {
  expect(shouldPollRun(run({ task_status: 'SUCCEEDED', notification_status: 'PUBLISHED' }))).toBe(false)
})
test.fails('D0 expected failure R10: successful task with pending delivery must keep polling', () => {
  expect(shouldPollRun(run({ task_status: 'SUCCEEDED', business_status: 'COMMITTED', notification_status: 'PENDING' }))).toBe(true)
})
test.fails('D0 expected failure R10: manual notification recovery must remain observable', () => {
  expect(shouldPollRun(run({ task_status: 'SUCCEEDED', notification_status: 'MANUAL_REQUIRED' }))).toBe(true)
})
test('R09 blocked input identifies the FIFO head', () => {
  expect(runOutcome(run({ blocked_by_input_seq: 2 }))).toBe('等待前序输入 #2')
})
test('R13 recovery preserves the original input in the status message', () => {
  expect(runOutcome(run({ task_status: 'FAILED', recovery_required: true }))).toContain('原输入已保留')
})
