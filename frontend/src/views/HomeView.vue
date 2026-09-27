<script setup lang="ts">
import { useAuth } from '../state/auth'

const auth = useAuth()
</script>

<template>
  <section class="home-page">
    <div class="home-copy">
      <p class="eyebrow">SECONDHAND AGENT</p>
      <h1>一个账号，自由切换买卖模式</h1>
      <p>
        浏览闲置商品、与 Seller Agent 协商，或发布并管理自己的商品。买家和卖家只是使用模式，
        不需要注册两套账号。
      </p>
    </div>

    <div v-if="auth.currentUser.value" class="mode-grid">
      <RouterLink class="mode-card" to="/buyer">
        <span class="mode-icon">买</span>
        <div>
          <p class="eyebrow">BUYER MODE</p>
          <h2>进入买家模式</h2>
          <p>继续浏览商品并使用现有协商功能。</p>
        </div>
        <b aria-hidden="true">→</b>
      </RouterLink>
      <RouterLink class="mode-card" to="/seller">
        <span class="mode-icon">卖</span>
        <div>
          <p class="eyebrow">SELLER MODE</p>
          <h2>进入卖家模式</h2>
          <p>管理商品、报价审批和协商会话。</p>
        </div>
        <b aria-hidden="true">→</b>
      </RouterLink>
    </div>

    <div v-else class="welcome-actions">
      <RouterLink class="primary-link" to="/register">创建统一账号</RouterLink>
      <RouterLink class="secondary-link" to="/login">已有账号，直接登录</RouterLink>
    </div>
  </section>
</template>
