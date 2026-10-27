<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import PendingOperations from '../components/PendingOperations.vue'
import { useAuth } from '../state/auth'
import { usePendingOperations } from '../state/usePendingOperations'
import { runOutcome as outcome, shouldPollRun } from '../state/runTracking'
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
const error = ref('')
const busy = ref(false)
const loading = ref(true)
const messageList = ref<HTMLElement | null>(null)
const terminal = computed(() => ['AGREED', 'CLOSED'].includes(detail.value?.negotiation.status ?? ''))
const unified = computed(() => ['negotiation-v2', 'negotiation-v3'].includes(detail.value?.graph_version ?? ''))
let timer: number | undefined
let observer: IntersectionObserver | undefined
const acknowledging = new Set<string>()
let polling = false

const { operations, notice, operationError, operationBusy, begin, check, retry, newIntent } = usePendingOperations(
  () => String(sid.value), (op, result) => {
    if (typeof result.run_id !== 'string') return
    runs.value[result.run_id] = 'task_status' in result ? result as unknown as RunStatus : {
      run_id: result.run_id, input_seq: Number(result.input_seq), task_status: String(result.status),
      business_status: 'NOT_COMMITTED', business_outcome: null, notification_status: 'NOT_REQUIRED',
      blocked_by_input_seq: null, public_error_category: null, recovery_required: false,
    }
    if (op.kind === 'message') text.value = ''
    void poll()
  },
)
watch(operationBusy, value => { busy.value = value }, { immediate: true, flush: 'sync' })

async function load() {
  pageGeneration++
  const alive = pageGuard()
  const current = sid.value
  viewId = crypto.randomUUID()
  loading.value = true
  messages.value = []; detail.value = null; receipts.value = {}; runs.value = {}
  text.value = ''; error.value = ''; acknowledging.clear()
  try {
    const [info, history] = await Promise.all([getNegotiation(current), getMessages(current)])
    if (!alive()) return
    detail.value = info; messages.value = history
    await renderCards(true)
  } catch (e) { if (alive()) error.value = e instanceof Error ? e.message : '会话加载失败' }
  finally { if (alive()) loading.value = false }
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
  if (!context || receipts.value[context] || acknowledging.has(context) || document.visibilityState !== 'visible') return
  acknowledging.add(context)
  try {
    const result = await acknowledgeDisplay(current, context, viewId, `display-${context}`)
    if (alive()) receipts.value[context] = result.display_receipt_id
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
  await begin(String(sid.value), 'message', {
    request_id: crypto.randomUUID().replaceAll('-', ''), content: option ? '继续' : text.value.trim(),
    client_view_id: viewId, display_receipt_id: latestReceipt(),
    ...(option ? { draft_id: option.draft_id, draft_version: option.draft_version } : {}),
  })
}

async function confirm(card: PublicCard) {
  const context = card.context_id
  if (!card.offer_id || !context || !receipts.value[context] || busy.value) return
  await begin(String(sid.value), 'confirm', { request_id: crypto.randomUUID().replaceAll('-', ''),
    offer_id: card.offer_id, client_view_id: viewId, display_receipt_id: receipts.value[context]! })
}

async function close() {
  if (busy.value) return
  await begin(String(sid.value), 'close', { request_id: crypto.randomUUID().replaceAll('-', ''), client_view_id: viewId })
}

async function refresh(card: PublicCard) {
  const alive = pageGuard()
  if (!card.context_id) return
  const current = sid.value
  try {
    const result = await refreshContext(current, card.context_id, crypto.randomUUID())
    if (!alive()) return
    card.context_id = result.context_id
    await renderCards()
  } catch (e) { if (alive()) error.value = e instanceof Error ? e.message : '方案已失效，请重新协商' }
}

async function poll() {
  const alive = pageGuard()
  if (polling || !detail.value) return
  polling = true
  const current = sid.value
  try {
    const after = Math.max(0, ...messages.value.map(m => m.id))
    const [updates, info] = await Promise.all([getMessages(current, after), getNegotiation(current)])
    if (!alive()) return
    detail.value = info
    messages.value.push(...updates.filter(m => !messages.value.some(old => old.id === m.id)))
    for (const [id, run] of Object.entries(runs.value)) {
      if (shouldPollRun(run)) {
        const result = await getRun(current, id)
        if (!alive()) return
        runs.value[id] = result
      }
    }
    await renderCards(updates.length > 0)
  } catch { /* Polling never changes the pending input or its receipt. */ }
  finally { polling = false }
}

function activeCard(card: PublicCard) {
  return !terminal.value && card.can_confirm && card.offer_id === detail.value?.negotiation.current_offer_id
    && (!card.expires_at || Date.parse(card.expires_at) > Date.now())
}

watch([sid, auth.version], () => {
  if (auth.currentUser.value) void load()
  else { pageGeneration++; detail.value = null; messages.value = []; runs.value = {}; receipts.value = {} }
}, { immediate: true, flush: 'sync' })
onMounted(() => {
  observer = new IntersectionObserver(entries => entries.forEach(entry => {
    if (entry.isIntersecting) void markDisplayed((entry.target as HTMLElement).dataset.context ?? '')
  }), { threshold: 0.5 })
  void renderCards()
  timer = window.setInterval(() => void poll(), 2000)
})
onUnmounted(() => { pageGeneration++; if (timer) clearInterval(timer); observer?.disconnect() })
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
