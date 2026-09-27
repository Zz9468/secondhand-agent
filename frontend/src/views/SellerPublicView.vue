<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import type { PublicProduct } from '../api/products'
import {
  getPublicSeller,
  listPublicSellerProducts,
  type PublicSeller,
} from '../api/sellers'
import ProductCard from '../components/ProductCard.vue'

const route = useRoute()
const seller = ref<PublicSeller | null>(null)
const products = ref<PublicProduct[]>([])
const loading = ref(true)
const errorMessage = ref('')

const sellerId = computed(() => String(route.params.sellerId ?? ''))

async function loadSellerPage(): Promise<void> {
  loading.value = true
  errorMessage.value = ''
  seller.value = null
  products.value = []
  if (!sellerId.value) {
    errorMessage.value = '卖家编号无效。'
    loading.value = false
    return
  }
  try {
    const [sellerDetail, sellerProducts] = await Promise.all([
      getPublicSeller(sellerId.value),
      listPublicSellerProducts(sellerId.value),
    ])
    seller.value = sellerDetail
    products.value = sellerProducts
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '卖家主页加载失败，请稍后重试。'
  } finally {
    loading.value = false
  }
}

watch(sellerId, () => void loadSellerPage(), { immediate: true })
</script>

<template>
  <section class="public-detail-page seller-public-page">
    <RouterLink class="back-link" to="/buyer">← 返回商品大厅</RouterLink>

    <section v-if="loading" class="loading-card">正在读取卖家主页…</section>

    <section v-else-if="!seller" class="loading-card error-state">
      <h1>无法查看这位卖家</h1>
      <p>{{ errorMessage }}</p>
      <button type="button" @click="loadSellerPage">重新加载</button>
    </section>

    <template v-else>
      <header class="seller-public-hero">
        <span class="seller-public-avatar">{{ seller.display_name.slice(0, 1) }}</span>
        <div>
          <p class="eyebrow">PUBLIC SELLER PAGE</p>
          <h1>{{ seller.display_name }}</h1>
          <p>正在展示 {{ seller.available_product_count }} 件已上架商品。</p>
        </div>
      </header>

      <section aria-labelledby="seller-products-title">
        <div class="section-heading">
          <div>
            <p class="eyebrow">LISTINGS</p>
            <h2 id="seller-products-title">这位卖家的在售商品</h2>
          </div>
          <span>{{ products.length }} 件商品</span>
        </div>
        <div class="catalog-product-grid">
          <ProductCard v-for="product in products" :key="product.id" :product="product" />
        </div>
      </section>
    </template>
  </section>
</template>
