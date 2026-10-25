<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ApiError } from '../api/client'
import {
  acknowledgeDisplay, closeNegotiation, confirmNegotiation, getMessages, getNegotiation,
  getRun, refreshContext, sendMessage,
  type AcceptedInput, type ChatMessage, type DraftOption, type NegotiationDetail,
  type PublicCard, type RunStatus, type SendMessagePayload,
} from '../api/negotiations'

const route = useRoute()
const sid = computed(() => Number(route.params.sessionId))
const viewId = crypto.randomUUID()
const detail = ref<NegotiationDetail | null>(null)
const messages = ref<ChatMessage[]>([])
const runs = ref<Record<string, RunStatus>>({})
const receipts = ref<Record<string, string>>({})
const text = ref('')
const error = ref('')
const busy = ref(false)
const loading = ref(true)
const messageList = ref<HTMLElement | null>(null)
const terminal = computed(() => ['AGREED', 'CLOSED'].includes(detail.value?.negotiation.status ?? ''))
const unified = computed(() => ['negotiation-v2', 'negotiation-v3'].includes(detail.value?.graph_version ?? ''))
let pending: { sid: number; payload: SendMessagePayload } | null = null
let pendingButton: { key: string; request: string; receipt: string } | null = null
let timer: number | undefined
let observer: IntersectionObserver | undefined
const acknowledging = new Set<string>()
let polling = false

function remember(result: AcceptedInput) {
  runs.value[result.run_id] = { run_id: result.run_id, input_seq: result.input_seq,
    task_status: result.status, business_status: 'NOT_COMMITTED', business_outcome: null,
    notification_status: 'NOT_REQUIRED', blocked_by_input_seq: null,
    public_error_category: null, recovery_required: false }
  sessionStorage.setItem(`negotiation-runs:${sid.value}`, JSON.stringify(Object.keys(runs.value)))
}

async function load() {
  const current = sid.value
  loading.value = true
  messages.value = []; detail.value = null; receipts.value = {}; runs.value = {}
  pending = null; pendingButton = null; error.value = ''
  try {
    const [info, history] = await Promise.all([getNegotiation(current), getMessages(current)])
    if (sid.value !== current) return
    detail.value = info; messages.value = history
    const ids: string[] = JSON.parse(sessionStorage.getItem(`negotiation-runs:${current}`) || '[]')
    for (const id of ids) {
      try { runs.value[id] = await getRun(current, id) } catch { /* Expired references are ignored. */ }
    }
    await renderCards(true)
  } catch (e) { error.value = e instanceof Error ? e.message : '会话加载失败' }
  finally { if (sid.value === current) loading.value = false }
}

async function renderCards(scroll = false) {
  await nextTick()
  if (scroll) messageList.value?.scrollTo({ top: messageList.value.scrollHeight })
  observer?.disconnect()
  messageList.value?.querySelectorAll('[data-context]').forEach(el => observer?.observe(el))
}

