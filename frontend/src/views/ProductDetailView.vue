<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { createNegotiation } from '../api/negotiations'
import { getPublicProduct, type PublicProduct } from '../api/products'
import { useAuth } from '../state/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuth()
const product = ref<PublicProduct | null>(null)
const loading = ref(true)
const startingNegotiation = ref(false)
const errorMessage = ref('')
const actionError = ref('')

const productId = computed(() => Number(route.params.productId))
const isOwnProduct = computed(
  () => product.value?.seller.id === auth.currentUser.value?.id,
)

function formatPrice(value: string): string {
  return new Intl.NumberFormat('zh-CN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(Number(value))
}

async function loadProduct(): Promise<void> {
  loading.value = true
  errorMessage.value = ''
  product.value = null
  if (!Number.isInteger(productId.value) || productId.value <= 0) {
    errorMessage.value = '商品编号无效。'
    loading.value = false
    return
  }
  try {
    product.value = await getPublicProduct(productId.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '商品详情加载失败，请稍后重试。'
  } finally {
    loading.value = false
  }
}

async function startNegotiation(): Promise<void> {
  if (!product.value || isOwnProduct.value || startingNegotiation.value) return
  startingNegotiation.value = true
  actionError.value = ''
  try {
    const result = await createNegotiation(product.value.id)
    await router.push({
      name: 'negotiation',
      params: { sessionId: result.session_id },
    })
  } catch (error) {
    actionError.value = error instanceof Error ? error.message : '发起协商失败，请稍后重试。'
  } finally {
    startingNegotiation.value = false
  }
}

watch(productId, () => void loadProduct(), { immediate: true })
</script>

<template>
  <section class="public-detail-page">
    <RouterLink class="back-link" to="/buyer">← 返回商品大厅</RouterLink>

    <section v-if="loading" class="loading-card">正在读取商品详情…</section>

    <section v-else-if="!product" class="loading-card error-state">
      <h1>无法查看这件商品</h1>
      <p>{{ errorMessage }}</p>
      <button type="button" @click="loadProduct">重新加载</button>
    </section>

    <article v-else class="product-detail-layout">
      <div class="product-detail-visual" aria-hidden="true">
        <span>{{ String(product.id).padStart(2, '0').slice(-2) }}</span>
        <small>SECONDHAND OBJECT</small>
      </div>
      <div class="product-detail-copy">
        <p class="eyebrow">商品 #{{ product.id }} · 已上架</p>
        <h1>{{ product.title }}</h1>
        <p class="product-detail-description">{{ product.description }}</p>
        <div class="product-detail-price">
          <span>公开标价</span>
          <strong>¥{{ formatPrice(product.listed_price) }}</strong>
        </div>
        <RouterLink
          class="seller-detail-link"
          :to="{ name: 'seller-public', params: { sellerId: product.seller.id } }"
        >
          <span class="seller-avatar">{{ product.seller.display_name.slice(0, 1) }}</span>
          <span>
            <small>由这位卖家发布</small>
            <strong>{{ product.seller.display_name }}</strong>
          </span>
          <b aria-hidden="true">查看卖家主页 →</b>
        </RouterLink>
        <button
          v-if="!isOwnProduct"
          class="start-negotiation-button"
          type="button"
          :disabled="startingNegotiation"
          @click="startNegotiation"
        >
          {{ startingNegotiation ? '正在进入协商…' : '与卖家协商' }}
        </button>
        <p v-else class="own-product-note">这是你发布的商品，不能与自己发起协商。</p>
        <p v-if="actionError" class="error-banner" role="alert">{{ actionError }}</p>
        <p class="browse-only-note">
          浏览和刷新本页不会创建会话；只有点击“与卖家协商”才会创建或恢复进行中的会话。
        </p>
      </div>
    </article>
  </section>
</template>
