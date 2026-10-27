<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import PendingOperations from '../components/PendingOperations.vue'
import DemoNotice from '../components/DemoNotice.vue'
import { useAuth } from '../state/auth'
import { usePendingOperations } from '../state/usePendingOperations'
import { needsManualRecovery, runOutcome as outcome, runPollDelay, shouldPollRun } from '../state/runTracking'
import { createPolling } from '../state/polling'
import {
  acknowledgeDisplay, getMessages, getNegotiation,
  getRun, refreshContext,
  type ChatMessage, type DraftOption, type NegotiationDetail,
  type PublicCard, type RunStatus,
} from '../api/negotiations'

const route = useRoute()
const sid = computed(() => Number(route.params.sessionId))
let viewId = crypto.randomUUID()
const auth = useAuth()
let pageGeneration = 0
function pageGuard() {
  const generation = pageGeneration, version = auth.version.value
  return () => generation === pageGeneration && version === auth.version.value
}
const detail = ref<NegotiationDetail | null>(null)
const messages = ref<ChatMessage[]>([])
const runs = ref<Record<string, RunStatus>>({})
const receipts = ref<Record<string, string>>({})
const text = ref('')
// A matching string is insufficient: editing and restoring the same text is a new draft.
let draftRevision = 0
const submittedDrafts = new Map<string, number>()
watch(text, () => { draftRevision++ }, { flush: 'sync' })
const error = ref('')
const busy = ref(false)
const loading = ref(true)
const syncNotice = ref('')
const messageList = ref<HTMLElement | null>(null)
const terminal = computed(() => ['AGREED', 'CLOSED'].includes(detail.value?.negotiation.status ?? ''))
const unified = computed(() => ['negotiation-v2', 'negotiation-v3'].includes(detail.value?.graph_version ?? ''))
let observer: IntersectionObserver | undefined
const acknowledging = new Set<string>()
const refreshing = ref(new Set<string>())
let mounted = false

const { operations, notice, operationError, operationBusy, begin, check, retry, newIntent } = usePendingOperations(
  () => String(sid.value), (op, result) => {
    if (typeof result.run_id !== 'string') return
    runs.value[result.run_id] = 'task_status' in result ? result as unknown as RunStatus : {
      run_id: result.run_id, input_seq: Number(result.input_seq), task_status: String(result.status),
      business_status: 'NOT_COMMITTED', business_outcome: null, notification_status: 'NOT_REQUIRED',
      blocked_by_input_seq: null, public_error_category: null, recovery_required: false,
    }
    if (submittedDrafts.get(op.requestId) === draftRevision) text.value = ''
    submittedDrafts.delete(op.requestId)
    polling.refresh()
  },
)
watch(operationBusy, value => { busy.value = value }, { immediate: true, flush: 'sync' })
const unfinishedRuns = computed(() => Object.values(runs.value).filter(shouldPollRun))
const manualRecovery = computed(() => unfinishedRuns.value.some(needsManualRecovery))
function actionPending(kind: 'confirm' | 'close') {
  return operations.value.some(op => op.kind === kind && op.status === 'accepted'
    && (!op.runId || !runs.value[op.runId] || shouldPollRun(runs.value[op.runId]!)))
}

function load() {
  polling.stop()
  pageGeneration++
  viewId = crypto.randomUUID()
  loading.value = true
  messages.value = []; detail.value = null; receipts.value = {}; runs.value = {}
  text.value = ''; error.value = ''; syncNotice.value = ''; acknowledging.clear()
  submittedDrafts.clear()
  refreshing.value.clear()
  resumeSync()
}

async function renderCards(scroll = false) {
  await nextTick()
  if (scroll) messageList.value?.scrollTo({ top: messageList.value.scrollHeight })
  observer?.disconnect()
  messageList.value?.querySelectorAll('[data-context]').forEach(el => observer?.observe(el))
}

