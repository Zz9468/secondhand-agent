<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import { getHealth, getReadiness } from '../api/health'
import {
  ApiError,
  closeNegotiation,
  confirmNegotiation,
  getMessages,
  getNegotiation,
  sendMessage,
  type BuyerOfferPayload,
  type ChatMessage,
  type NegotiationDetail,
  type ShippingPayer,
} from '../api/negotiations'

const route = useRoute()
const sessionId = computed(() => Number(route.params.sessionId))
const negotiation = ref<NegotiationDetail | null>(null)
const messages = ref<ChatMessage[]>([])
const messageText = ref('')
const submittingOffer = ref(false)
const offerPrice = ref('')
const shippingPaidBy = ref<ShippingPayer>('buyer')
const shippingCost = ref('')
const deliveryMethod = ref<'shipping' | 'pickup'>('shipping')
const loading = ref(true)
const sending = ref(false)
const errorMessage = ref('')
const serviceReady = ref(false)
const modelReady = ref(false)
const lastOutcome = ref('')
const messageList = ref<HTMLElement | null>(null)
const polling = ref(false)
const lifecycleBusy = ref(false)

let pendingConfirmation: {
  sessionId: number
  offerId: number
  requestId: string
} | null = null
let pendingClosure: { sessionId: number; requestId: string } | null = null

const MESSAGE_POLL_INTERVAL_MS = 2000
let messagePollTimer: number | undefined

const currentOffer = computed(() => {
  const state = negotiation.value?.negotiation
  if (!state?.current_offer_id) return null
  return state.recent_offers.find((offer) => offer.id === state.current_offer_id) ?? null
})

const sessionIsTerminal = computed(() => {
  const status = negotiation.value?.negotiation.status
  return status === 'AGREED' || status === 'CLOSED'
})

const canConfirmCurrentOffer = computed(() => {
  const state = negotiation.value?.negotiation
  const offer = currentOffer.value
  if (!state || state.status !== 'ACTIVE' || !offer) return false
  if (offer.expires_at && new Date(offer.expires_at).getTime() <= Date.now()) return false
  return (
    (offer.proposer === 'AGENT' && offer.status === 'PROPOSED')
    || (offer.proposer === 'BUYER' && offer.status === 'ACCEPTED')
  )
})

const canCloseNegotiation = computed(() => {
  const status = negotiation.value?.negotiation.status
  return status === 'ACTIVE' || status === 'WAITING_APPROVAL'
})

const canSend = computed(() => {
  if (
    !modelReady.value
    || !messageText.value.trim()
    || sending.value
    || sessionIsTerminal.value
  ) return false
  if (!submittingOffer.value) return true
  if (!offerPrice.value || Number(offerPrice.value) < 0) return false
  if (deliveryMethod.value === 'pickup' || shippingPaidBy.value !== 'seller') return true
  return shippingCost.value !== '' && Number(shippingCost.value) >= 0
})

async function loadPage(): Promise<void> {
  const requestedSessionId = sessionId.value
  loading.value = true
  errorMessage.value = ''
  negotiation.value = null
  messages.value = []
  if (!Number.isInteger(requestedSessionId) || requestedSessionId <= 0) {
    errorMessage.value = '协商会话编号无效。'
    loading.value = false
    return
  }
  try {
    await getHealth()
    const readiness = await getReadiness()
    serviceReady.value = true
    modelReady.value = readiness.model === 'configured'
    const [detail, history] = await Promise.all([
      getNegotiation(requestedSessionId),
      getMessages(requestedSessionId),
    ])
    if (sessionId.value !== requestedSessionId) return
    negotiation.value = detail
    messages.value = history
    lastOutcome.value = ''
    await scrollToLatest()
  } catch (error) {
    if (sessionId.value !== requestedSessionId) return
    errorMessage.value = readableError(error)
  } finally {
    if (sessionId.value === requestedSessionId) loading.value = false
  }
}

async function submitMessage(): Promise<void> {
  const activeSessionId = sessionId.value
  if (!canSend.value || !Number.isInteger(activeSessionId)) return
  sending.value = true
  errorMessage.value = ''

  const offer = buildOfferPayload()
  try {
    const response = await sendMessage(activeSessionId, {
      request_id: crypto.randomUUID().replaceAll('-', ''),
      content: messageText.value.trim(),
      ...(offer ? { offer } : {}),
    })
    messages.value.push(response.buyer_message, response.agent_message)
    lastOutcome.value = response.outcome
    messageText.value = ''
    submittingOffer.value = false
    offerPrice.value = ''
    shippingCost.value = ''
    negotiation.value = await getNegotiation(activeSessionId)
    await scrollToLatest()
  } catch (error) {
    errorMessage.value = readableError(error)
  } finally {
    sending.value = false
  }
}

