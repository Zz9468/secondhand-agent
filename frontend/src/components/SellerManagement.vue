<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import {
  listSellerApprovals,
  reviewSellerApproval,
  type ApprovalStatus,
  type SellerApproval,
} from '../api/approvals'
import { ApiError } from '../api/client'
import {
  createProduct,
  listSellerProducts,
  updatePolicy,
  updateProduct,
  type NegotiationStyle,
  type ProductStatus,
  type SellerProduct,
} from '../api/products'
import {
  getSellerNegotiation,
  listSellerNegotiations,
  type NegotiationStatus,
  type SellerNegotiationDetail,
  type SellerNegotiationOffer,
  type SellerNegotiationSummary,
} from '../api/sellerNegotiations'
import { useAuth } from '../state/auth'

interface ProductForm {
  title: string
  description: string
  listedPrice: string
  status: ProductStatus
  minimumNetPrice: string
  autoAcceptThreshold: string
  negotiationStyle: NegotiationStyle
  maxRounds: number
}

type SellerSection = 'products' | 'approvals' | 'negotiations'

const auth = useAuth()
const route = useRoute()
const router = useRouter()
const products = ref<SellerProduct[]>([])
const selectedProduct = ref<SellerProduct | null>(null)
const approvals = ref<SellerApproval[]>([])
const selectedApproval = ref<SellerApproval | null>(null)
const negotiations = ref<SellerNegotiationSummary[]>([])
const selectedNegotiation = ref<SellerNegotiationDetail | null>(null)
const activeSection = computed<SellerSection>(() => {
  if (route.name === 'seller-approvals') return 'approvals'
  if (route.name === 'seller-negotiations') return 'negotiations'
  return 'products'
})
const approvalComment = ref('')
const pendingReviewRequest = ref<{
  approvalId: number
  action: 'approve' | 'reject'
  requestId: string
} | null>(null)
const loading = ref(true)
const saving = ref(false)
const reviewing = ref(false)
const polling = ref(false)
const errorMessage = ref('')
const successMessage = ref('')
const form = reactive<ProductForm>(blankForm())

const SELLER_POLL_INTERVAL_MS = 3000
let sellerPollTimer: number | undefined

function blankForm(): ProductForm {
  return {
    title: '',
    description: '',
    listedPrice: '',
    status: 'DRAFT',
    minimumNetPrice: '',
    autoAcceptThreshold: '',
    negotiationStyle: 'BALANCED',
    maxRounds: 6,
  }
}

async function loadWorkspace(): Promise<void> {
  loading.value = true
  errorMessage.value = ''
  try {
    await Promise.all([loadProducts(), loadApprovals(), loadNegotiations()])
  } catch (error) {
    errorMessage.value = readableError(error)
  } finally {
    loading.value = false
  }
}

async function loadProducts(preferredId?: number): Promise<void> {
  products.value = await listSellerProducts()
  const targetId = preferredId ?? selectedProduct.value?.id
  const target = products.value.find((product) => product.id === targetId)
  if (target) {
    selectProduct(target)
  } else {
    resetProductForm()
  }
}

function startNewProduct(): void {
  void navigateToSection('products')
  resetProductForm()
}

function resetProductForm(): void {
  selectedProduct.value = null
  Object.assign(form, blankForm())
  errorMessage.value = ''
  successMessage.value = ''
}

function selectProduct(product: SellerProduct): void {
  selectedProduct.value = product
  Object.assign(form, {
    title: product.title,
    description: product.description,
    listedPrice: product.listed_price,
    status: product.status,
    minimumNetPrice: product.policy.minimum_net_price,
    autoAcceptThreshold: product.policy.auto_accept_threshold,
    negotiationStyle: product.policy.negotiation_style,
    maxRounds: product.policy.max_rounds,
  })
  errorMessage.value = ''
  successMessage.value = ''
}

