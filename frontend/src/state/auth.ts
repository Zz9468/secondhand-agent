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
  return user
}

async function register(payload: RegisterPayload): Promise<UserIdentity> {
  const user = await registerUser(payload)
  currentUser.value = user
  initialized.value = true
  return user
}

async function logout(): Promise<void> {
  await logoutUser()
  currentUser.value = null
  initialized.value = true
}

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