async function pollNegotiationUpdates(): Promise<void> {
  const activeSessionId = sessionId.value
  if (
    !Number.isInteger(activeSessionId)
    || negotiation.value === null
    || loading.value
    || sending.value
    || polling.value
    || sessionIsTerminal.value
  ) return

  polling.value = true
  try {
    const afterId = messages.value.reduce(
      (latest, message) => Math.max(latest, message.id),
      0,
    )
    const [updates, detail] = await Promise.all([
      getMessages(activeSessionId, afterId),
      getNegotiation(activeSessionId),
    ])
    if (sessionId.value !== activeSessionId) return
    negotiation.value = detail
    if (updates.length > 0) {
      const knownIds = new Set(messages.value.map((message) => message.id))
      messages.value.push(...updates.filter((message) => !knownIds.has(message.id)))
      await scrollToLatest()
    }
  } catch {
    // 轮询瞬时失败不打断输入，下一轮继续从最后一条消息补取。
  } finally {
    polling.value = false
  }
}

async function confirmCurrentOffer(): Promise<void> {
  const activeSessionId = sessionId.value
  const offer = currentOffer.value
  if (!canConfirmCurrentOffer.value || !Number.isInteger(activeSessionId) || !offer) return

  lifecycleBusy.value = true
  errorMessage.value = ''
  if (
    pendingConfirmation === null
    || pendingConfirmation.sessionId !== activeSessionId
    || pendingConfirmation.offerId !== offer.id
  ) {
    pendingConfirmation = {
      sessionId: activeSessionId,
      offerId: offer.id,
      requestId: crypto.randomUUID().replaceAll('-', ''),
    }
  }
  try {
    const result = await confirmNegotiation(
      activeSessionId,
      offer.id,
      pendingConfirmation.requestId,
    )
    appendMessage(result.system_message)
    negotiation.value = await getNegotiation(activeSessionId)
    lastOutcome.value = 'INTENT_AGREED'
    pendingConfirmation = null
    await scrollToLatest()
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) {
      pendingConfirmation = null
      try {
        negotiation.value = await getNegotiation(activeSessionId)
      } catch {
        // 下一轮轮询会继续同步服务端状态。
      }
    }
    errorMessage.value = readableError(error)
  } finally {
    lifecycleBusy.value = false
  }
}

async function closeCurrentNegotiation(): Promise<void> {
  const activeSessionId = sessionId.value
  if (!canCloseNegotiation.value || !Number.isInteger(activeSessionId)) return
  if (!window.confirm('确定结束本次协商吗？尚未完成的审批也会被取消。')) return

  lifecycleBusy.value = true
  errorMessage.value = ''
  if (pendingClosure === null || pendingClosure.sessionId !== activeSessionId) {
    pendingClosure = {
      sessionId: activeSessionId,
      requestId: crypto.randomUUID().replaceAll('-', ''),
    }
  }
  try {
    const result = await closeNegotiation(activeSessionId, pendingClosure.requestId)
    appendMessage(result.system_message)
    negotiation.value = await getNegotiation(activeSessionId)
    lastOutcome.value = 'NEGOTIATION_CLOSED'
    pendingClosure = null
    await scrollToLatest()
  } catch (error) {
    if (error instanceof ApiError && error.status === 409) {
      pendingClosure = null
      try {
        negotiation.value = await getNegotiation(activeSessionId)
      } catch {
        // 下一轮轮询会继续同步服务端状态。
      }
    }
    errorMessage.value = readableError(error)
  } finally {
    lifecycleBusy.value = false
  }
}

function appendMessage(message: ChatMessage): void {
  if (!messages.value.some((item) => item.id === message.id)) {
    messages.value.push(message)
  }
}

function buildOfferPayload(): BuyerOfferPayload | undefined {
  if (!submittingOffer.value) return undefined
  return {
    price: offerPrice.value,
    shipping_paid_by: shippingPaidBy.value,
    ...(shippingPaidBy.value === 'seller'
      ? { shipping_cost: shippingCost.value }
      : {}),
    delivery_method: deliveryMethod.value,
  }
}

function readableError(error: unknown): string {
  if (error instanceof ApiError && error.status === 503) {
    return '模型服务尚未配置，请在本地 .env 填写模型服务配置。'
  }
  if (error instanceof Error) return error.message
  return '请求失败，请检查后端与数据库状态。'
}