async function loadApprovals(
  preferredId?: number,
  preserveComment = false,
): Promise<void> {
  approvals.value = await listSellerApprovals()
  const targetId = preferredId ?? selectedApproval.value?.id
  const nextApproval =
    approvals.value.find((approval) => approval.id === targetId) ??
    approvals.value.find((approval) => approval.status === 'PENDING') ??
    approvals.value[0] ??
    null
  selectedApproval.value = nextApproval
  if (activeSection.value === 'approvals' && !preserveComment) {
    approvalComment.value = nextApproval?.seller_comment ?? ''
  }
}

async function showApprovals(): Promise<void> {
  await navigateToSection('approvals')
  errorMessage.value = ''
  successMessage.value = ''
  try {
    await loadApprovals()
  } catch (error) {
    errorMessage.value = readableError(error)
  }
}

async function loadNegotiations(preferredId?: number): Promise<void> {
  negotiations.value = await listSellerNegotiations()
  const targetId = preferredId ?? selectedNegotiation.value?.negotiation.id
  const nextNegotiation =
    negotiations.value.find((item) => item.id === targetId) ??
    negotiations.value[0] ??
    null
  if (nextNegotiation === null) {
    selectedNegotiation.value = null
    return
  }
  if (
    activeSection.value === 'negotiations'
    || selectedNegotiation.value?.negotiation.id === nextNegotiation.id
  ) {
    selectedNegotiation.value = await getSellerNegotiation(nextNegotiation.id)
  }
}

async function showNegotiations(preferredId?: number): Promise<void> {
  await navigateToSection('negotiations')
  errorMessage.value = ''
  successMessage.value = ''
  try {
    await loadNegotiations(preferredId)
  } catch (error) {
    errorMessage.value = readableError(error)
  }
}

async function selectNegotiation(negotiation: SellerNegotiationSummary): Promise<void> {
  await showNegotiations(negotiation.id)
}

async function openApproval(approvalId: number): Promise<void> {
  await navigateToSection('approvals')
  await loadApprovals(approvalId)
}

function selectApproval(approval: SellerApproval): void {
  selectedApproval.value = approval
  approvalComment.value = approval.seller_comment ?? ''
  if (pendingReviewRequest.value?.approvalId !== approval.id) {
    pendingReviewRequest.value = null
  }
  errorMessage.value = ''
  successMessage.value = ''
}

async function navigateToSection(section: SellerSection): Promise<void> {
  const routeNames: Record<SellerSection, string> = {
    products: 'seller-products',
    approvals: 'seller-approvals',
    negotiations: 'seller-negotiations',
  }
  if (route.name !== routeNames[section]) {
    await router.push({ name: routeNames[section] })
  }
}

async function reviewApproval(action: 'approve' | 'reject'): Promise<void> {
  const approval = selectedApproval.value
  if (!approval || approval.status !== 'PENDING') return
  reviewing.value = true
  errorMessage.value = ''
  successMessage.value = ''
  try {
    if (
      pendingReviewRequest.value?.approvalId !== approval.id ||
      pendingReviewRequest.value.action !== action
    ) {
      pendingReviewRequest.value = {
        approvalId: approval.id,
        action,
        requestId: `${action}-${crypto.randomUUID()}`,
      }
    }
    const saved = await reviewSellerApproval(
      approval.id,
      action,
      pendingReviewRequest.value.requestId,
      approvalComment.value.trim() || null,
    )
    await loadApprovals(saved.id)
    await loadNegotiations(approval.session_id)
    pendingReviewRequest.value = null
    successMessage.value = action === 'approve' ? '审批已同意。' : '审批已拒绝。'
  } catch (error) {
    errorMessage.value = readableError(error)
    if (error instanceof ApiError && error.status === 409) {
      await loadApprovals(approval.id)
      if (selectedApproval.value?.status !== 'PENDING') {
        pendingReviewRequest.value = null
      }
    }
  } finally {
    reviewing.value = false
  }
}

