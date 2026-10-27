import type { RunStatus } from '../api/negotiations'

export function shouldPollRun(run: RunStatus): boolean {
  return run.task_status !== 'SUCCEEDED'
}

export function runOutcome(run: RunStatus): string {
  const names: Record<string, string> = { OFFER_ACCEPTED: '报价已接受，待最终确认', WAITING_APPROVAL: '等待卖家审批',
    OFFER_REJECTED: '报价未被接受', INTENT_CONFIRMED: '交易意向已记录', OFFER_WITHDRAWN: '报价已撤回', SESSION_CLOSED: '协商已结束' }
  if (run.business_status === 'COMMITTED') return `${names[run.business_outcome ?? ''] ?? '业务结果已记录'}${run.notification_status !== 'PUBLISHED' ? '；结果通知恢复中' : ''}`
  if (run.recovery_required || run.task_status === 'FAILED') return '处理需要恢复，原输入已保留'
  if (run.blocked_by_input_seq) return `等待前序输入 #${run.blocked_by_input_seq}`
  return run.task_status === 'SUCCEEDED' ? '已回复，未提交业务动作' : '正在处理'
}
