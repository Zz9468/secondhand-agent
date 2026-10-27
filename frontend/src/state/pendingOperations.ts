// Session-local recovery hints. Only an owned server response proves acceptance.
export type OperationKind = 'message' | 'confirm' | 'close' | 'recovery' | 'approve' | 'reject'
export type OperationPayload = Record<string, string | number | null>
export interface PendingOperation {
  version: 1
  account: string
  target: string
  kind: OperationKind
  requestId: string
  createdAt: number
  status: 'unknown' | 'accepted' | 'rejected' | 'blocked'
  payload?: OperationPayload
  runId?: string
}
export const OPERATION_TTL = 24 * 60 * 60 * 1000
export const OPERATION_LIMIT = 100
const PREFIX = 'secondhand-operations:v1:'
const INVALID_CACHE = '部分本地恢复记录已损坏或过期；不能据此认定旧操作失败，请先核对服务端历史。'
export const storageKey = (account: string) => `${PREFIX}${encodeURIComponent(account)}`
const isObject = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)
const str = (v: unknown, max = 200) => typeof v === 'string' && v.length > 0 && v.length <= max
const integer = (v: unknown) => Number.isSafeInteger(v) && Number(v) > 0
const fields: Record<OperationKind, string[]> = {
  message: ['request_id', 'content', 'client_view_id', 'display_receipt_id', 'draft_id', 'draft_version', 'reply_to_message_id'],
  confirm: ['request_id', 'offer_id', 'client_view_id', 'display_receipt_id'],
  close: ['request_id', 'client_view_id'],
  recovery: ['request_id', 'action', 'reason'],
  approve: ['request_id', 'comment', 'expected_effect_revision'],
  reject: ['request_id', 'comment', 'expected_effect_revision'],
}

function validPayload(kind: OperationKind, p: unknown, request: string): p is OperationPayload {
  if (!isObject(p) || p.request_id !== request || Object.keys(p).some(k => !fields[kind].includes(k))) return false
  if (kind === 'recovery') return ['RETRY', 'TERMINATE', 'NOTIFY'].includes(String(p.action)) && str(p.reason, 500)
  if (kind === 'approve' || kind === 'reject') return (p.comment === null || str(p.comment, 2000))
    && (p.expected_effect_revision === undefined || (Number.isSafeInteger(p.expected_effect_revision) && Number(p.expected_effect_revision) >= 0))
  if (!str(p.client_view_id)) return false
  if (kind === 'close') return true
  if (kind === 'confirm') return integer(p.offer_id) && str(p.display_receipt_id)
  return str(p.content, 4000) && (p.display_receipt_id == null || str(p.display_receipt_id))
    && (p.draft_id === undefined ? p.draft_version === undefined : str(p.draft_id) && integer(p.draft_version))
    && (p.reply_to_message_id === undefined || integer(p.reply_to_message_id))
}

function validRecord(v: unknown, account: string, now: number): v is PendingOperation {
  if (!isObject(v) || v.version !== 1 || v.account !== account || !str(v.target) || !str(v.requestId, 64)
      || !Object.hasOwn(fields, String(v.kind)) || !['unknown', 'accepted', 'rejected', 'blocked'].includes(String(v.status))
      || typeof v.createdAt !== 'number' || !Number.isFinite(v.createdAt)
      || v.createdAt > now || now - v.createdAt > OPERATION_TTL) return false
  const kind = v.kind as OperationKind
  if (kind !== 'recovery' && !/^[1-9]\d*$/.test(String(v.target))) return false
  if (v.runId !== undefined && !str(v.runId)) return false
  if (v.status === 'accepted' && ['message', 'confirm', 'close'].includes(kind) && !str(v.runId)) return false
  return v.status === 'unknown' ? validPayload(kind, v.payload, String(v.requestId)) : v.payload === undefined
}

export function operationPath(op: PendingOperation): string {
  const target = encodeURIComponent(op.target)
  if (op.kind === 'recovery') return `/api/seller/graph-recovery/${target}`
  if (op.kind === 'approve' || op.kind === 'reject') return `/api/seller/approvals/${target}/${op.kind}`
  return `/api/negotiations/${target}/${op.kind === 'message' ? 'messages' : op.kind}`
}
export function lookupPath(op: PendingOperation): string {
  const target = encodeURIComponent(op.target), request = encodeURIComponent(op.requestId)
  if (op.kind === 'recovery') return `/api/seller/graph-recovery/${target}/requests/${request}`
  if (op.kind === 'approve' || op.kind === 'reject') return `/api/seller/approvals/${target}/requests/${request}`
  return `/api/negotiations/${target}/${op.runId ? `runs/${encodeURIComponent(op.runId)}` : `requests/${request}`}`
}

export class PendingOperationStore {
  constructor(private storage: () => Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>,
    private now: () => number = Date.now) {}

  read(account: string): { records: PendingOperation[]; warning: string } {
    try {
      const raw = this.storage().getItem(storageKey(account))
      if (!raw) return { records: [], warning: '' }
      if (raw.length > 1_000_000) return { records: [], warning: INVALID_CACHE }
      const parsed: unknown = JSON.parse(raw)
      if (!Array.isArray(parsed)) return { records: [], warning: INVALID_CACHE }
      const seen = new Set<string>()
      const records = parsed.filter((v): v is PendingOperation => {
        if (!validRecord(v, account, this.now()) || seen.has(v.requestId)) return false
        seen.add(v.requestId); return true
      }).slice(-OPERATION_LIMIT)
      return { records, warning: records.length !== parsed.length ? INVALID_CACHE : '' }
    } catch { return { records: [], warning: '本地恢复存储不可读；请核对服务端历史，不能据此认定旧操作失败。' } }
  }

  save(op: PendingOperation): void {
    if (!validRecord(op, op.account, this.now())) throw new Error('原操作记录无效或已过期；请先核对服务端历史。')
    const { records } = this.read(op.account)
    const next = records.filter(r => r.requestId !== op.requestId)
    // Never evict an unresolved request to make room for a new one.
    while (next.length >= OPERATION_LIMIT) {
      const index = next.findIndex(r => r.status === 'accepted' || r.status === 'rejected')
      if (index < 0) throw new Error('待核对操作过多，请先核对原操作。')
      next.splice(index, 1)
    }
    next.push(op)
    try {
      this.storage().setItem(storageKey(op.account), JSON.stringify(next))
    } catch { throw new Error('本地恢复存储不可用，本次未发送。请恢复存储后重试。') }
  }

  clear(account: string): void {
    try { this.storage().removeItem(storageKey(account)) } catch { /* Account keys still isolate data. */ }
  }
}

export const pendingOperationStore = new PendingOperationStore(() => sessionStorage)

export function createOperation(account: string, target: string, kind: OperationKind,
  payload: OperationPayload, now = Date.now()): PendingOperation {
  return { version: 1, account, target, kind, requestId: String(payload.request_id),
    payload: structuredClone(payload), status: 'unknown', createdAt: now }
}
