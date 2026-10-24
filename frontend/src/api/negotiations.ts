import { requestJson } from './client'

export { ApiError } from './client'

export type MessageRole = 'BUYER' | 'AGENT' | 'SYSTEM'
export type ShippingPayer = 'buyer' | 'seller'
export type NegotiationStatus = 'ACTIVE' | 'WAITING_APPROVAL' | 'AGREED' | 'CLOSED'

export interface ChatMessage {
  id: number
  role: MessageRole
  content: string
  request_id: string
  created_at: string
  card?: PublicCard | null
}

export interface DraftOption {
  draft_id: string
  draft_version: number
  candidate: { amount: string | null; delivery: string | null; payer: string | null; region: string | null; conditions: { source_quote: string }[]; commitments: string[] }
  missing: string[]
}

export interface PublicCard {
  kind?: 'offer' | 'drafts'
  context_id: string | null
  offer_id?: number
  amount?: string
  shipping_paid_by?: ShippingPayer
  terms?: { delivery_method?: string }
  can_confirm: boolean
  expires_at?: string
  drafts?: DraftOption[]
}

export interface RunStatus {
  run_id: string
  input_seq: number
  task_status: string
  business_status: string
  business_outcome: string | null
  notification_status: string
  blocked_by_input_seq: number | null
  public_error_category: string | null
  recovery_required: boolean
}

export interface AcceptedInput {
  run_id: string
  input_seq: number
  buyer_message_id: number
  status: string
  idempotent_replay: boolean
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
  graph_version?: string
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
  client_view_id: string
  display_receipt_id?: string | null
  draft_id?: string
  draft_version?: number
  reply_to_message_id?: number
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

export interface BuyerNegotiationSummary {
  id: number
  product_id: number
  product_title: string
  product_status: 'DRAFT' | 'AVAILABLE' | 'UNAVAILABLE'
  seller: {
    id: string
    display_name: string
  }
  status: NegotiationStatus
  current_offer_id: number | null
  confirmed_offer_id: number | null
  round_count: number
  created_at: string
  updated_at: string
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

interface BuyerNegotiationListResponse {
  negotiations: BuyerNegotiationSummary[]
}

export function createNegotiation(productId: number): Promise<CreateNegotiationResponse> {
  return requestJson<CreateNegotiationResponse>('/api/negotiations', {
    method: 'POST',
    body: JSON.stringify({ product_id: productId }),
  })
}

export async function listBuyerNegotiations(): Promise<BuyerNegotiationSummary[]> {
  const result = await requestJson<BuyerNegotiationListResponse>('/api/buyer/negotiations')
  return result.negotiations
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
): Promise<AcceptedInput> {
  return requestJson<AcceptedInput>(`/api/negotiations/${sessionId}/messages`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function confirmNegotiation(
  sessionId: number,
  offerId: number,
  requestId: string,
  viewId: string,
  receiptId: string,
): Promise<AcceptedInput> {
  return requestJson<AcceptedInput>(
    `/api/negotiations/${sessionId}/confirm`,
    {
      method: 'POST',
      body: JSON.stringify({ offer_id: offerId, request_id: requestId, client_view_id: viewId, display_receipt_id: receiptId }),
    },
  )
}

export function closeNegotiation(
  sessionId: number,
  requestId: string,
  viewId: string,
): Promise<AcceptedInput> {
  return requestJson<AcceptedInput>(
    `/api/negotiations/${sessionId}/close`,
    {
      method: 'POST',
      body: JSON.stringify({ request_id: requestId, client_view_id: viewId }),
    },
  )
}

export function getRun(sid: number, run: string): Promise<RunStatus> {
  return requestJson(`/api/negotiations/${sid}/runs/${run}`)
}

export function acknowledgeDisplay(sid: number, context: string, view: string, request: string): Promise<{ display_receipt_id: string }> {
  return requestJson(`/api/negotiations/${sid}/display-acks`, {
    method: 'POST', body: JSON.stringify({ context_id: context, client_view_id: view, request_id: request }),
  })
}

export function refreshContext(sid: number, context: string, request: string): Promise<{ context_id: string; expires_at: string }> {
  return requestJson(`/api/negotiations/${sid}/interaction-contexts/refresh`, {
    method: 'POST', body: JSON.stringify({ context_id: context, request_id: request }),
  })
}
