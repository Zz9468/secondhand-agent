import { requestJson } from './client'
import type { PublicProduct } from './products'

export interface PublicSeller {
  id: string
  display_name: string
  available_product_count: number
}

interface PublicSellerListResponse {
  sellers: PublicSeller[]
}

interface PublicProductListResponse {
  products: PublicProduct[]
}

export async function listPublicSellers(): Promise<PublicSeller[]> {
  const result = await requestJson<PublicSellerListResponse>('/api/sellers')
  return result.sellers
}

export function getPublicSeller(sellerId: string): Promise<PublicSeller> {
  return requestJson<PublicSeller>(`/api/sellers/${encodeURIComponent(sellerId)}`)
}

export async function listPublicSellerProducts(sellerId: string): Promise<PublicProduct[]> {
  const result = await requestJson<PublicProductListResponse>(
    `/api/sellers/${encodeURIComponent(sellerId)}/products`,
  )
  return result.products
}
