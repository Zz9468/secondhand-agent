<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  listBuyerNegotiations,
  type BuyerNegotiationSummary,
  type NegotiationStatus,
} from '../api/negotiations'

const negotiations = ref<BuyerNegotiationSummary[]>([])
const loading = ref(true)
const errorMessage = ref('')

async function loadNegotiations(): Promise<void> {
  loading.value = true
  errorMessage.value = ''
  try {
    negotiations.value = await listBuyerNegotiations()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '协商记录加载失败，请稍后重试。'
  } finally {
    loading.value = false
  }
}

function statusLabel(status: NegotiationStatus): string {
  return {
    ACTIVE: '协商中',
    WAITING_APPROVAL: '等待卖家审批',
    AGREED: '已确认交易意向',
    CLOSED: '已结束',
  }[status]
}

function formatTime(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value))
}

onMounted(() => void loadNegotiations())
</script>

<template>
  <section class="buyer-history-page">
    <header class="buyer-history-heading">
      <div>
        <RouterLink class="back-link" to="/buyer">← 返回商品大厅</RouterLink>
        <p class="eyebrow">MY NEGOTIATIONS</p>
        <h1>我的协商</h1>
        <p>恢复进行中的沟通，或回看已经结束的交易意向记录。</p>
      </div>
      <button class="ghost-button" type="button" :disabled="loading" @click="loadNegotiations">
        {{ loading ? '载入中…' : '刷新记录' }}
      </button>
    </header>

    <section v-if="loading" class="loading-card">正在读取协商记录…</section>

    <section v-else-if="errorMessage" class="loading-card error-state">
      <h1>暂时无法读取协商记录</h1>
      <p>{{ errorMessage }}</p>
      <button type="button" @click="loadNegotiations">重新加载</button>
    </section>

    <section v-else-if="negotiations.length === 0" class="buyer-history-empty">
      <p class="eyebrow">NO NEGOTIATIONS YET</p>
      <h2>还没有发起过协商</h2>
      <p>浏览商品详情，并明确点击“与卖家协商”后，记录会出现在这里。</p>
      <RouterLink class="primary-link" to="/buyer">去逛商品大厅</RouterLink>
    </section>

    <section v-else class="buyer-negotiation-list" aria-label="买家协商记录">
      <RouterLink
        v-for="item in negotiations"
        :key="item.id"
        class="buyer-negotiation-card"
        :to="{ name: 'negotiation', params: { sessionId: item.id } }"
      >
        <div class="buyer-negotiation-main">
          <span class="seller-avatar">{{ item.seller.display_name.slice(0, 1) }}</span>
          <div>
            <small>{{ item.seller.display_name }} · 会话 #{{ item.id }}</small>
            <strong>{{ item.product_title }}</strong>
            <time>更新于 {{ formatTime(item.updated_at) }}</time>
          </div>
        </div>
        <div class="buyer-negotiation-meta">
          <span class="negotiation-status" :data-status="item.status">
            {{ statusLabel(item.status) }}
          </span>
          <small>{{ item.round_count }} 轮沟通</small>
          <b aria-hidden="true">→</b>
        </div>
      </RouterLink>
    </section>
  </section>
</template>
