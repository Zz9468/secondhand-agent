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

export async function getMessages(sessionId: number): Promise<ChatMessage[]> {
  const result = await requestJson<MessageListResponse>(
    `/api/negotiations/${sessionId}/messages`,
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
