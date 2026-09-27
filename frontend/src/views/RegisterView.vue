<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'

import { useAuth } from '../state/auth'

const auth = useAuth()
const router = useRouter()
const username = ref('')
const displayName = ref('')
const password = ref('')
const submitting = ref(false)
const errorMessage = ref('')

async function submit(): Promise<void> {
  submitting.value = true
  errorMessage.value = ''
  try {
    await auth.register({
      username: username.value,
      display_name: displayName.value,
      password: password.value,
    })
    password.value = ''
    await router.replace('/')
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '注册失败，请稍后重试。'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <section class="auth-page">
    <form class="auth-card" @submit.prevent="submit">
      <p class="eyebrow">CREATE ACCOUNT</p>
      <h1>注册统一账号</h1>
      <p>无需选择买家或卖家身份，注册后两种模式都可以使用。</p>
      <label>
        <span>用户名</span>
        <input
          v-model="username"
          autocomplete="username"
          maxlength="64"
          minlength="3"
          pattern="[A-Za-z0-9_-]+"
          required
        />
        <small>3～64 位，仅限字母、数字、下划线和连字符。</small>
      </label>
      <label>
        <span>公开显示名称</span>
        <input
          v-model="displayName"
          autocomplete="nickname"
          maxlength="100"
          required
        />
        <small>会在后续公开卖家页面中展示，登录用户名不会公开。</small>
      </label>
      <label>
        <span>密码</span>
        <input
          v-model="password"
          autocomplete="new-password"
          maxlength="128"
          minlength="12"
          type="password"
          required
        />
        <small>至少 12 位，并包含字母、数字、符号中的至少两类。</small>
      </label>
      <p v-if="errorMessage" class="error-banner" role="alert">{{ errorMessage }}</p>
      <button type="submit" :disabled="submitting">
        {{ submitting ? '注册中…' : '注册并登录' }}
      </button>
      <p class="auth-alternative">
        已有账号？<RouterLink to="/login">返回登录</RouterLink>
      </p>
    </form>
  </section>
</template>
