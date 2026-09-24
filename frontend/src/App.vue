<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { getHealth, getReadiness } from './api/health'

type CheckState = 'checking' | 'ok' | 'error'

const apiState = ref<CheckState>('checking')
const databaseState = ref<CheckState>('checking')
const detail = ref('正在检查本地服务…')

async function checkServices(): Promise<void> {
  apiState.value = 'checking'
  databaseState.value = 'checking'
  detail.value = '正在检查本地服务…'

  try {
    const health = await getHealth()
    apiState.value = 'ok'
    detail.value = `${health.service} · ${health.environment}`
  } catch {
    apiState.value = 'error'
    databaseState.value = 'error'
    detail.value = '无法连接 FastAPI，请确认后端已在 8000 端口启动。'
    return
  }

  try {
    await getReadiness()
    databaseState.value = 'ok'
  } catch {
    databaseState.value = 'error'
    detail.value = 'API 已启动，但 MySQL 尚未就绪。'
  }
}

function stateLabel(state: CheckState): string {
  return {
    checking: '检查中',
    ok: '正常',
    error: '未就绪',
  }[state]
}

onMounted(checkServices)
</script>

<template>
  <main class="shell">
    <section class="hero">
      <p class="eyebrow">V1 · 阶段一</p>
      <h1>SecondHand Agent</h1>
      <p class="lead">面向个人闲置交易的自主协商卖家助手</p>
      <p class="description">
        当前已建立 FastAPI、LangChain、MySQL 与 Vue 的最小工程骨架。下一阶段将实现独立于大模型的价格规则核心。
      </p>
    </section>

    <section class="status-panel" aria-labelledby="status-title">
      <div class="panel-heading">
        <div>
          <p class="eyebrow">LOCAL STATUS</p>
          <h2 id="status-title">开发环境</h2>
        </div>
        <button type="button" @click="checkServices">重新检查</button>
      </div>

      <div class="status-grid">
        <article class="status-card">
          <span class="status-dot" :data-state="apiState" aria-hidden="true"></span>
          <div>
            <h3>FastAPI</h3>
            <p>{{ stateLabel(apiState) }}</p>
          </div>
        </article>

        <article class="status-card">
          <span class="status-dot" :data-state="databaseState" aria-hidden="true"></span>
          <div>
            <h3>MySQL 8.x</h3>
            <p>{{ stateLabel(databaseState) }}</p>
          </div>
        </article>
      </div>

      <p class="detail" role="status">{{ detail }}</p>
    </section>

    <footer>Agent 提出决策，业务服务负责最终执行。</footer>
  </main>
</template>

