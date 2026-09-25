<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'

import { getHealth, getReadiness } from './api/health'
import {
  ApiError,
  getMessages,
  getNegotiation,
  sendMessage,
  type BuyerOfferPayload,
  type ChatMessage,
  type NegotiationDetail,
  type ShippingPayer,
} from './api/negotiations'

const sessionId = 1001
const buyerId = 'demo-buyer'

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

const currentOffer = computed(() => {
  const state = negotiation.value?.negotiation
  if (!state?.current_offer_id) return null
  return state.recent_offers.find((offer) => offer.id === state.current_offer_id) ?? null
})

const canSend = computed(() => {
  if (!modelReady.value || !messageText.value.trim() || sending.value) return false
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
    const [detail, history] = await Promise.all([
      getNegotiation(sessionId, buyerId),
      getMessages(sessionId, buyerId),
    ])
    negotiation.value = detail
    messages.value = history
    await scrollToLatest()
  } catch (error) {
    serviceReady.value = false
    modelReady.value = false
    errorMessage.value = readableError(error)
  } finally {
    loading.value = false
  }
}

async function submitMessage(): Promise<void> {
  if (!canSend.value) return
  sending.value = true
  errorMessage.value = ''

  const offer = buildOfferPayload()
  try {
    const response = await sendMessage(sessionId, buyerId, {
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
    negotiation.value = await getNegotiation(sessionId, buyerId)
    await scrollToLatest()
  } catch (error) {
    errorMessage.value = readableError(error)
  } finally {
    sending.value = false
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

onMounted(loadPage)

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
      <div class="service-status" :data-ready="serviceReady && modelReady">
        <span aria-hidden="true"></span>
        {{ serviceReady ? (modelReady ? '协商服务已就绪' : '模型未配置') : '服务未就绪' }}
      </div>
    </header>

    <section v-if="loading" class="loading-card">正在读取演示会话…</section>

    <section v-else-if="negotiation" class="workspace">
      <aside class="product-panel">
        <div class="product-visual" aria-hidden="true">
          <span>14</span>
          <small>PRO</small>
        </div>
        <p class="eyebrow">演示商品 · #{{ negotiation.product.id }}</p>
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
        </section>

        <p class="privacy-note">卖家底价和自动接受阈值不会展示给买家。</p>
      </aside>

      <section class="chat-panel" aria-labelledby="chat-title">
        <div class="chat-heading">
          <div>
            <p class="eyebrow">LIVE NEGOTIATION</p>
            <h2 id="chat-title">和 Seller Agent 协商</h2>
          </div>
          <button class="ghost-button" type="button" @click="loadPage">刷新</button>
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

        <form class="composer" @submit.prevent="submitMessage">
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
      <button type="button" @click="loadPage">重新连接</button>
    </section>
  </main>
</template>
