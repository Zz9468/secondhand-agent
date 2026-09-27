import { requestJson } from './client'

export interface UserIdentity {
  id: string
  username: string
  display_name: string
  expires_at: string | null
}

export interface LoginPayload {
  username: string
  password: string
}

export interface RegisterPayload extends LoginPayload {
  display_name: string
}

export function registerUser(payload: RegisterPayload): Promise<UserIdentity> {
  return requestJson<UserIdentity>('/api/auth/register', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function loginUser(payload: LoginPayload): Promise<UserIdentity> {
  return requestJson<UserIdentity>('/api/auth/login', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function getCurrentUser(): Promise<UserIdentity> {
  return requestJson<UserIdentity>('/api/auth/me')
}

export function logoutUser(): Promise<{ logged_out: boolean }> {
  return requestJson<{ logged_out: boolean }>('/api/auth/logout', {
    method: 'POST',
  })
}
