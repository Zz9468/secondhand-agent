import { requestJson } from './client'

export { ApiError } from './client'

export type MessageRole = 'BUYER' | 'AGENT' | 'SYSTEM'
export type ShippingPayer = 'buyer' | 'seller'

export interface ChatMessage {
  id: number
  role: MessageRole
  content: string
  request_id: string
  created_at: string
}

export interface Offer {
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

export interface NegotiationDetail {
  product: {
    id: number
    title: string
    description: string
    listed_price: string
    status: string
  }
  negotiation: {
    id: number
    product_id: number
    status: string
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
    negotiation_style: string
    max_rounds: number
    recent_offers: Offer[]
  }
}

export interface BuyerOfferPayload {
  price: string
  shipping_paid_by: ShippingPayer
  shipping_cost?: string
  delivery_method?: 'shipping' | 'pickup'
}

export interface SendMessagePayload {
  request_id: string
  content: string
  offer?: BuyerOfferPayload
}

export interface SendMessageResponse {
  buyer_message: ChatMessage
  agent_message: ChatMessage
  outcome: string
  formal_offer_id: number | null
  idempotent_replay: boolean
}

export interface CreateNegotiationResponse {
  session_id: number
  created: boolean
}

export interface ConfirmNegotiationResponse {
  session_id: number
  status: 'AGREED'
  confirmed_offer_id: number
  confirmed_at: string
  confirmation_source:
    | 'AGENT_COUNTER'
    | 'AUTO_ACCEPTED_BUYER_OFFER'
    | 'SELLER_APPROVED_BUYER_OFFER'
  system_message: ChatMessage
  idempotent_replay: boolean
}

export interface CloseNegotiationResponse {
  session_id: number
  status: 'CLOSED'
  cancelled_approval_id: number | null
  system_message: ChatMessage
  idempotent_replay: boolean
}

interface MessageListResponse {
  messages: ChatMessage[]
}

export function createNegotiation(productId: number): Promise<CreateNegotiationResponse> {
  return requestJson<CreateNegotiationResponse>('/api/negotiations', {
    method: 'POST',
    body: JSON.stringify({ product_id: productId }),
  })
}

export function getNegotiation(sessionId: number): Promise<NegotiationDetail> {
  return requestJson<NegotiationDetail>(`/api/negotiations/${sessionId}`)
}

export async function getMessages(
  sessionId: number,
  afterId = 0,
): Promise<ChatMessage[]> {
  const result = await requestJson<MessageListResponse>(
    `/api/negotiations/${sessionId}/messages?after_id=${afterId}`,
  )
  return result.messages
}

export function sendMessage(
  sessionId: number,
  payload: SendMessagePayload,
): Promise<SendMessageResponse> {
  return requestJson<SendMessageResponse>(`/api/negotiations/${sessionId}/messages`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function confirmNegotiation(
  sessionId: number,
  offerId: number,
  requestId: string,
): Promise<ConfirmNegotiationResponse> {
  return requestJson<ConfirmNegotiationResponse>(
    `/api/negotiations/${sessionId}/confirm`,
    {
      method: 'POST',
      body: JSON.stringify({ offer_id: offerId, request_id: requestId }),
    },
  )
}

export function closeNegotiation(
  sessionId: number,
  requestId: string,
): Promise<CloseNegotiationResponse> {
  return requestJson<CloseNegotiationResponse>(
    `/api/negotiations/${sessionId}/close`,
    {
      method: 'POST',
      body: JSON.stringify({ request_id: requestId }),
    },
  )
}
