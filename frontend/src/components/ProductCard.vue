<script setup lang="ts">
import type { PublicProduct } from '../api/products'

defineProps<{
  product: PublicProduct
}>()

function formatPrice(value: string): string {
  return new Intl.NumberFormat('zh-CN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(Number(value))
}
</script>

<template>
  <article class="catalog-product-card">
    <RouterLink
      class="catalog-product-visual"
      :to="{ name: 'product-detail', params: { productId: product.id } }"
      :aria-label="`查看商品：${product.title}`"
    >
      <span>{{ String(product.id).padStart(2, '0').slice(-2) }}</span>
      <small>SECONDHAND</small>
    </RouterLink>
    <div class="catalog-product-body">
      <RouterLink
        class="seller-inline-link"
        :to="{ name: 'seller-public', params: { sellerId: product.seller.id } }"
      >
        {{ product.seller.display_name }}
      </RouterLink>
      <RouterLink
        class="catalog-product-title"
        :to="{ name: 'product-detail', params: { productId: product.id } }"
      >
        {{ product.title }}
      </RouterLink>
      <p>{{ product.description }}</p>
      <div class="catalog-product-footer">
        <strong>¥{{ formatPrice(product.listed_price) }}</strong>
        <RouterLink
          :to="{ name: 'product-detail', params: { productId: product.id } }"
        >
          查看详情
        </RouterLink>
      </div>
    </div>
  </article>
</template>
