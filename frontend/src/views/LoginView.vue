<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useAuth } from '../state/auth'

const auth = useAuth()
const route = useRoute()
const router = useRouter()
const username = ref('')
const password = ref('')
const submitting = ref(false)
const errorMessage = ref('')

const unavailableMessage = computed(() => (
  route.query.reason === 'unavailable'
    ? '暂时无法确认登录状态，请检查后端认证服务。'
    : ''
))

function redirectTarget(): string {
  const target = route.query.redirect
  if (typeof target !== 'string' || !target.startsWith('/') || target.startsWith('//')) {
    return '/'
  }
  return target
}

async function submit(): Promise<void> {
  submitting.value = true
  errorMessage.value = ''
  try {
    await auth.login({ username: username.value, password: password.value })
    password.value = ''
    await router.replace(redirectTarget())
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登录失败，请稍后重试。'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <section class="auth-page">
    <form class="auth-card" @submit.prevent="submit">
      <p class="eyebrow">WELCOME BACK</p>
      <h1>登录统一账号</h1>
      <p>登录后可以在买家模式和卖家模式之间随时切换。</p>
      <p v-if="unavailableMessage" class="error-banner" role="alert">
        {{ unavailableMessage }}
      </p>
      <label>
        <span>用户名</span>
        <input
          v-model="username"
          autocomplete="username"
          maxlength="64"
          minlength="3"
          required
        />
      </label>
      <label>
        <span>密码</span>
        <input
          v-model="password"
          autocomplete="current-password"
          maxlength="128"
          minlength="12"
          type="password"
          required
        />
      </label>
      <p v-if="errorMessage" class="error-banner" role="alert">{{ errorMessage }}</p>
      <button type="submit" :disabled="submitting">
        {{ submitting ? '登录中…' : '登录' }}
      </button>
      <p class="auth-alternative">
        还没有账号？<RouterLink to="/register">立即注册</RouterLink>
      </p>
    </form>
  </section>
</template>
