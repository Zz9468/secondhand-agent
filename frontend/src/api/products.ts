import { requestJson } from './client'

export type ProductStatus = 'DRAFT' | 'AVAILABLE' | 'UNAVAILABLE'
export type NegotiationStyle = 'FIRM' | 'BALANCED' | 'FLEXIBLE'

interface ProductFacts {
  id: number
  title: string
  description: string
  listed_price: string
  status: ProductStatus
}

export interface PublicProduct extends ProductFacts {
  seller: {
    id: string
    display_name: string
  }
}

export interface SellerPolicy {
  minimum_net_price: string
  auto_accept_threshold: string
  negotiation_style: NegotiationStyle
  max_rounds: number
  version: number
}

export interface SellerProduct extends ProductFacts {
  policy: SellerPolicy
}

export interface ProductWritePayload {
  title: string
  description: string
  listed_price: string
  status: ProductStatus
}

export interface PolicyWritePayload {
  minimum_net_price: string
  auto_accept_threshold: string
  negotiation_style: NegotiationStyle
  max_rounds: number
}

interface PublicProductListResponse {
  products: PublicProduct[]
}

interface SellerProductListResponse {
  products: SellerProduct[]
}

export async function listPublicProducts(sellerId?: string): Promise<PublicProduct[]> {
  const query = sellerId ? `?seller_id=${encodeURIComponent(sellerId)}` : ''
  const result = await requestJson<PublicProductListResponse>(`/api/products${query}`)
  return result.products
}

export function getPublicProduct(productId: number): Promise<PublicProduct> {
  return requestJson<PublicProduct>(`/api/products/${productId}`)
}

export function createProduct(
  product: ProductWritePayload,
  policy: PolicyWritePayload,
): Promise<SellerProduct> {
  return requestJson<SellerProduct>('/api/products', {
    method: 'POST',
    body: JSON.stringify({ ...product, policy }),
  })
}

export async function listSellerProducts(): Promise<SellerProduct[]> {
  const result = await requestJson<SellerProductListResponse>('/api/seller/products')
  return result.products
}

export function updateProduct(
  productId: number,
  payload: ProductWritePayload,
): Promise<SellerProduct> {
  return requestJson<SellerProduct>(`/api/seller/products/${productId}`, {
    method: 'PUT',
    body: JSON.stringify(payload),
  })
}

export function deleteProduct(productId: number): Promise<void> {
  return requestJson<void>(`/api/seller/products/${productId}`, {
    method: 'DELETE',
  })
}

export function updatePolicy(
  productId: number,
  payload: PolicyWritePayload & { expected_version: number },
): Promise<SellerProduct> {
  return requestJson<SellerProduct>(`/api/seller/products/${productId}/policy`, {
    method: 'PUT',
    body: JSON.stringify(payload),
  })
}
