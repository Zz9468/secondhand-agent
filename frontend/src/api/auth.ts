import { requestJson } from './client'

export interface BuyerIdentity {
  buyer_id: string
  expires_at: string
}

export async function ensureVisitorIdentity(): Promise<BuyerIdentity> {
  return requestJson<BuyerIdentity>('/api/auth/visitor', {
    method: 'POST',
  })
}

export interface SellerIdentity {
  id: string
  username: string
  expires_at: string | null
}

export function loginSeller(
  username: string,
  password: string,
): Promise<SellerIdentity> {
  return requestJson<SellerIdentity>('/api/auth/seller/login', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  })
}

export function getSellerIdentity(): Promise<SellerIdentity> {
  return requestJson<SellerIdentity>('/api/auth/seller/me')
}

export function logoutSeller(): Promise<{ logged_out: boolean }> {
  return requestJson<{ logged_out: boolean }>('/api/auth/seller/logout', {
    method: 'POST',
  })
}
