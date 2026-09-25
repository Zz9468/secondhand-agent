<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import {
  getSellerIdentity,
  loginSeller,
  logoutSeller,
  type SellerIdentity,
} from '../api/auth'
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

const identity = ref<SellerIdentity | null>(null)
const products = ref<SellerProduct[]>([])
const selectedProduct = ref<SellerProduct | null>(null)
const username = ref('demo-seller')
const password = ref('')
const loading = ref(true)
const saving = ref(false)
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
    await loadProducts()
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
    await loadProducts()
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
      <p>登录后可管理自己的商品和私有协商策略。</p>
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
      </aside>

      <form class="product-editor" @submit.prevent="saveProduct">
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
    </div>
  </section>
</template>
