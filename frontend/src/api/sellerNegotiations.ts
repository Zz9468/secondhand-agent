import { requestJson } from './client'
import type { ApprovalFollowupStatus, ApprovalStatus } from './approvals'
import type { ShippingPayer } from './negotiations'
import type { ProductStatus } from './products'

export type NegotiationStatus = 'ACTIVE' | 'WAITING_APPROVAL' | 'AGREED' | 'CLOSED'

export interface SellerNegotiationOffer {
  id: number
  proposer: 'BUYER' | 'AGENT'
  price: string
  shipping_paid_by: ShippingPayer
  shipping_cost: string | null
  seller_borne_discount: string
  additional_terms: Record<string, unknown>
  status: 'PROPOSED' | 'ACCEPTED' | 'REJECTED' | 'WITHDRAWN'
  expires_at: string | null
  created_at: string
}

export interface SellerNegotiationApproval {
  id: number
  offer_id: number
  policy_version: number
  status: ApprovalStatus
  reason: string
  seller_comment: string | null
  expires_at: string
  reviewed_at: string | null
  followup_status: ApprovalFollowupStatus | null
  created_at: string
  updated_at: string
}

export interface SellerNegotiationSummary {
  id: number
  product_id: number
  product_title: string
  product_status: ProductStatus
  buyer_display_name: string
  status: NegotiationStatus
  current_offer_id: number | null
  confirmed_offer_id: number | null
  confirmed_at: string | null
  confirmation_source:
    | 'AGENT_COUNTER'
    | 'AUTO_ACCEPTED_BUYER_OFFER'
    | 'SELLER_APPROVED_BUYER_OFFER'
    | null
  round_count: number
  version: number
  current_offer: SellerNegotiationOffer | null
  confirmed_offer: SellerNegotiationOffer | null
  latest_approval: SellerNegotiationApproval | null
  created_at: string
  updated_at: string
}

export interface SellerNegotiationMessage {
  id: number
  role: 'BUYER' | 'AGENT' | 'SYSTEM'
  content: string
  formal_offer_id: number | null
  agent_outcome: string | null
  created_at: string
}

export interface SellerNegotiationDetail {
  negotiation: SellerNegotiationSummary
  messages: SellerNegotiationMessage[]
  offers: SellerNegotiationOffer[]
  approvals: SellerNegotiationApproval[]
}

interface SellerNegotiationListResponse {
  negotiations: SellerNegotiationSummary[]
}

export async function listSellerNegotiations(
  status?: NegotiationStatus,
): Promise<SellerNegotiationSummary[]> {
  const query = status ? `?status=${encodeURIComponent(status)}` : ''
  const result = await requestJson<SellerNegotiationListResponse>(
    `/api/seller/negotiations${query}`,
  )
  return result.negotiations
}

export function getSellerNegotiation(
  sessionId: number,
): Promise<SellerNegotiationDetail> {
  return requestJson<SellerNegotiationDetail>(
    `/api/seller/negotiations/${sessionId}`,
  )
}
