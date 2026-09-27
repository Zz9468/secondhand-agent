<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useAuth } from './state/auth'

const auth = useAuth()
const route = useRoute()
const router = useRouter()
const signingOut = ref(false)
const navigationError = ref('')

const activeMode = computed<'buyer' | 'seller' | null>(() => {
  if (/^\/(buyer|sellers|products|negotiations)(\/|$)/.test(route.path)) {
    return 'buyer'
  }
  if (/^\/seller(\/|$)/.test(route.path)) return 'seller'
  return null
})

async function signOut(): Promise<void> {
  signingOut.value = true
  navigationError.value = ''
  try {
    await auth.logout()
    await router.replace('/login')
  } catch (error) {
    navigationError.value = error instanceof Error ? error.message : '退出失败，请稍后重试。'
  } finally {
    signingOut.value = false
  }
}

watch(auth.currentUser, (user) => {
  if (user === null && route.meta.requiresAuth) {
    void router.replace({ name: 'login', query: { redirect: route.fullPath } })
  }
})
</script>

<template>
  <main class="app-shell">
    <header class="topbar">
      <RouterLink class="brand" to="/" aria-label="SecondHand Agent 首页">
        <span class="brand-mark">S</span>
        <span>
          <strong>SecondHand</strong>
          <small>自主协商卖家助手</small>
        </span>
      </RouterLink>

      <div class="topbar-actions">
        <template v-if="auth.currentUser.value">
          <nav class="view-switcher" aria-label="买卖模式切换">
            <RouterLink to="/buyer" :data-active="activeMode === 'buyer'">
              买家模式
            </RouterLink>
            <RouterLink to="/seller" :data-active="activeMode === 'seller'">
              卖家模式
            </RouterLink>
          </nav>
          <div class="account-chip" :title="`登录账号：${auth.currentUser.value.username}`">
            <span>{{ auth.currentUser.value.display_name.slice(0, 1) }}</span>
            <div>
              <strong>{{ auth.currentUser.value.display_name }}</strong>
              <small>@{{ auth.currentUser.value.username }}</small>
            </div>
          </div>
          <button
            class="ghost-button"
            type="button"
            :disabled="signingOut"
            @click="signOut"
          >
            {{ signingOut ? '退出中…' : '退出' }}
          </button>
        </template>
        <nav v-else class="guest-navigation" aria-label="账号入口">
          <RouterLink to="/login">登录</RouterLink>
          <RouterLink class="primary-link compact" to="/register">注册</RouterLink>
        </nav>
      </div>
    </header>

    <p v-if="navigationError" class="error-banner" role="alert">
      {{ navigationError }}
    </p>
    <RouterView />
  </main>
</template>
