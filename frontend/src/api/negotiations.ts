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

interface MessageListResponse {
  messages: ChatMessage[]
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message)
  }
}

async function requestJson<T>(path: string, buyerId: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
      'X-Buyer-ID': buyerId,
      ...init?.headers,
    },
  })

  if (!response.ok) {
    let detail = `请求失败（${response.status}）`
    try {
      const body = (await response.json()) as { detail?: string | Array<{ msg?: string }> }
      if (typeof body.detail === 'string') {
        detail = body.detail
      } else if (Array.isArray(body.detail) && body.detail[0]?.msg) {
        detail = body.detail[0].msg
      }
    } catch {
      // 非 JSON 错误沿用状态码提示，避免掩盖原始请求失败。
    }
    throw new ApiError(detail, response.status)
  }

  return (await response.json()) as T
}

export function getNegotiation(sessionId: number, buyerId: string): Promise<NegotiationDetail> {
  return requestJson<NegotiationDetail>(`/api/negotiations/${sessionId}`, buyerId)
}

export async function getMessages(sessionId: number, buyerId: string): Promise<ChatMessage[]> {
  const result = await requestJson<MessageListResponse>(
    `/api/negotiations/${sessionId}/messages`,
    buyerId,
  )
  return result.messages
}

export function sendMessage(
  sessionId: number,
  buyerId: string,
  payload: SendMessagePayload,
): Promise<SendMessageResponse> {
  return requestJson<SendMessageResponse>(`/api/negotiations/${sessionId}/messages`, buyerId, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}