function roleLabel(role: ChatMessage['role']): string {
  return { BUYER: '你', AGENT: 'Seller Agent', SYSTEM: '系统' }[role]
}

function formatTime(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value))
}

function shippingLabel(payer: ShippingPayer): string {
  return payer === 'seller' ? '卖家包邮' : '买家承担运费'
}

function negotiationStatusLabel(status: string): string {
  return {
    ACTIVE: '协商中',
    WAITING_APPROVAL: '等待卖家审批',
    AGREED: '已确认交易意向',
    CLOSED: '已结束',
  }[status] ?? status
}

function outcomeLabel(outcome: string): string {
  return {
    INFORMATIONAL: '已回复商品咨询',
    COUNTER_OFFERED: 'Agent 已还价',
    OFFER_ACCEPTED: '报价已接受',
    NEEDS_SELLER_CONFIRMATION: '等待卖家确认',
    REJECTED: '报价未接受',
    CLARIFICATION: '需要补充条件',
    SAFE_FAILURE: '本轮未生成正式决策',
    MODEL_ERROR: '模型服务处理失败',
    APPROVAL_APPROVED: '卖家审批已通过',
    APPROVAL_REJECTED: '卖家审批未通过',
    COUNTER_OFFERED_AFTER_APPROVAL: '卖家审批后已还价',
    INTENT_AGREED: '交易意向已确认',
    NEGOTIATION_CLOSED: '协商已结束',
    IDEMPOTENT_REPLAY: '已返回原请求结果',
  }[outcome] ?? outcome
}

async function scrollToLatest(): Promise<void> {
  await nextTick()
  messageList.value?.scrollTo({ top: messageList.value.scrollHeight, behavior: 'smooth' })
}

watch(sessionId, () => {
  pendingConfirmation = null
  pendingClosure = null
  void loadPage()
}, { immediate: true })

watch(deliveryMethod, (value) => {
  if (value === 'pickup') {
    shippingPaidBy.value = 'buyer'
    shippingCost.value = ''
  }
})

onMounted(() => {
  messagePollTimer = window.setInterval(
    () => void pollNegotiationUpdates(),
    MESSAGE_POLL_INTERVAL_MS,
  )
})

onUnmounted(() => {
  if (messagePollTimer !== undefined) window.clearInterval(messagePollTimer)
})
</script>

