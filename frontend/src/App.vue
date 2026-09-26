<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'

import { getHealth, getReadiness } from './api/health'
import { ensureVisitorIdentity } from './api/auth'
import { listPublicProducts, type PublicProduct } from './api/products'
import {
  ApiError,
  closeNegotiation,
  confirmNegotiation,
  createNegotiation,
  getMessages,
  getNegotiation,
  sendMessage,
  type BuyerOfferPayload,
  type ChatMessage,
  type NegotiationDetail,
  type ShippingPayer,
} from './api/negotiations'
import SellerManagement from './components/SellerManagement.vue'

const activeView = ref<'buyer' | 'seller'>('buyer')
const products = ref<PublicProduct[]>([])
const selectedProductId = ref<number | null>(null)
const sessionId = ref<number | null>(null)

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
  loading.value = true
  errorMessage.value = ''
  try {
    await getHealth()
    const readiness = await getReadiness()
    serviceReady.value = true
    modelReady.value = readiness.model === 'configured'
    if (readiness.authentication !== 'configured') {
      throw new Error('认证服务尚未配置，请在本地 .env 设置 AUTH_SECRET。')
    }
    await ensureVisitorIdentity()
    products.value = await listPublicProducts()
    if (products.value.length === 0) {
      throw new Error('目前没有已上架的商品，请先在卖家管理中创建并上架商品。')
    }
    if (!products.value.some((product) => product.id === selectedProductId.value)) {
      selectedProductId.value = products.value[0]?.id ?? null
    }
    if (selectedProductId.value === null) {
      throw new Error('没有可协商的商品。')
    }
    await loadProductSession(selectedProductId.value)
  } catch (error) {
    serviceReady.value = false
    modelReady.value = false
    errorMessage.value = readableError(error)
  } finally {
    loading.value = false
  }
}

async function loadProductSession(productId: number): Promise<void> {
  const activeSession = await createNegotiation(productId)
  sessionId.value = activeSession.session_id
  const [detail, history] = await Promise.all([
    getNegotiation(activeSession.session_id),
    getMessages(activeSession.session_id),
  ])
  negotiation.value = detail
  messages.value = history
  lastOutcome.value = ''
  await scrollToLatest()
}

async function switchProduct(): Promise<void> {
  if (selectedProductId.value === null) return
  loading.value = true
  errorMessage.value = ''
  try {
    await loadProductSession(selectedProductId.value)
  } catch (error) {
    errorMessage.value = readableError(error)
  } finally {
    loading.value = false
  }
}

async function submitMessage(): Promise<void> {
  if (!canSend.value || sessionId.value === null) return
  sending.value = true
  errorMessage.value = ''

  const offer = buildOfferPayload()
  try {
    const response = await sendMessage(sessionId.value, {
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
    negotiation.value = await getNegotiation(sessionId.value)
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
    activeView.value !== 'buyer'
    || activeSessionId === null
    || loading.value
    || sending.value
    || polling.value
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
    // 切换商品期间返回的旧请求不能覆盖新会话页面。
    if (sessionId.value !== activeSessionId) return
    negotiation.value = detail
    if (updates.length > 0) {
      const knownIds = new Set(messages.value.map((message) => message.id))
      messages.value.push(...updates.filter((message) => !knownIds.has(message.id)))
      await scrollToLatest()
    }
  } catch {
    // 轮询瞬时失败不覆盖正在编辑的内容，下一轮会从最后一条消息继续补取。
  } finally {
    polling.value = false
  }
}

async function confirmCurrentOffer(): Promise<void> {
  const activeSessionId = sessionId.value
  const offer = currentOffer.value
  if (!canConfirmCurrentOffer.value || activeSessionId === null || !offer) return

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
        // 状态刷新失败时仍展示原始确认冲突，下一次轮询会继续同步状态。
      }
    }
    errorMessage.value = readableError(error)
  } finally {
    lifecycleBusy.value = false
  }
}

async function closeCurrentNegotiation(): Promise<void> {
  const activeSessionId = sessionId.value
  if (!canCloseNegotiation.value || activeSessionId === null) return
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
    const result = await closeNegotiation(
      activeSessionId,
      pendingClosure.requestId,
    )
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
        // 状态刷新失败时仍展示原始关闭冲突，避免网络错误覆盖业务原因。
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
    return '模型服务尚未配置，请在本地 .env 填写 MODEL_BASE_URL 和 MODEL_API_KEY。'
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

async function scrollToLatest(): Promise<void> {
  await nextTick()
  messageList.value?.scrollTo({ top: messageList.value.scrollHeight, behavior: 'smooth' })
}

onMounted(() => {
  void loadPage()
  messagePollTimer = window.setInterval(
    () => void pollNegotiationUpdates(),
    MESSAGE_POLL_INTERVAL_MS,
  )
})

onUnmounted(() => {
  if (messagePollTimer !== undefined) {
    window.clearInterval(messagePollTimer)
  }
})

watch(activeView, (view) => {
  if (view === 'buyer') void pollNegotiationUpdates()
})

watch(deliveryMethod, (value) => {
  if (value === 'pickup') {
    shippingPaidBy.value = 'buyer'
    shippingCost.value = ''
  }
})
</script>

<template>
  <main class="app-shell">
    <header class="topbar">
      <a class="brand" href="#" aria-label="SecondHand Agent 首页">
        <span class="brand-mark">S</span>
        <span>
          <strong>SecondHand</strong>
          <small>自主协商卖家助手</small>
        </span>
      </a>
      <div class="topbar-actions">
        <nav class="view-switcher" aria-label="功能导航">
          <button
            type="button"
            :data-active="activeView === 'buyer'"
            @click="activeView = 'buyer'"
          >
            买家协商
          </button>
          <button
            type="button"
            :data-active="activeView === 'seller'"
            @click="activeView = 'seller'"
          >
            卖家管理
          </button>
        </nav>
        <div class="service-status" :data-ready="serviceReady && modelReady">
          <span aria-hidden="true"></span>
          {{ serviceReady ? (modelReady ? '协商服务已就绪' : '模型未配置') : '服务未就绪' }}
        </div>
      </div>
    </header>

    <SellerManagement v-if="activeView === 'seller'" />

    <section v-else-if="loading" class="loading-card">正在读取商品与会话…</section>

    <section v-else-if="negotiation" class="workspace">
      <aside class="product-panel">
        <div class="product-visual" aria-hidden="true">
          <span>14</span>
          <small>PRO</small>
        </div>
        <p class="eyebrow">演示商品 · #{{ negotiation.product.id }}</p>
        <label class="catalog-picker">
          <span>选择商品</span>
          <select v-model="selectedProductId" @change="switchProduct">
            <option v-for="product in products" :key="product.id" :value="product.id">
              #{{ product.id }} · {{ product.title }}
            </option>
          </select>
        </label>
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
            <dd>{{ negotiation.negotiation.status }}</dd>
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
        <p v-else-if="lastOutcome" class="outcome-banner">本轮结果：{{ lastOutcome }}</p>
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
              :disabled="sessionIsTerminal"
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
      <button type="button" @click="loadPage">重新连接</button>
    </section>
  </main>
</template>
