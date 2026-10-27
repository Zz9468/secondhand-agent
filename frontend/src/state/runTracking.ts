import type { NegotiationStatus, RunStatus } from '../api/negotiations'

export function shouldPollRun(run: RunStatus): boolean {
  // InputQueue.finalize requires a published reply, including no-business paths.
  // NOT_REQUIRED means no delivery row yet, not a waiver of that obligation.
  return run.task_status !== 'SUCCEEDED' || run.notification_status !== 'PUBLISHED'
    || run.recovery_required || run.blocked_by_input_seq !== null
}

export function needsManualRecovery(run: RunStatus): boolean {
  return run.recovery_required || ['FAILED', 'STALE', 'CANCELLED'].includes(run.task_status)
    || run.notification_status === 'MANUAL_REQUIRED'
}

export function runPollDelay(runs: RunStatus[]): number {
  const pending = runs.filter(shouldPollRun)
  return pending.length && pending.every(needsManualRecovery) ? 10_000 : 2_000
}

export function runOutcome(run: RunStatus, sessionStatus?: NegotiationStatus): string {
  const names: Record<string, string> = { OFFER_ACCEPTED: '报价已接受，待最终确认', WAITING_APPROVAL: '等待卖家审批',
    OFFER_REJECTED: '报价未被接受', INTENT_CONFIRMED: '交易意向已记录', OFFER_WITHDRAWN: '报价已撤回', SESSION_CLOSED: '协商已结束' }
  const manual = needsManualRecovery(run)
  if (run.business_status === 'COMMITTED') {
    let result = names[run.business_outcome ?? ''] ?? '业务结果已记录'
    if (run.business_outcome === 'WAITING_APPROVAL' && sessionStatus && sessionStatus !== 'WAITING_APPROVAL') {
      result = sessionStatus === 'AGREED' ? '交易意向已记录'
        : sessionStatus === 'CLOSED' ? '协商已结束' : '卖家审批状态已更新，请查看最新消息'
    }
    if (manual) return `${result}；需要卖家恢复，原输入已保留`
    if (run.notification_status !== 'PUBLISHED') return `${result}；结果通知恢复中`
    if (shouldPollRun(run)) return `${result}；结果已通知，处理收尾中`
    return result
  }
  if (manual) return '处理需要卖家恢复，原输入已保留'
  if (run.blocked_by_input_seq) return `等待前序输入 #${run.blocked_by_input_seq}`
  if (run.task_status === 'RETRY_WAIT') return '暂时失败，等待后台重试'
  if (run.task_status === 'PENDING') return '已排队，等待处理'
  if (!shouldPollRun(run)) return '已回复，未提交业务动作'
  if (run.notification_status !== 'NOT_REQUIRED' || run.task_status === 'SUCCEEDED') return '回复交付中'
  return '正在处理'
}