async function markDisplayed(context: string) {
  const alive = pageGuard()
  const current = sid.value
  if (!context || receipts.value[context] || acknowledging.has(context) || refreshing.value.has(context) || document.visibilityState !== 'visible') return
  acknowledging.add(context)
  try {
    const result = await acknowledgeDisplay(current, context, viewId, `display-${context}`)
    if (alive() && !refreshing.value.has(context)) receipts.value[context] = result.display_receipt_id
  } catch { /* An invalid card never grants confirmation; the card offers refresh. */ }
  finally { acknowledging.delete(context) }
}

function latestReceipt(): string | null {
  for (const message of [...messages.value].reverse()) {
    const context = message.card?.context_id
    if (context && receipts.value[context]) return receipts.value[context]!
  }
  return null
}

async function submit(option?: DraftOption) {
  if (busy.value || terminal.value || (!option && !text.value.trim())) return
  const requestId = crypto.randomUUID().replaceAll('-', '')
  if (!option) submittedDrafts.set(requestId, draftRevision)
  await begin(String(sid.value), 'message', {
    request_id: requestId, content: option ? '继续' : text.value.trim(),
    client_view_id: viewId, display_receipt_id: latestReceipt(),
    ...(option ? { draft_id: option.draft_id, draft_version: option.draft_version } : {}),
  })
  if (!operations.value.some(op => op.requestId === requestId && ['unknown', 'blocked'].includes(op.status))) {
    submittedDrafts.delete(requestId)
  }
}

async function confirm(card: PublicCard) {
  const context = card.context_id
  if (!activeCard(card) || !card.offer_id || !context || !receipts.value[context] || busy.value || actionPending('confirm')) return
  await begin(String(sid.value), 'confirm', { request_id: crypto.randomUUID().replaceAll('-', ''),
    offer_id: card.offer_id, client_view_id: viewId, display_receipt_id: receipts.value[context]! })
}

async function close() {
  if (busy.value || terminal.value || actionPending('close')) return
  await begin(String(sid.value), 'close', { request_id: crypto.randomUUID().replaceAll('-', ''), client_view_id: viewId })
}

async function refresh(card: PublicCard) {
  const alive = pageGuard()
  const context = card.context_id
  if (!context || refreshing.value.has(context)) return
  const current = sid.value
  refreshing.value.add(context)
  delete receipts.value[context]
  error.value = ''
  try {
    const result = await refreshContext(current, context, crypto.randomUUID())
    if (!alive()) return
    card.context_id = result.context_id
    await renderCards()
  } catch (e) { if (alive()) error.value = e instanceof Error ? e.message : '方案已失效，请重新协商' }
  finally { if (alive()) refreshing.value.delete(context) }
}

async function syncPage(signal: AbortSignal, cycleAlive: () => boolean) {
  const pageAlive = pageGuard()
  const alive = () => pageAlive() && cycleAlive()
  const current = sid.value
  const after = Math.max(0, ...messages.value.map(m => m.id))
  // Apply successful reads independently: one failed run cannot starve other inputs.
  const results = await Promise.allSettled([
    getMessages(current, after, signal).then(updates => {
      if (!alive()) return
      messages.value.push(...updates.filter(m => !messages.value.some(old => old.id === m.id)))
      return renderCards(updates.length > 0)
    }),
    getNegotiation(current, signal).then(info => {
      if (!alive()) return
      const firstLoad = detail.value === null
      detail.value = info
      // History may arrive before the workspace exists. Scroll once it mounts,
      // so display acknowledgements still require genuinely visible cards.
      return renderCards(firstLoad)
    }),
    ...Object.entries(runs.value).filter(([, run]) => shouldPollRun(run)).map(([id]) =>
      getRun(current, id, signal).then(result => { if (alive()) runs.value[id] = result })),
  ])
  if (!alive()) return
  loading.value = false
  if (results.some(r => r.status === 'rejected')) throw new Error('status sync failed')
  syncNotice.value = ''
}