function approvalStatusLabel(status: ApprovalStatus): string {
  return {
    PENDING: '待处理',
    APPROVED: '已同意',
    REJECTED: '已拒绝',
    CANCELLED: '已取消',
    EXPIRED: '已失效',
  }[status]
}

function followupStatusLabel(status: SellerApproval['followup_status']): string {
  if (status === null) return '无需通知'
  return {
    PENDING: '等待通知买家',
    SENT: '已通知买家',
    FAILED: '通知失败，等待重试',
  }[status]
}

function negotiationStatusLabel(status: NegotiationStatus): string {
  return {
    ACTIVE: '协商中',
    WAITING_APPROVAL: '等待卖家审批',
    AGREED: '意向已达成',
    CLOSED: '已结束',
  }[status]
}

function formatDate(value: string | null): string {
  if (!value) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(new Date(value))
}

function shippingLabel(approval: SellerApproval): string {
  if (approval.offer.shipping_paid_by === 'buyer') return '买家承担运费'
  return `卖家承担运费 ${approval.offer.shipping_cost ?? '金额未知'} 元`
}

function deliveryLabel(approval: SellerApproval): string {
  const method = approval.offer.additional_terms.delivery_method
  if (method === 'pickup') return '当面自提'
  if (method === 'shipping') return '快递配送'
  return '未指定配送方式'
}

function offerShippingLabel(offer: SellerNegotiationOffer): string {
  if (offer.shipping_paid_by === 'buyer') return '买家承担运费'
  return `卖家承担运费 ${offer.shipping_cost ?? '金额未知'} 元`
}

function offerStatusLabel(status: SellerNegotiationOffer['status']): string {
  return {
    PROPOSED: '已提出',
    ACCEPTED: '已接受',
    REJECTED: '已拒绝',
    WITHDRAWN: '已撤回',
  }[status]
}

function messageRoleLabel(role: SellerNegotiationDetail['messages'][number]['role']): string {
  return { BUYER: '买家', AGENT: 'Seller Agent', SYSTEM: '系统' }[role]
}

async function saveProduct(): Promise<void> {
  saving.value = true
  errorMessage.value = ''
  successMessage.value = ''
  const productPayload = {
    title: form.title,
    description: form.description,
    listed_price: form.listedPrice,
    status: form.status,
  }
  const policyPayload = {
    minimum_net_price: form.minimumNetPrice,
    auto_accept_threshold: form.autoAcceptThreshold,
    negotiation_style: form.negotiationStyle,
    max_rounds: form.maxRounds,
  }
  const existingProduct = selectedProduct.value
  const creating = existingProduct === null

  try {
    let saved: SellerProduct
    if (existingProduct === null) {
      saved = await createProduct(productPayload, policyPayload)
    } else {
      const productId = existingProduct.id
      await updateProduct(productId, productPayload)
      saved = await updatePolicy(productId, {
        ...policyPayload,
        expected_version: existingProduct.policy.version,
      })
    }
    await loadProducts(saved.id)
    successMessage.value = creating ? '商品已创建。' : '商品与协商策略已保存。'
  } catch (error) {
    errorMessage.value = readableError(error)
    if (error instanceof ApiError && error.status === 409) {
      await loadProducts(selectedProduct.value?.id)
    }
  } finally {
    saving.value = false
  }
}

function readableError(error: unknown): string {
  if (error instanceof Error) return error.message
  return '请求失败，请检查后端与数据库状态。'
}

async function pollSellerUpdates(): Promise<void> {
  if (
    auth.currentUser.value === null
    || polling.value
    || saving.value
    || reviewing.value
  ) return
  polling.value = true
  try {
    await Promise.all([
      // 后台刷新不能覆盖卖家正在输入但尚未提交的审批意见。
      loadApprovals(selectedApproval.value?.id, true),
      loadNegotiations(selectedNegotiation.value?.negotiation.id),
    ])
  } catch {
    // 后台轮询失败时保留当前页面，下一个周期会继续同步。
  } finally {
    polling.value = false
  }
}

