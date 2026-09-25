<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import {
  getSellerIdentity,
  loginSeller,
  logoutSeller,
  type SellerIdentity,
} from '../api/auth'
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

type SellerSection = 'products' | 'approvals'

const identity = ref<SellerIdentity | null>(null)
const products = ref<SellerProduct[]>([])
const selectedProduct = ref<SellerProduct | null>(null)
const approvals = ref<SellerApproval[]>([])
const selectedApproval = ref<SellerApproval | null>(null)
const activeSection = ref<SellerSection>('products')
const approvalComment = ref('')
const pendingReviewRequest = ref<{
  approvalId: number
  action: 'approve' | 'reject'
  requestId: string
} | null>(null)
const username = ref('demo-seller')
const password = ref('')
const loading = ref(true)
const saving = ref(false)
const reviewing = ref(false)
const errorMessage = ref('')
const successMessage = ref('')
const form = reactive<ProductForm>(blankForm())

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

async function restoreSession(): Promise<void> {
  loading.value = true
  try {
    identity.value = await getSellerIdentity()
    await Promise.all([loadProducts(), loadApprovals()])
  } catch (error) {
    if (!(error instanceof ApiError && error.status === 401)) {
      errorMessage.value = readableError(error)
    }
  } finally {
    loading.value = false
  }
}

async function submitLogin(): Promise<void> {
  errorMessage.value = ''
  loading.value = true
  try {
    identity.value = await loginSeller(username.value, password.value)
    password.value = ''
    await Promise.all([loadProducts(), loadApprovals()])
  } catch (error) {
    errorMessage.value = readableError(error)
  } finally {
    loading.value = false
  }
}

async function signOut(): Promise<void> {
  await logoutSeller()
  identity.value = null
  products.value = []
  approvals.value = []
  selectedApproval.value = null
  activeSection.value = 'products'
  startNewProduct()
}

async function loadProducts(preferredId?: number): Promise<void> {
  products.value = await listSellerProducts()
  const targetId = preferredId ?? selectedProduct.value?.id
  const target = products.value.find((product) => product.id === targetId)
  if (target) {
    selectProduct(target)
  } else {
    startNewProduct()
  }
}

function startNewProduct(): void {
  activeSection.value = 'products'
  selectedProduct.value = null
  Object.assign(form, blankForm())
  errorMessage.value = ''
  successMessage.value = ''
}

function selectProduct(product: SellerProduct): void {
  activeSection.value = 'products'
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

async function loadApprovals(preferredId?: number): Promise<void> {
  approvals.value = await listSellerApprovals()
  const targetId = preferredId ?? selectedApproval.value?.id
  const nextApproval =
    approvals.value.find((approval) => approval.id === targetId) ??
    approvals.value.find((approval) => approval.status === 'PENDING') ??
    approvals.value[0] ??
    null
  selectedApproval.value = nextApproval
  if (activeSection.value === 'approvals') {
    approvalComment.value = nextApproval?.seller_comment ?? ''
  }
}

async function showApprovals(): Promise<void> {
  activeSection.value = 'approvals'
  errorMessage.value = ''
  successMessage.value = ''
  try {
    await loadApprovals()
  } catch (error) {
    errorMessage.value = readableError(error)
  }
}

function selectApproval(approval: SellerApproval): void {
  activeSection.value = 'approvals'
  selectedApproval.value = approval
  approvalComment.value = approval.seller_comment ?? ''
  if (pendingReviewRequest.value?.approvalId !== approval.id) {
    pendingReviewRequest.value = null
  }
  errorMessage.value = ''
  successMessage.value = ''
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

onMounted(restoreSession)
</script>

<template>
  <section class="seller-page">
    <div v-if="loading" class="loading-card">正在读取卖家信息…</div>

    <form v-else-if="!identity" class="login-card" @submit.prevent="submitLogin">
      <p class="eyebrow">SELLER CONSOLE</p>
      <h1>卖家登录</h1>
      <p>登录后可管理商品、私有协商策略和待处理报价审批。</p>
      <label>
        <span>用户名</span>
        <input v-model="username" autocomplete="username" required />
      </label>
      <label>
        <span>密码</span>
        <input
          v-model="password"
          autocomplete="current-password"
          minlength="12"
          type="password"
          required
        />
      </label>
      <p v-if="errorMessage" class="error-banner" role="alert">{{ errorMessage }}</p>
      <button type="submit">登录</button>
    </form>

    <div v-else class="seller-workspace">
      <aside class="seller-sidebar">
        <div class="seller-profile">
          <div>
            <p class="eyebrow">SELLER</p>
            <strong>{{ identity.username }}</strong>
          </div>
          <button class="ghost-button" type="button" @click="signOut">退出</button>
        </div>
        <div class="seller-section-tabs">
          <button
            :data-active="activeSection === 'products'"
            type="button"
            @click="activeSection = 'products'"
          >
            商品管理
          </button>
          <button
            :data-active="activeSection === 'approvals'"
            type="button"
            @click="showApprovals"
          >
            报价审批
            <span>{{ approvals.filter((item) => item.status === 'PENDING').length }}</span>
          </button>
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

        <template v-else>
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
            <small>¥{{ approval.offer.price }} · {{ approvalStatusLabel(approval.status) }}</small>
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

      <section v-else class="approval-editor">
        <div v-if="!selectedApproval" class="approval-empty">
          <p class="eyebrow">APPROVALS</p>
          <h1>暂无报价审批</h1>
          <p>Agent 提交需要人工确认的买家报价后，会显示在这里。</p>
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

          <div class="approval-meta-grid">
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
            <span>Agent 申请原因</span>
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
            同意只表示卖家授权当前这份报价，不代表交易已经成交；买家最终确认将在后续阶段完成。
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
    </div>
  </section>
</template>