const polling = createPolling({ sync: syncPage, delay: () => runPollDelay(Object.values(runs.value)),
  onError: () => { syncNotice.value = '状态同步暂时中断，将自动重试；原操作仍保留。' },
})
function resumeSync() {
  if (!mounted || !auth.currentUser.value) return
  if (!navigator.onLine || document.visibilityState !== 'visible') {
    polling.stop()
    if (!navigator.onLine) syncNotice.value = '网络已断开，连接恢复后将核对最新状态。'
    return
  }
  polling.start()
  void recheckUntracked()
  void renderCards()
}
async function recheckUntracked() {
  const alive = pageGuard()
  for (const op of [...operations.value]) {
    if (!alive()) return
    if (op.status !== 'rejected' && (!op.runId || !runs.value[op.runId])) await check(op)
  }
}

function activeCard(card: PublicCard) {
  return !terminal.value && card.can_confirm && card.offer_id === detail.value?.negotiation.current_offer_id
    && (!card.expires_at || Date.parse(card.expires_at) > Date.now())
}

watch([sid, auth.version], () => {
  if (auth.currentUser.value) void load()
  else { polling.stop(); pageGeneration++; detail.value = null; messages.value = []; runs.value = {}; receipts.value = {} }
}, { immediate: true, flush: 'sync' })
onMounted(() => {
  mounted = true
  observer = new IntersectionObserver(entries => entries.forEach(entry => {
    if (entry.isIntersecting) void markDisplayed((entry.target as HTMLElement).dataset.context ?? '')
  }), { threshold: 0.5 })
  void renderCards()
  window.addEventListener('online', resumeSync)
  window.addEventListener('offline', resumeSync)
  document.addEventListener('visibilitychange', resumeSync)
  resumeSync()
})
onUnmounted(() => {
  mounted = false; pageGeneration++; polling.stop(); observer?.disconnect()
  window.removeEventListener('online', resumeSync)
  window.removeEventListener('offline', resumeSync)
  document.removeEventListener('visibilitychange', resumeSync)
})
</script>

