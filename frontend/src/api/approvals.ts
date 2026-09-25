import { requestJson } from './client'

export type ApprovalStatus = 'PENDING' | 'APPROVED' | 'REJECTED' | 'CANCELLED' | 'EXPIRED'
export type ApprovalFollowupStatus = 'PENDING' | 'SENT' | 'FAILED'

export interface SellerApprovalOffer {
  id: number
  proposer: 'BUYER' | 'AGENT'
  price: string
  shipping_paid_by: 'buyer' | 'seller'
  shipping_cost: string | null
  seller_borne_discount: string
  additional_terms: Record<string, unknown>
  status: 'PROPOSED' | 'ACCEPTED' | 'REJECTED' | 'WITHDRAWN'
  expires_at: string | null
  created_at: string
}

export interface SellerApproval {
  id: number
  session_id: number
  product_id: number
  product_title: string
  offer_id: number
  policy_version: number
  status: ApprovalStatus
  reason: string
  seller_comment: string | null
  expires_at: string
  reviewed_at: string | null
  followup_status: ApprovalFollowupStatus | null
  followup_request_id: string | null
  created_at: string
  updated_at: string
  session_status: 'ACTIVE' | 'WAITING_APPROVAL' | 'AGREED' | 'CLOSED'
  current_offer_id: number | null
  offer: SellerApprovalOffer
}

interface SellerApprovalListResponse {
  approvals: SellerApproval[]
}

export async function listSellerApprovals(
  status?: ApprovalStatus,
): Promise<SellerApproval[]> {
  const query = status ? `?status=${encodeURIComponent(status)}` : ''
  const result = await requestJson<SellerApprovalListResponse>(
    `/api/seller/approvals${query}`,
  )
  return result.approvals
}

export function getSellerApproval(approvalId: number): Promise<SellerApproval> {
  return requestJson<SellerApproval>(`/api/seller/approvals/${approvalId}`)
}

export function reviewSellerApproval(
  approvalId: number,
  action: 'approve' | 'reject',
  requestId: string,
  comment: string | null,
): Promise<SellerApproval> {
  return requestJson<SellerApproval>(
    `/api/seller/approvals/${approvalId}/${action}`,
    {
      method: 'POST',
      body: JSON.stringify({ request_id: requestId, comment }),
    },
  )
}
