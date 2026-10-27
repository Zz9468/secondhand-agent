<script setup lang="ts">
import type { PendingOperation } from '../state/pendingOperations'
defineProps<{ operations: PendingOperation[]; busy: boolean; notice: string; error: string }>()
defineEmits<{ check: [op: PendingOperation]; retry: [op: PendingOperation]; newIntent: [] }>()
const names = { message: '消息/方案', confirm: '确认交易意向', close: '结束协商', recovery: '恢复操作', approve: '同意报价', reject: '拒绝报价' }
</script>
<template>
  <section class="pending-operations" aria-label="原操作核对">
    <p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="error" class="error-banner" role="alert">{{ error }}</p>
    <article v-for="op in operations" :key="op.requestId" class="operation-status">
      <p>{{ names[op.kind] }} · {{ op.target }} ·
        {{ op.status === 'accepted' ? (op.kind === 'recovery' ? '已登记恢复操作' : '已核对原操作，服务端已接受') : op.status === 'rejected' ? '本次提交未获接受，请刷新状态后操作' : op.status === 'blocked' ? '此次重试已被拒绝，原操作仍需核对' : '结果待核实，原操作已保存' }}</p>
      <small>请求 {{ op.requestId }}</small>
      <template v-if="op.status === 'unknown' || op.status === 'blocked'">
        <button type="button" :disabled="busy" @click="$emit('check', op)">核对原操作</button>
        <button v-if="op.status === 'unknown'" type="button" :disabled="busy" @click="$emit('retry', op)">重试原操作</button>
      </template>
    </article>
    <button v-if="operations.some(op => op.status === 'unknown' || op.status === 'blocked')" type="button" :disabled="busy" @click="$emit('newIntent')">另行发起新操作</button>
  </section>
</template>