<template>
  <section class="buyer-page negotiation-page">
    <div class="buyer-page-heading"><div><RouterLink class="back-link" to="/buyer/negotiations">← 我的协商</RouterLink><h1>协商会话 #{{ sid }}</h1></div></div>
    <DemoNotice />
    <p v-if="loading" class="loading-card">正在读取会话…</p>
    <p v-if="syncNotice" class="sync-notice" role="status">{{ syncNotice }}</p>
    <section v-if="detail" class="workspace">
      <aside class="product-panel">
        <h2>{{ detail.product.title }}</h2><p class="product-description">{{ detail.product.description }}</p>
        <div class="listed-price"><span>公开标价</span><strong>¥{{ detail.product.listed_price }}</strong></div>
        <p>议价进度 {{ detail.negotiation.round_count }} / {{ detail.negotiation.max_rounds }} 轮</p>
        <p v-if="terminal">{{ detail.negotiation.status === 'AGREED' ? '已记录交易意向' : '协商已结束' }}</p>
        <p v-if="detail.negotiation.status === 'WAITING_APPROVAL'" class="approval-waiting">等待卖家审批；仍可补充或修改报价，也可结束协商。</p>
        <p class="privacy-note">确认交易意向不代表付款、锁定库存或完成交易。</p>
      </aside>
      <section class="chat-panel">
        <div class="chat-heading"><h2>和卖家助手协商</h2><button v-if="unified && !terminal" type="button" :disabled="busy || actionPending('close')" @click="close">结束协商</button></div>
        <div ref="messageList" class="message-list" aria-live="polite">
          <p v-if="!messages.length">可以直接提问或报价，例如“电池怎么样？900 自提可以吗？”</p>
          <article v-for="message in messages" :key="message.id" class="message" :data-role="message.role">
            <strong>{{ message.role === 'BUYER' ? '你' : message.role === 'SYSTEM' ? '系统' : '卖家助手' }}</strong>
            <p style="white-space: pre-wrap">{{ message.content }}</p>
            <section v-if="message.card" class="offer-card" :data-context="message.card.context_id || undefined">
              <template v-if="message.card.kind !== 'drafts'">
                <strong>¥{{ message.card.amount }}</strong>
                <p>{{ message.card.terms?.delivery_method === 'pickup' ? '自提' : message.card.shipping_paid_by === 'seller' ? '卖家包邮' : '快递，买家承担运费' }}</p>
                <button v-if="activeCard(message.card)" type="button" :disabled="busy || actionPending('confirm') || !receipts[message.card.context_id || '']" @click="confirm(message.card)">确认交易意向</button>
                <p v-else>此卡片当前不可确认</p>
              </template>
              <div v-for="(option, index) in message.card.drafts" :key="option.draft_id">
                <strong>方案 {{ index + 1 }} · {{ option.candidate.amount ? `¥${option.candidate.amount}` : '金额待补充' }}</strong>
                <p>{{ option.candidate.delivery === 'pickup' ? '自提' : option.candidate.delivery === 'shipping' ? '快递' : '配送方式待补充' }}{{ option.candidate.payer === 'seller' ? '，卖家包邮' : '' }}{{ option.candidate.region ? `，${option.candidate.region}` : '' }}</p>
                <p v-for="condition in option.candidate.conditions" :key="condition.source_quote">前提：{{ condition.source_quote }}</p>
                <p v-for="commitment in option.candidate.commitments" :key="commitment">要求：{{ commitment }}</p>
                <button v-if="!terminal" type="button" :disabled="busy || !receipts[message.card.context_id || '']" @click="submit(option)">选择并继续此方案</button>
              </div>
              <button v-if="message.card.context_id && !terminal" type="button" class="ghost-button" :disabled="refreshing.has(message.card.context_id)" @click="refresh(message.card)">刷新此方案</button>
            </section>
          </article>
        </div>
        <section v-if="unfinishedRuns.length" class="queue-notice" aria-label="输入处理进度" role="status">
          <p>本页跟踪的未完成输入：{{ unfinishedRuns.length }} 条。输入按顺序处理，无需重复点击；可以继续编辑下一条消息。</p>
          <p v-if="manualRecovery">需要卖家在“卖家模式 → 恢复任务”中处置；原输入会保留，后续输入需等待前序恢复。</p>
          <p v-if="actionPending('confirm')">确认操作已接受，正在核对结果。</p>
          <p v-if="actionPending('close')">结束操作已接受，正在核对结果。</p>
        </section>
        <p v-for="run in Object.values(runs).sort((a, b) => a.input_seq - b.input_seq)" :key="run.run_id" class="outcome-banner">输入 #{{ run.input_seq }}：{{ outcome(run, detail.negotiation.status) }}</p>
        <PendingOperations :operations="operations" :busy="operationBusy" :notice="notice" :error="operationError"
          @check="check" @retry="retry" @new-intent="newIntent" />
        <p v-if="error" class="error-banner" role="alert">{{ error }}</p>
        <p v-if="!unified" class="outcome-banner">此会话使用旧版流程。新版统一聊天适用于新会话。</p>
        <form v-if="unified && !terminal" class="composer" @submit.prevent="submit()">
          <label class="message-input"><span class="sr-only">聊天消息</span><textarea v-model="text" maxlength="4000" rows="3" placeholder="提问、报价或补充上一个方案的资料…" @keydown.ctrl.enter.prevent="submit()"></textarea></label>
          <div class="composer-footer"><span>Ctrl + Enter 发送</span><button type="submit" :disabled="busy || !text.trim()">{{ busy ? '提交中…' : '发送' }}</button></div>
        </form>
      </section>
    </section>
    <p v-else-if="error" class="error-banner">{{ error }}</p>
  </section>
</template>
