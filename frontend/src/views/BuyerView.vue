<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { listPublicProducts, type PublicProduct } from '../api/products'
import { listPublicSellers, type PublicSeller } from '../api/sellers'
import ProductCard from '../components/ProductCard.vue'

const products = ref<PublicProduct[]>([])
const sellers = ref<PublicSeller[]>([])
const loading = ref(true)
const errorMessage = ref('')

async function loadCatalog(): Promise<void> {
  loading.value = true
  errorMessage.value = ''
  try {
    const [productItems, sellerItems] = await Promise.all([
      listPublicProducts(),
      listPublicSellers(),
    ])
    products.value = productItems
    sellers.value = sellerItems
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '商品大厅加载失败，请稍后重试。'
  } finally {
    loading.value = false
  }
}

onMounted(() => void loadCatalog())
</script>

<template>
  <section class="catalog-page">
    <header class="catalog-hero">
      <div>
        <p class="eyebrow">BUYER MARKETPLACE</p>
        <h1>慢慢看，遇见值得再次拥有的好物。</h1>
        <p>浏览商品和卖家不会创建协商会话。进入详情后，你仍可以安心比较公开信息。</p>
      </div>
      <div class="catalog-hero-actions">
        <RouterLink class="secondary-link" to="/buyer/negotiations">我的协商</RouterLink>
        <button class="ghost-button" type="button" :disabled="loading" @click="loadCatalog">
          {{ loading ? '载入中…' : '刷新大厅' }}
        </button>
      </div>
    </header>

    <section v-if="loading" class="loading-card">正在读取已上架商品…</section>

    <section v-else-if="errorMessage" class="loading-card error-state">
      <h1>暂时无法打开商品大厅</h1>
      <p>{{ errorMessage }}</p>
      <button type="button" @click="loadCatalog">重新加载</button>
    </section>

    <template v-else>
      <section class="seller-strip" aria-labelledby="seller-list-title">
        <div class="section-heading">
          <div>
            <p class="eyebrow">SELLERS</p>
            <h2 id="seller-list-title">正在发布好物的卖家</h2>
          </div>
          <span>{{ sellers.length }} 位卖家</span>
        </div>
        <div v-if="sellers.length" class="seller-card-grid">
          <RouterLink
            v-for="seller in sellers"
            :key="seller.id"
            class="seller-card"
            :to="{ name: 'seller-public', params: { sellerId: seller.id } }"
          >
            <span class="seller-avatar">{{ seller.display_name.slice(0, 1) }}</span>
            <div>
              <strong>{{ seller.display_name }}</strong>
              <small>{{ seller.available_product_count }} 件在售商品</small>
            </div>
            <b aria-hidden="true">→</b>
          </RouterLink>
        </div>
        <p v-else class="catalog-empty-note">暂时还没有卖家发布商品。</p>
      </section>

      <section aria-labelledby="product-list-title">
        <div class="section-heading">
          <div>
            <p class="eyebrow">AVAILABLE NOW</p>
            <h2 id="product-list-title">全部在售商品</h2>
          </div>
          <span>{{ products.length }} 件商品</span>
        </div>
        <div v-if="products.length" class="catalog-product-grid">
          <ProductCard v-for="product in products" :key="product.id" :product="product" />
        </div>
        <div v-else class="catalog-empty-note catalog-empty-card">
          <strong>大厅里暂时没有商品</strong>
          <p>可以切换到卖家模式，发布第一件闲置好物。</p>
        </div>
      </section>
    </template>
  </section>
</template>
