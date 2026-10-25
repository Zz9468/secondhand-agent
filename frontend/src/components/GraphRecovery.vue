<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { requestJson } from '../api/client'

interface RecoveryItem {
  target: string
  kind: 'RUN' | 'NOTIFICATION'
  session_id: number
  product_title: string
  status: string
  active_ms?: number
  calls?: number
  unknown_usage?: number
  can_terminate: boolean
}
const items = ref<RecoveryItem[]>([])
const reason = ref('')
const busy = ref(false)
const feedback = ref('')
// Preserve the request id on network failure; a retry cannot grant multiple extra attempts.
const pending = ref<{ target: string; action: string; reason: string; request_id: string } | null>(null)

async function refresh() {
  try {
    items.value = (await requestJson<{ items: RecoveryItem[] }>('/api/seller/graph-recovery')).items
  } catch (error) {
    feedback.value = error instanceof Error ? error.message : '读取恢复任务失败'
  }
}
async function act(item: RecoveryItem, action: string) {
  if (!reason.value.trim()) { feedback.value = '请填写处置原因'; return }
  busy.value = true
  if (!pending.value || pending.value.target !== item.target || pending.value.action !== action
      || pending.value.reason !== reason.value) {
    pending.value = { target: item.target, action, reason: reason.value, request_id: crypto.randomUUID() }
  }
  try {
    await requestJson(`/api/seller/graph-recovery/${encodeURIComponent(item.target)}`, {
      method: 'POST', body: JSON.stringify(pending.value),
    })
    pending.value = null
    feedback.value = action === 'TERMINATE'
      ? '已登记安全终止；已提交的业务结果仍会通知买家。'
      : '已登记恢复操作，后台将继续处理。'
    await refresh()
  } catch (error) {
    feedback.value = error instanceof Error ? error.message : '恢复操作失败'
  } finally { busy.value = false }
}
onMounted(refresh)
</script>

<template>
  <section class="graph-recovery">
    <h2>协商任务恢复</h2>
    <p>重试沿用原任务和剩余预算。安全终止停止后续处理，并保留已提交结果。</p>
    <button type="button" :disabled="busy" @click="refresh">刷新</button>
    <p v-if="feedback" role="status">{{ feedback }}</p>
    <label>处置原因 <input v-model="reason" maxlength="500" placeholder="填写本次恢复原因" /></label>
    <p v-if="!items.length">当前没有需要人工处置的协商任务。</p>
    <article v-for="item in items" :key="item.target">
      <strong>{{ item.product_title }} · 会话 #{{ item.session_id }}</strong>
      <p>{{ item.kind === 'NOTIFICATION' ? '结果通知待恢复' : '协商处理待恢复' }} · {{ item.status }}</p>
      <p v-if="item.calls !== undefined">
        已调用 {{ item.calls }} 次 · 活动时间 {{ ((item.active_ms ?? 0) / 1000).toFixed(1) }} 秒
        · 用量未知 {{ item.unknown_usage }} 次
      </p>
      <button type="button" :disabled="busy" @click="act(item, item.kind === 'NOTIFICATION' ? 'NOTIFY' : 'RETRY')">重试一次</button>
      <button v-if="item.can_terminate" type="button" :disabled="busy" @click="act(item, 'TERMINATE')">安全终止</button>
    </article>
  </section>
</template>

<style scoped>
.graph-recovery { padding: 1.25rem; margin-bottom: 1rem; border: 1px solid #cbd5e1; border-radius: 12px; }
article { border-top: 1px solid #e2e8f0; padding: 1rem 0; }
button { margin: .5rem .5rem .5rem 0; }
label { display: block; margin-top: 1rem; }
input { width: min(100%, 30rem); }
</style>