watch(activeSection, (section) => {
  errorMessage.value = ''
  successMessage.value = ''
  if (section === 'approvals') {
    approvalComment.value = selectedApproval.value?.seller_comment ?? ''
    void loadApprovals().catch((error: unknown) => {
      errorMessage.value = readableError(error)
    })
  } else if (section === 'negotiations') {
    void loadNegotiations().catch((error: unknown) => {
      errorMessage.value = readableError(error)
    })
  }
})

onMounted(() => {
  void loadWorkspace()
  sellerPollTimer = window.setInterval(
    () => void pollSellerUpdates(),
    SELLER_POLL_INTERVAL_MS,
  )
})

onUnmounted(() => {
  if (sellerPollTimer !== undefined) {
    window.clearInterval(sellerPollTimer)
  }
})
</script>

<template>
  <section class="seller-page">
    <div v-if="loading" class="loading-card">正在读取卖家信息…</div>

    <div v-else class="seller-workspace">
      <aside class="seller-sidebar">
        <div class="seller-profile">
          <div>
            <p class="eyebrow">SELLER</p>
            <strong>{{ auth.currentUser.value?.display_name }}</strong>
            <small v-if="auth.currentUser.value">
              @{{ auth.currentUser.value.username }}
            </small>
          </div>
        </div>
        <div class="seller-section-tabs">
          <RouterLink
            :to="{ name: 'seller-products' }"
            :data-active="activeSection === 'products'"
          >
            商品管理
          </RouterLink>
          <RouterLink
            :to="{ name: 'seller-approvals' }"
            :data-active="activeSection === 'approvals'"
          >
            报价审批
            <span>{{ approvals.filter((item) => item.status === 'PENDING').length }}</span>
          </RouterLink>
          <RouterLink
            :to="{ name: 'seller-negotiations' }"
            :data-active="activeSection === 'negotiations'"
          >
            协商会话
            <span>{{ negotiations.length }}</span>
          </RouterLink>
        </div>

        <template v-if="activeSection === 'products'">
          <button class="new-product-button" type="button" @click="startNewProduct">
            ＋ 新建商品
          </button>
          <p v-if="products.length === 0" class="empty-note">还没有商品，请先新建。</p>
          <button
            v-for="product in products"
            :key="product.id"
            class="product-list-item"
            :data-active="selectedProduct?.id === product.id"
            type="button"
            @click="selectProduct(product)"
          >
            <span>{{ product.title }}</span>
            <small>#{{ product.id }} · {{ product.status }}</small>
          </button>
        </template>

        <template v-else-if="activeSection === 'approvals'">
          <button class="new-product-button" type="button" @click="showApprovals">
            刷新审批列表
          </button>
          <p v-if="approvals.length === 0" class="empty-note">当前没有审批记录。</p>
          <button
            v-for="approval in approvals"
            :key="approval.id"
            class="approval-list-item"
            :data-active="selectedApproval?.id === approval.id"
            type="button"
            @click="selectApproval(approval)"
          >
            <span>{{ approval.product_title }}</span>
            <small>
              {{ approval.buyer_display_name }} · ¥{{ approval.offer.price }} ·
              {{ approvalStatusLabel(approval.status) }}
            </small>
          </button>
        </template>

        <template v-else>
          <button class="new-product-button" type="button" @click="showNegotiations()">
            刷新会话列表
          </button>
          <p v-if="negotiations.length === 0" class="empty-note">当前没有协商会话。</p>
          <button
            v-for="item in negotiations"
            :key="item.id"
            class="negotiation-list-item"
            :data-active="selectedNegotiation?.negotiation.id === item.id"
            type="button"
            @click="selectNegotiation(item)"
          >
            <span>{{ item.product_title }}</span>
            <small>
              {{ item.buyer_display_name }} · #{{ item.id }} ·
              {{ negotiationStatusLabel(item.status) }}
              <template v-if="item.current_offer"> · ¥{{ item.current_offer.price }}</template>
            </small>
          </button>
        </template>
      </aside>

      <form
        v-if="activeSection === 'products'"
        class="product-editor"
        @submit.prevent="saveProduct"
      >
        <div class="editor-heading">
          <div>
            <p class="eyebrow">PRODUCT & POLICY</p>
            <h1>{{ selectedProduct ? `编辑商品 #${selectedProduct.id}` : '新建商品' }}</h1>
          </div>
          <span v-if="selectedProduct" class="version-badge">
            策略版本 v{{ selectedProduct.policy.version }}
          </span>
        </div>

        <fieldset>
          <legend>公开商品信息</legend>
          <label>
            <span>商品标题</span>
            <input v-model="form.title" maxlength="200" required />
          </label>
          <label class="wide-field">
            <span>商品描述</span>
            <textarea v-model="form.description" maxlength="5000" rows="4" required></textarea>
          </label>
          <label>
            <span>公开标价</span>
            <input v-model="form.listedPrice" min="0" step="0.01" type="number" required />
          </label>
          <label>
            <span>商品状态</span>
            <select v-model="form.status">
              <option value="DRAFT">草稿</option>
              <option value="AVAILABLE">上架</option>
              <option value="UNAVAILABLE">下架</option>
            </select>
          </label>
        </fieldset>

        <fieldset class="policy-fieldset">
          <legend>私有协商策略</legend>
          <p class="privacy-note wide-field">
            以下规则只供卖家 Agent 决策，公开商品接口和买家页面不会返回这些字段。
          </p>
          <label>
            <span>最低净收入</span>
            <input
              v-model="form.minimumNetPrice"
              min="0"
              step="0.01"
              type="number"
              required
            />
          </label>
          <label>
            <span>自动接受阈值</span>
            <input
              v-model="form.autoAcceptThreshold"
              min="0"
              step="0.01"
              type="number"
              required
            />
          </label>
          <label>
            <span>协商风格</span>
            <select v-model="form.negotiationStyle">
              <option value="FIRM">强硬</option>
              <option value="BALANCED">平衡</option>
              <option value="FLEXIBLE">灵活</option>
            </select>
          </label>
          <label>
            <span>最大轮数</span>
            <input v-model="form.maxRounds" min="1" max="100" type="number" required />
          </label>
        </fieldset>

        <p v-if="errorMessage" class="error-banner" role="alert">{{ errorMessage }}</p>
        <p v-else-if="successMessage" class="outcome-banner">{{ successMessage }}</p>
        <div class="editor-actions">
          <button type="submit" :disabled="saving">
            {{ saving ? '保存中…' : '保存商品' }}
          </button>
        </div>
      </form>

      <section v-else-if="activeSection === 'approvals'" class="approval-editor">
        <div v-if="!selectedApproval" class="approval-empty">
          <p class="eyebrow">APPROVALS</p>
          <h1>暂无报价审批</h1>
          <p>{{ errorMessage || 'Agent 提交需要人工确认的买家报价后，会显示在这里。' }}</p>
        </div>

        <template v-else>
          <div class="editor-heading">
            <div>
              <p class="eyebrow">APPROVAL #{{ selectedApproval.id }}</p>
              <h1>{{ selectedApproval.product_title }}</h1>
            </div>
            <span class="approval-status" :data-status="selectedApproval.status">
              {{ approvalStatusLabel(selectedApproval.status) }}
            </span>
          </div>

          <button
            class="inline-link-button"
            type="button"
            @click="showNegotiations(selectedApproval.session_id)"
          >
            查看完整协商会话 #{{ selectedApproval.session_id }}
          </button>

          <div class="approval-meta-grid">
            <div>
              <span>买家</span>
              <strong>{{ selectedApproval.buyer_display_name }}</strong>
            </div>
            <div>
              <span>协商会话</span>
              <strong>#{{ selectedApproval.session_id }}</strong>
            </div>
            <div>
              <span>策略版本</span>
              <strong>v{{ selectedApproval.policy_version }}</strong>
            </div>
            <div>
              <span>提交时间</span>
              <strong>{{ formatDate(selectedApproval.created_at) }}</strong>
            </div>
            <div>
              <span>有效期至</span>
              <strong>{{ formatDate(selectedApproval.expires_at) }}</strong>
            </div>
          </div>

          <div class="approval-offer-card">
            <p class="eyebrow">BUYER OFFER</p>
            <strong>¥{{ selectedApproval.offer.price }}</strong>
            <span>{{ shippingLabel(selectedApproval) }}</span>
            <span>配送方式：{{ deliveryLabel(selectedApproval) }}</span>
            <span>
              卖家承担的其他优惠：¥{{ selectedApproval.offer.seller_borne_discount }}
            </span>
          </div>

          <div class="approval-reason">
            <span>系统申请原因</span>
            <p>{{ selectedApproval.reason }}</p>
          </div>

          <label v-if="selectedApproval.status === 'PENDING'" class="approval-comment">
            <span>卖家意见（选填）</span>
            <textarea
              v-model="approvalComment"
              maxlength="2000"
              placeholder="说明同意或拒绝的原因，后续将用于通知买家。"
              rows="4"
            ></textarea>
          </label>
          <div v-else class="approval-reason">
            <span>卖家意见</span>
            <p>{{ selectedApproval.seller_comment || '未填写' }}</p>
          </div>

          <p class="approval-warning">
            同意只表示卖家授权当前报价，不代表交易已经成交；系统通知买家后，仍需买家明确确认才会记录交易意向。
          </p>
          <p v-if="selectedApproval.status !== 'PENDING'" class="approval-warning">
            后续状态：{{ followupStatusLabel(selectedApproval.followup_status) }}
          </p>
          <p v-if="errorMessage" class="error-banner" role="alert">{{ errorMessage }}</p>
          <p v-else-if="successMessage" class="outcome-banner">{{ successMessage }}</p>
          <div v-if="selectedApproval.status === 'PENDING'" class="approval-actions">
            <button
              class="reject-button"
              type="button"
              :disabled="reviewing"
              @click="reviewApproval('reject')"
            >
              拒绝报价
            </button>
            <button
              type="button"
              :disabled="reviewing"
              @click="reviewApproval('approve')"
            >
              {{ reviewing ? '处理中…' : '同意报价' }}
            </button>
          </div>
        </template>
      </section>

      <section v-else class="negotiation-editor">
        <div v-if="!selectedNegotiation" class="approval-empty">
          <p class="eyebrow">NEGOTIATIONS</p>
          <h1>暂无协商会话</h1>
          <p>{{ errorMessage || '买家针对卖家商品发起协商后，会显示在这里。' }}</p>
        </div>

        <template v-else>
          <div class="editor-heading">
            <div>
              <p class="eyebrow">
                与 {{ selectedNegotiation.negotiation.buyer_display_name }} 的协商
                · #{{ selectedNegotiation.negotiation.id }}
              </p>
              <h1>{{ selectedNegotiation.negotiation.product_title }}</h1>
            </div>
            <span
              class="negotiation-status"
              :data-status="selectedNegotiation.negotiation.status"
            >
              {{ negotiationStatusLabel(selectedNegotiation.negotiation.status) }}
            </span>
          </div>

          <div class="approval-meta-grid">
            <div>
              <span>买家</span>
              <strong>{{ selectedNegotiation.negotiation.buyer_display_name }}</strong>
            </div>
            <div>
              <span>商品编号</span>
              <strong>#{{ selectedNegotiation.negotiation.product_id }}</strong>
            </div>
            <div>
              <span>协商轮次</span>
              <strong>{{ selectedNegotiation.negotiation.round_count }} 轮</strong>
            </div>
            <div>
              <span>创建时间</span>
              <strong>{{ formatDate(selectedNegotiation.negotiation.created_at) }}</strong>
            </div>
            <div>
              <span>最近更新</span>
              <strong>{{ formatDate(selectedNegotiation.negotiation.updated_at) }}</strong>
            </div>
          </div>

          <div class="negotiation-offer-grid">
            <section class="negotiation-offer-card">
              <p class="eyebrow">CURRENT OFFER</p>
              <template v-if="selectedNegotiation.negotiation.current_offer">
                <strong>¥{{ selectedNegotiation.negotiation.current_offer.price }}</strong>
                <span>{{ offerShippingLabel(selectedNegotiation.negotiation.current_offer) }}</span>
                <span>
                  {{ selectedNegotiation.negotiation.current_offer.proposer === 'BUYER'
                    ? '买家报价'
                    : 'Agent 还价' }}
                  · {{ offerStatusLabel(selectedNegotiation.negotiation.current_offer.status) }}
                </span>
              </template>
              <span v-else>暂无正式报价</span>
            </section>
            <section class="negotiation-offer-card confirmed-card">
              <p class="eyebrow">CONFIRMED INTENT</p>
              <template v-if="selectedNegotiation.negotiation.confirmed_offer">
                <strong>¥{{ selectedNegotiation.negotiation.confirmed_offer.price }}</strong>
                <span>
                  买家确认于 {{ formatDate(selectedNegotiation.negotiation.confirmed_at) }}
                </span>
                <span>仅代表交易意向，不代表付款或实际成交</span>
              </template>
              <span v-else>买家尚未确认交易意向</span>
            </section>
          </div>

          <section
            v-if="selectedNegotiation.negotiation.latest_approval"
            class="negotiation-approval-summary"
          >
            <div>
              <p class="eyebrow">LATEST APPROVAL</p>
              <strong>
                {{ approvalStatusLabel(selectedNegotiation.negotiation.latest_approval.status) }}
                · {{ followupStatusLabel(
                  selectedNegotiation.negotiation.latest_approval.followup_status,
                ) }}
              </strong>
            </div>
            <button
              class="inline-link-button"
              type="button"
              @click="openApproval(selectedNegotiation.negotiation.latest_approval.id)"
            >
              查看审批 #{{ selectedNegotiation.negotiation.latest_approval.id }}
            </button>
          </section>

          <div class="negotiation-detail-grid">
            <section>
              <div class="detail-section-heading">
                <h2>消息记录</h2>
                <span>{{ selectedNegotiation.messages.length }} 条</span>
              </div>
              <div class="seller-message-timeline">
                <p v-if="selectedNegotiation.messages.length === 0" class="empty-note">
                  暂无消息。
                </p>
                <article
                  v-for="message in selectedNegotiation.messages"
                  :key="message.id"
                  :data-role="message.role"
                >
                  <div>
                    <strong>{{ messageRoleLabel(message.role) }}</strong>
                    <time>{{ formatDate(message.created_at) }}</time>
                  </div>
                  <p>{{ message.content }}</p>
                </article>
              </div>
            </section>

            <section>
              <div class="detail-section-heading">
                <h2>报价时间线</h2>
                <span>{{ selectedNegotiation.offers.length }} 份</span>
              </div>
              <div class="offer-timeline">
                <p v-if="selectedNegotiation.offers.length === 0" class="empty-note">
                  暂无正式报价。
                </p>
                <article v-for="offer in selectedNegotiation.offers" :key="offer.id">
                  <div>
                    <strong>#{{ offer.id }} · ¥{{ offer.price }}</strong>
                    <span>{{ offerStatusLabel(offer.status) }}</span>
                  </div>
                  <small>
                    {{ offer.proposer === 'BUYER' ? '买家报价' : 'Agent 还价' }} ·
                    {{ offerShippingLabel(offer) }} · {{ formatDate(offer.created_at) }}
                  </small>
                </article>
              </div>
            </section>
          </div>

          <p v-if="errorMessage" class="error-banner" role="alert">{{ errorMessage }}</p>
        </template>
      </section>
    </div>
  </section>
</template>
