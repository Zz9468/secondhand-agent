import { expect, test } from 'vitest'
import type { RunStatus } from '../../src/api/negotiations'
import { runOutcome, runPollDelay, shouldPollRun } from '../../src/state/runTracking'

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
test('R10 successful task with pending delivery must keep polling', () => {
  expect(shouldPollRun(run({ task_status: 'SUCCEEDED', business_status: 'COMMITTED', notification_status: 'PENDING' }))).toBe(true)
})
test('R10 manual notification recovery must remain observable', () => {
  expect(shouldPollRun(run({ task_status: 'SUCCEEDED', notification_status: 'MANUAL_REQUIRED' }))).toBe(true)
})

test.each(['NOT_REQUIRED', 'PENDING', 'RUNNING', 'RETRY_WAIT', 'MANUAL_REQUIRED'])('R10 SUCCEEDED plus %s cannot prove delivery completion', notification_status => {
  expect(shouldPollRun(run({ task_status: 'SUCCEEDED', notification_status }))).toBe(true)
})

test('R10 publication before graph finalization still needs tracking', () => {
  expect(shouldPollRun(run({ task_status: 'RETRY_WAIT', notification_status: 'PUBLISHED' }))).toBe(true)
  expect(runOutcome(run({ business_status: 'COMMITTED', notification_status: 'PUBLISHED' }))).toContain('收尾中')
})

test('R09 execution, automatic retry and seller recovery have separate labels', () => {
  expect(runOutcome(run())).toContain('已排队')
  expect(runOutcome(run({ task_status: 'RUNNING' }))).toBe('正在处理')
  expect(runOutcome(run({ task_status: 'RETRY_WAIT' }))).toContain('后台重试')
  expect(runOutcome(run({ business_status: 'COMMITTED', notification_status: 'MANUAL_REQUIRED' }))).toContain('需要卖家恢复')
})

test('R10 approval receipt is historical while session continues changing', () => {
  const waiting = run({ task_status: 'SUCCEEDED', business_status: 'COMMITTED', business_outcome: 'WAITING_APPROVAL', notification_status: 'PUBLISHED' })
  expect(shouldPollRun(waiting)).toBe(false)
  expect(runOutcome(waiting, 'WAITING_APPROVAL')).toBe('等待卖家审批')
  expect(runOutcome(waiting, 'ACTIVE')).toContain('审批状态已更新')
  expect(runOutcome(waiting, 'AGREED')).toBe('交易意向已记录')
  expect(runOutcome(waiting, 'CLOSED')).toBe('协商已结束')
})

test('R13 manual recovery uses low frequency but never slows an active input', () => {
  const failed = run({ task_status: 'FAILED', recovery_required: true })
  expect(runPollDelay([failed])).toBe(10_000)
  expect(runPollDelay([failed, run()])).toBe(2_000)
  expect(runPollDelay([])).toBe(2_000)
})
test('R09 blocked input identifies the FIFO head', () => {
  expect(runOutcome(run({ blocked_by_input_seq: 2 }))).toBe('等待前序输入 #2')
})
test('R13 recovery preserves the original input in the status message', () => {
  expect(runOutcome(run({ task_status: 'FAILED', recovery_required: true }))).toContain('原输入已保留')
})