async function markDisplayed(context: string) {
  const current = sid.value
  if (!context || receipts.value[context] || acknowledging.has(context) || document.visibilityState !== 'visible') return
  acknowledging.add(context)
  try {
    const result = await acknowledgeDisplay(current, context, viewId, `display-${context}`)
    if (sid.value === current) receipts.value[context] = result.display_receipt_id
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
  const current = sid.value
  const content = option ? '继续' : text.value.trim()
  // Retries preserve the complete original input, including its display reference.
  if (!pending || pending.sid !== current || pending.payload.content !== content
      || pending.payload.draft_id !== option?.draft_id) {
    pending = { sid: current, payload: { request_id: crypto.randomUUID().replaceAll('-', ''),
      content, client_view_id: viewId, display_receipt_id: latestReceipt(),
      ...(option ? { draft_id: option.draft_id, draft_version: option.draft_version } : {}) } }
  }
  busy.value = true; error.value = ''
  try {
    const result = await sendMessage(current, pending.payload)
    if (sid.value !== current) return
    remember(result); pending = null; text.value = ''
    await poll()
  } catch (e) {
    if (e instanceof ApiError && [400, 404, 409, 422].includes(e.status)) pending = null
    error.value = e instanceof Error ? e.message : '发送失败，可重试原输入'
  }
  finally { busy.value = false }
}

async function confirm(card: PublicCard) {
  const context = card.context_id
  if (!card.offer_id || !context || !receipts.value[context] || busy.value) return
  const current = sid.value
  const key = `${current}:confirm:${context}`
  if (pendingButton?.key !== key) pendingButton = { key, request: crypto.randomUUID().replaceAll('-', ''), receipt: receipts.value[context]! }
  busy.value = true; error.value = ''
  try {
    const result = await confirmNegotiation(current, card.offer_id, pendingButton.request, viewId, pendingButton.receipt)
    if (sid.value !== current) return
    remember(result); pendingButton = null; await poll()
  } catch (e) { error.value = e instanceof Error ? e.message : '确认失败' }
  finally { busy.value = false }
}

async function close() {
  if (busy.value) return
  const current = sid.value
  const key = `${current}:close`
  if (pendingButton?.key !== key) pendingButton = { key, request: crypto.randomUUID().replaceAll('-', ''), receipt: '' }
  busy.value = true; error.value = ''
  try {
    const result = await closeNegotiation(current, pendingButton.request, viewId)
    if (sid.value !== current) return
    remember(result); pendingButton = null; await poll()
  } catch (e) { error.value = e instanceof Error ? e.message : '结束协商失败' }
  finally { busy.value = false }
}

async function refresh(card: PublicCard) {
  if (!card.context_id) return
  const current = sid.value
  try {
    const result = await refreshContext(current, card.context_id, crypto.randomUUID())
    if (sid.value !== current) return
    card.context_id = result.context_id
    await renderCards()
  } catch (e) { error.value = e instanceof Error ? e.message : '方案已失效，请重新协商' }
}

async function poll() {
  if (polling || !detail.value) return
  polling = true
  const current = sid.value
  try {
    const after = Math.max(0, ...messages.value.map(m => m.id))
    const [updates, info] = await Promise.all([getMessages(current, after), getNegotiation(current)])
    if (current !== sid.value) return
    detail.value = info
    messages.value.push(...updates.filter(m => !messages.value.some(old => old.id === m.id)))
    for (const [id, run] of Object.entries(runs.value)) {
      if (run.task_status !== 'SUCCEEDED') runs.value[id] = await getRun(current, id)
    }
    await renderCards(updates.length > 0)
  } catch { /* Polling never changes the pending input or its receipt. */ }
  finally { polling = false }
}

function outcome(run: RunStatus) {
  const names: Record<string, string> = { OFFER_ACCEPTED: '报价已接受，待最终确认', WAITING_APPROVAL: '等待卖家审批',
    OFFER_REJECTED: '报价未被接受', INTENT_CONFIRMED: '交易意向已记录', OFFER_WITHDRAWN: '报价已撤回', SESSION_CLOSED: '协商已结束' }
  if (run.business_status === 'COMMITTED') return `${names[run.business_outcome ?? ''] ?? '业务结果已记录'}${run.notification_status !== 'PUBLISHED' ? '；结果通知恢复中' : ''}`
  if (run.recovery_required || run.task_status === 'FAILED') return '处理需要恢复，原输入已保留'
  if (run.blocked_by_input_seq) return `等待前序输入 #${run.blocked_by_input_seq}`
  return run.task_status === 'SUCCEEDED' ? '已回复，未提交业务动作' : '正在处理'
}

function activeCard(card: PublicCard) {
  return !terminal.value && card.can_confirm && card.offer_id === detail.value?.negotiation.current_offer_id
    && (!card.expires_at || Date.parse(card.expires_at) > Date.now())
}

watch(sid, () => void load(), { immediate: true })
onMounted(() => {
  observer = new IntersectionObserver(entries => entries.forEach(entry => {
    if (entry.isIntersecting) void markDisplayed((entry.target as HTMLElement).dataset.context ?? '')
  }), { threshold: 0.5 })
  void renderCards()
  timer = window.setInterval(() => void poll(), 2000)
})
onUnmounted(() => { if (timer) clearInterval(timer); observer?.disconnect() })
</script>

<template>
  <section class="buyer-page negotiation-page">
    <div class="buyer-page-heading"><div><RouterLink class="back-link" to="/buyer/negotiations">← 我的协商</RouterLink><h1>协商会话 #{{ sid }}</h1></div></div>
    <p v-if="loading" class="loading-card">正在读取会话…</p>
    <section v-if="detail" class="workspace">
      <aside class="product-panel">
        <h2>{{ detail.product.title }}</h2><p class="product-description">{{ detail.product.description }}</p>
        <div class="listed-price"><span>公开标价</span><strong>¥{{ detail.product.listed_price }}</strong></div>
        <p>议价进度 {{ detail.negotiation.round_count }} / {{ detail.negotiation.max_rounds }} 轮</p>
        <p v-if="terminal">{{ detail.negotiation.status === 'AGREED' ? '已记录交易意向' : '协商已结束' }}</p>
        <p class="privacy-note">确认交易意向不代表付款、锁定库存或完成交易。</p>
      </aside>
      <section class="chat-panel">
        <div class="chat-heading"><h2>和卖家助手协商</h2><button v-if="unified && !terminal" type="button" :disabled="busy" @click="close">结束协商</button></div>
        <div ref="messageList" class="message-list" aria-live="polite">
          <p v-if="!messages.length">可以直接提问或报价，例如“电池怎么样？900 自提可以吗？”</p>
          <article v-for="message in messages" :key="message.id" class="message" :data-role="message.role">
            <strong>{{ message.role === 'BUYER' ? '你' : message.role === 'SYSTEM' ? '系统' : '卖家助手' }}</strong>
            <p style="white-space: pre-wrap">{{ message.content }}</p>
            <section v-if="message.card" class="offer-card" :data-context="message.card.context_id || undefined">
              <template v-if="message.card.kind !== 'drafts'">
                <strong>¥{{ message.card.amount }}</strong>
                <p>{{ message.card.terms?.delivery_method === 'pickup' ? '自提' : message.card.shipping_paid_by === 'seller' ? '卖家包邮' : '快递，买家承担运费' }}</p>
                <button v-if="activeCard(message.card)" type="button" :disabled="busy || !receipts[message.card.context_id || '']" @click="confirm(message.card)">确认交易意向</button>
                <p v-else>此卡片当前不可确认</p>
              </template>
              <div v-for="(option, index) in message.card.drafts" :key="option.draft_id">
                <strong>方案 {{ index + 1 }} · {{ option.candidate.amount ? `¥${option.candidate.amount}` : '金额待补充' }}</strong>
                <p>{{ option.candidate.delivery === 'pickup' ? '自提' : option.candidate.delivery === 'shipping' ? '快递' : '配送方式待补充' }}{{ option.candidate.payer === 'seller' ? '，卖家包邮' : '' }}{{ option.candidate.region ? `，${option.candidate.region}` : '' }}</p>
                <p v-for="condition in option.candidate.conditions" :key="condition.source_quote">前提：{{ condition.source_quote }}</p>
                <p v-for="commitment in option.candidate.commitments" :key="commitment">要求：{{ commitment }}</p>
                <button v-if="!terminal" type="button" :disabled="busy || !receipts[message.card.context_id || '']" @click="submit(option)">选择并继续此方案</button>
              </div>
              <button v-if="message.card.context_id && !terminal" type="button" class="ghost-button" @click="refresh(message.card)">刷新此方案</button>
            </section>
          </article>
        </div>
        <p v-for="run in Object.values(runs)" :key="run.run_id" class="outcome-banner">输入 #{{ run.input_seq }}：{{ outcome(run) }}</p>
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