<template>
  <section class="buyer-page negotiation-page">
    <div class="buyer-page-heading">
      <div>
        <RouterLink class="back-link" to="/buyer/negotiations">← 我的协商</RouterLink>
        <p class="eyebrow">BUYER NEGOTIATION · #{{ sessionId }}</p>
        <h1>协商会话</h1>
      </div>
      <div class="service-status" :data-ready="serviceReady && modelReady">
        <span aria-hidden="true"></span>
        {{ serviceReady ? (modelReady ? '协商服务已就绪' : '模型未配置') : '服务未就绪' }}
      </div>
    </div>

    <section v-if="loading" class="loading-card">正在读取协商会话…</section>

    <section v-else-if="negotiation" class="workspace">
      <aside class="product-panel">
        <div class="product-visual" aria-hidden="true">
          <span>{{ String(negotiation.product.id).padStart(2, '0').slice(-2) }}</span>
          <small>SECONDHAND</small>
        </div>
        <p class="eyebrow">商品 #{{ negotiation.product.id }}</p>
        <h1>{{ negotiation.product.title }}</h1>
        <p class="product-description">{{ negotiation.product.description }}</p>
        <div class="listed-price">
          <span>公开标价</span>
          <strong>¥{{ negotiation.product.listed_price }}</strong>
        </div>

        <dl class="session-facts">
          <div>
            <dt>协商进度</dt>
            <dd>{{ negotiation.negotiation.round_count }} / {{ negotiation.negotiation.max_rounds }} 轮</dd>
          </div>
          <div>
            <dt>当前状态</dt>
            <dd>{{ negotiationStatusLabel(negotiation.negotiation.status) }}</dd>
          </div>
        </dl>

        <section class="offer-card">
          <p class="eyebrow">CURRENT OFFER</p>
          <template v-if="currentOffer">
            <strong>¥{{ currentOffer.price }}</strong>
            <span>{{ currentOffer.proposer === 'BUYER' ? '买家报价' : 'Agent 还价' }}</span>
            <span>{{ shippingLabel(currentOffer.shipping_paid_by) }}</span>
          </template>
          <p v-else>还没有正式报价</p>
          <button
            v-if="canConfirmCurrentOffer"
            class="confirm-intent-button"
            type="button"
            :disabled="lifecycleBusy"
            @click="confirmCurrentOffer"
          >
            {{ lifecycleBusy ? '确认中…' : '确认交易意向' }}
          </button>
          <small v-if="canConfirmCurrentOffer" class="intent-disclaimer">
            确认后仅记录双方交易意向，不代表付款、锁定库存或实际成交。
          </small>
          <p v-else-if="negotiation.negotiation.status === 'AGREED'" class="intent-complete">
            已确认报价 #{{ negotiation.negotiation.confirmed_offer_id }}，交易意向已记录。
          </p>
        </section>

        <p class="privacy-note">卖家底价和自动接受阈值不会展示给买家。</p>
      </aside>

      <section class="chat-panel" aria-labelledby="chat-title">
        <div class="chat-heading">
          <div>
            <p class="eyebrow">LIVE NEGOTIATION</p>
            <h2 id="chat-title">和 Seller Agent 协商</h2>
          </div>
          <div class="chat-heading-actions">
            <button
              v-if="canCloseNegotiation"
              class="ghost-button close-button"
              type="button"
              :disabled="lifecycleBusy"
              @click="closeCurrentNegotiation"
            >
              结束协商
            </button>
            <button class="ghost-button" type="button" @click="loadPage">刷新</button>
          </div>
        </div>

        <div ref="messageList" class="message-list" aria-live="polite">
          <article v-if="messages.length === 0" class="welcome-message">
            <span class="agent-avatar">S</span>
            <div>
              <strong>Seller Agent</strong>
              <p>你好，可以询问商品情况，也可以在下方勾选“提交正式报价”开始议价。</p>
            </div>
          </article>

          <article
            v-for="message in messages"
            :key="message.id"
            class="message"
            :data-role="message.role"
          >
            <div class="message-meta">
              <strong>{{ roleLabel(message.role) }}</strong>
              <time>{{ formatTime(message.created_at) }}</time>
            </div>
            <p>{{ message.content }}</p>
          </article>
        </div>

        <p v-if="errorMessage" class="error-banner" role="alert">{{ errorMessage }}</p>
        <p v-else-if="lastOutcome" class="outcome-banner">本轮结果：{{ outcomeLabel(lastOutcome) }}</p>
        <p v-if="sessionIsTerminal" class="terminal-banner">
          {{ negotiation.negotiation.status === 'AGREED'
            ? '双方交易意向已经记录，本会话不能继续议价。'
            : '本次协商已经结束。' }}
        </p>

        <form v-if="!sessionIsTerminal" class="composer" @submit.prevent="submitMessage">
          <label class="message-input">
            <span class="sr-only">聊天消息</span>
            <textarea
              v-model="messageText"
              maxlength="4000"
              rows="3"
              placeholder="询问成色，或描述你的报价…"
              @keydown.ctrl.enter="submitMessage"
            ></textarea>
          </label>

          <label class="offer-toggle">
            <input v-model="submittingOffer" type="checkbox" />
            <span>提交正式报价</span>
            <small>金额与运费将由后端校验并保存</small>
          </label>

          <div v-if="submittingOffer" class="offer-fields">
            <label>
              <span>报价金额</span>
              <div class="money-input">
                <b>¥</b>
                <input v-model="offerPrice" min="0" step="0.01" type="number" required />
              </div>
            </label>
            <label>
              <span>配送方式</span>
              <select v-model="deliveryMethod">
                <option value="shipping">快递</option>
                <option value="pickup">面交</option>
              </select>
            </label>
            <label>
              <span>运费承担</span>
              <select v-model="shippingPaidBy" :disabled="deliveryMethod === 'pickup'">
                <option value="buyer">买家承担</option>
                <option value="seller">卖家承担（包邮）</option>
              </select>
            </label>
            <label v-if="shippingPaidBy === 'seller' && deliveryMethod !== 'pickup'">
              <span>预计运费</span>
              <div class="money-input">
                <b>¥</b>
                <input v-model="shippingCost" min="0" step="0.01" type="number" required />
              </div>
            </label>
          </div>

          <div class="composer-footer">
            <span>Ctrl + Enter 发送</span>
            <button type="submit" :disabled="!canSend">
              {{ sending ? '处理中…' : '发送消息' }}
            </button>
          </div>
        </form>
      </section>
    </section>

    <section v-else class="loading-card error-state">
      <h1>暂时无法载入会话</h1>
      <p>{{ errorMessage }}</p>
      <div class="welcome-actions">
        <RouterLink class="secondary-link" to="/buyer/negotiations">返回我的协商</RouterLink>
        <button type="button" @click="loadPage">重新连接</button>
      </div>
    </section>
  </section>
</template>
