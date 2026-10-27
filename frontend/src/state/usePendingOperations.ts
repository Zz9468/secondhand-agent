import { computed, onUnmounted, ref, watch } from 'vue'
import { ApiError, requestJson } from '../api/client'
import { useAuth } from './auth'
import { rejectedOperationMessage } from './operationFeedback'
import { createOperation, lookupPath, operationPath, pendingOperationStore,
  type OperationKind, type OperationPayload, type PendingOperation } from './pendingOperations'

export type OperationResult = Record<string, unknown>

export function usePendingOperations(scope: () => string,
  onAccepted: (op: PendingOperation, result: OperationResult) => void | Promise<void>) {
  const auth = useAuth()
  const operations = ref<PendingOperation[]>([])
  const notice = ref('')
  const operationError = ref('')
  const operationBusy = ref(false)
  let generation = 0
  let allowNew = false
  const matches = (op: PendingOperation) => scope() === 'recovery' ? op.kind === 'recovery'
    : scope() === 'approval' ? ['approve', 'reject'].includes(op.kind)
    : ['message', 'confirm', 'close'].includes(op.kind) && op.target === scope()
  const unresolved = computed(() => operations.value.some(op => op.status === 'unknown' || op.status === 'blocked'))
  function guard() {
    const g = generation, authVersion = auth.version.value, account = auth.currentUser.value?.id
    return () => g === generation && authVersion === auth.version.value && !!account && account === auth.currentUser.value?.id
  }
  function update(op: PendingOperation) {
    operations.value = [...operations.value.filter(o => o.requestId !== op.requestId), op]
  }
  function accept(op: PendingOperation, result: OperationResult) {
    if (notice.value.startsWith('暂未查到')) notice.value = ''
    const accepted: PendingOperation = { ...op, status: 'accepted', payload: undefined,
      ...(typeof result.run_id === 'string' ? { runId: result.run_id } : {}) }
    update(accepted)
    try { pendingOperationStore.save(accepted) }
    catch { notice.value = '服务端已接受原操作，但本地记录更新失败；刷新后请再次核对，勿重复发起。' }
    return onAccepted(accepted, result)
  }
  async function lookup(op: PendingOperation, alive: () => boolean): Promise<boolean> {
    try {
      const result = await requestJson<OperationResult>(lookupPath(op))
      if (!alive()) return true
      await accept(op, result)
      return true
    } catch (e) {
      if (!alive()) return true
      if (e instanceof ApiError && e.status === 404) {
        notice.value = op.status === 'blocked'
          ? '暂未查到原请求；原提交可能仍在途中。此次重试已被拒绝，请继续核对原操作或服务端历史。'
          : '暂未查到原请求；它可能仍在途中。可核对或使用原编号和原始内容重试。'
        return false
      }
      throw e
    }
  }
  async function check(op: PendingOperation) {
    if (operationBusy.value) return
    const alive = guard()
    operationBusy.value = true; operationError.value = ''
    try { await lookup(op, alive) }
    catch { if (alive()) operationError.value = '暂时无法核对原操作，结果仍待核实。' }
    finally { if (alive()) operationBusy.value = false }
  }
  async function transmit(op: PendingOperation, retry: boolean) {
    if (operationBusy.value) return
    const alive = guard()
    if (!alive() || op.account !== auth.currentUser.value?.id || op.status !== 'unknown' || !op.payload) return
    operationBusy.value = true; operationError.value = ''; notice.value = ''
    try {
      // A missing result is not evidence of failure. Replays always use the saved body.
      if (retry && await lookup(op, alive)) return
      if (!alive()) return
      pendingOperationStore.save(op) // must succeed before any POST
      update(op)
      let result: OperationResult
      try {
        result = await requestJson<OperationResult>(operationPath(op), { method: 'POST', body: JSON.stringify(op.payload) })
      } catch (e) {
        if (!alive()) return
        if (e instanceof ApiError && [400, 403, 404, 409, 422, 429].includes(e.status)) {
          // Conflicts can belong to an already accepted request: consult its receipt first.
          if (e.status === 409 && await lookup(op, alive)) return
          if (!alive()) return
          // Rejecting this retry cannot prove an earlier in-flight POST failed.
          const rejected: PendingOperation = { ...op, payload: undefined, status: retry ? 'blocked' : 'rejected' }
          update(rejected)
          if (!retry && notice.value.startsWith('暂未查到')) notice.value = ''
          try { pendingOperationStore.save(rejected) } catch { notice.value = '本地记录更新失败；刷新后请重新核对。' }
          operationError.value = rejectedOperationMessage(e)
        } else {
          operationError.value = '提交结果待核实，原操作已保存。请核对原操作或显式重试。'
        }
        return
      }
      if (!alive()) return
      // Cache or subsequent history read errors must not relabel an accepted POST as failed.
      try { await accept(op, result) }
      catch { if (alive()) notice.value = '服务端已接受原操作；页面更新暂时失败，请核对原操作。' }
    } catch (e) {
      if (alive()) operationError.value = e instanceof Error ? e.message : '无法核对原操作，暂未重试。'
    } finally { if (alive()) operationBusy.value = false }
  }
  async function begin(target: string, kind: OperationKind, payload: OperationPayload) {
    if (operationBusy.value) return
    if (unresolved.value && !allowNew) {
      operationError.value = '还有结果待核实的原操作。请先核对；若这是不同意图，请明确选择另行发起新操作。'
      return
    }
    const account = auth.currentUser.value?.id
    if (!account) return
    allowNew = false
    return transmit(createOperation(account, target, kind, payload), false)
  }
  function newIntent() { allowNew = true; notice.value = '下一次提交将作为新操作，原操作仍保留核对入口。' }
  async function restore() {
    generation++; operationBusy.value = false; allowNew = false
    operations.value = []; notice.value = ''; operationError.value = ''
    const account = auth.currentUser.value?.id
    if (!account) return
    const saved = pendingOperationStore.read(account)
    operations.value = saved.records.filter(matches)
    notice.value = saved.warning
    const alive = guard()
    operationBusy.value = true
    for (const op of [...operations.value]) {
      if (!alive()) return
      if (op.status === 'rejected') continue
      try { await lookup(op, alive) }
      catch { if (alive()) operationError.value = '暂时无法核对原操作，结果仍待核实。' }
    }
    if (alive()) operationBusy.value = false
  }
  watch([scope, auth.version], () => { void restore() }, { immediate: true, flush: 'sync' })
  onUnmounted(() => { generation++ })
  return { operations, notice, operationError, operationBusy, begin, check,
    retry: (op: PendingOperation) => transmit(op, true), newIntent }
}
