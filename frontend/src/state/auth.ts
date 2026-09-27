import { computed, ref } from 'vue'

import {
  getCurrentUser,
  loginUser,
  logoutUser,
  registerUser,
  type LoginPayload,
  type RegisterPayload,
  type UserIdentity,
} from '../api/auth'
import { ApiError, setUnauthorizedHandler } from '../api/client'

const currentUser = ref<UserIdentity | null>(null)
const initialized = ref(false)
let initializationPromise: Promise<void> | null = null
let reconciliationPromise: Promise<void> | null = null

const AUTH_SYNC_CHANNEL = 'secondhand-auth-sync'
const AUTH_SYNC_STORAGE_KEY = 'secondhand-auth-sync-event'
const currentTabId = crypto.randomUUID()
let reloadScheduled = false

interface AuthSyncEvent {
  source: string
  version: 1
}

const authSyncChannel =
  typeof BroadcastChannel === 'undefined' ? null : new BroadcastChannel(AUTH_SYNC_CHANNEL)

function isAuthSyncEvent(value: unknown): value is AuthSyncEvent {
  return (
    typeof value === 'object' &&
    value !== null &&
    'source' in value &&
    typeof value.source === 'string' &&
    'version' in value &&
    value.version === 1
  )
}

function reloadForExternalAuthChange(event: AuthSyncEvent): void {
  if (event.source === currentTabId || reloadScheduled) return
  // 同一浏览器配置共享 HttpOnly Cookie。重新加载可同时清空旧账号的页面数据，
  // 避免界面仍显示账号 A，而后续写请求已经按账号 B 授权。
  reloadScheduled = true
  window.location.reload()
}

function publishAuthChange(): void {
  const event: AuthSyncEvent = { source: currentTabId, version: 1 }
  if (authSyncChannel !== null) {
    authSyncChannel.postMessage(event)
    return
  }
  // BroadcastChannel 不可用时使用 storage 事件；该事件不会回传到当前标签页。
  window.localStorage.setItem(
    AUTH_SYNC_STORAGE_KEY,
    JSON.stringify({ ...event, nonce: crypto.randomUUID() }),
  )
}

if (authSyncChannel !== null) {
  authSyncChannel.addEventListener('message', (message: MessageEvent<unknown>) => {
    if (isAuthSyncEvent(message.data)) reloadForExternalAuthChange(message.data)
  })
} else {
  window.addEventListener('storage', (event) => {
    if (event.key !== AUTH_SYNC_STORAGE_KEY || event.newValue === null) return
    try {
      const value: unknown = JSON.parse(event.newValue)
      if (isAuthSyncEvent(value)) reloadForExternalAuthChange(value)
    } catch {
      // 其他脚本写入的无效值不应影响认证状态。
    }
  })
}

setUnauthorizedHandler(() => {
  currentUser.value = null
  initialized.value = true
})

async function initialize(): Promise<void> {
  if (initialized.value) return
  if (initializationPromise !== null) return initializationPromise

  initializationPromise = (async () => {
    try {
      currentUser.value = await getCurrentUser()
      initialized.value = true
    } catch (error) {
      currentUser.value = null
      if (error instanceof ApiError && error.status === 401) {
        initialized.value = true
        return
      }
      throw error
    }
  })()

  try {
    await initializationPromise
  } finally {
    initializationPromise = null
  }
}

async function login(payload: LoginPayload): Promise<UserIdentity> {
  const user = await loginUser(payload)
  currentUser.value = user
  initialized.value = true
  publishAuthChange()
  return user
}

async function register(payload: RegisterPayload): Promise<UserIdentity> {
  const user = await registerUser(payload)
  currentUser.value = user
  initialized.value = true
  publishAuthChange()
  return user
}

async function logout(): Promise<void> {
  await logoutUser()
  currentUser.value = null
  initialized.value = true
  publishAuthChange()
}

async function reconcileCurrentUser(): Promise<void> {
  if (!initialized.value || reconciliationPromise !== null || reloadScheduled) return

  const expectedUserId = currentUser.value?.id ?? null
  reconciliationPromise = (async () => {
    try {
      const user = await getCurrentUser()
      if (user.id !== expectedUserId) {
        reloadForExternalAuthChange({ source: 'external-cookie-change', version: 1 })
        return
      }
      currentUser.value = user
    } catch (error) {
      if (error instanceof ApiError && error.status === 401 && expectedUserId !== null) {
        reloadForExternalAuthChange({ source: 'external-cookie-change', version: 1 })
      }
    }
  })()

  try {
    await reconciliationPromise
  } finally {
    reconciliationPromise = null
  }
}

window.addEventListener('focus', () => {
  void reconcileCurrentUser()
})
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') void reconcileCurrentUser()
})

const auth = {
  currentUser: computed(() => currentUser.value),
  initialized: computed(() => initialized.value),
  initialize,
  login,
  register,
  logout,
}

export function useAuth(): typeof auth {
  return auth
}
