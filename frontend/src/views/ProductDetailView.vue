<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import { getPublicProduct, type PublicProduct } from '../api/products'

const route = useRoute()
const product = ref<PublicProduct | null>(null)
const loading = ref(true)
const errorMessage = ref('')

const productId = computed(() => Number(route.params.productId))

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
        <p class="browse-only-note">
          当前页面只读取公开商品信息，不会创建协商会话，也不会展示卖家的私有定价规则。
        </p>
      </div>
    </article>
  </section>
</template>
