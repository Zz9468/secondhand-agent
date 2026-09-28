import { requestJson } from './client'

export type ModelTaskType = 'CHAT_DECISION' | 'APPROVAL_FOLLOWUP'
export type ModelTaskStatus =
  | 'PENDING'
  | 'RUNNING'
  | 'RETRY_WAIT'
  | 'SUCCEEDED'
  | 'FAILED'
  | 'STALE'
  | 'CANCELLED'
export type ModelTaskErrorCategory =
  | 'MODEL_TIMEOUT'
  | 'RATE_LIMITED'
  | 'NETWORK'
  | 'INVALID_OUTPUT'
  | 'BUSINESS_CONFLICT'
  | 'INTERNAL'
  | 'UNKNOWN'

export interface SellerModelTask {
  id: number
  task_type: ModelTaskType
  status: ModelTaskStatus
  session_id: number
  product_id: number
  product_title: string
  buyer_display_name: string
  offer_id: number | null
  approval_id: number | null
  attempt_count: number
  max_attempts: number
  manual_retry_count: number
  next_retry_at: string | null
  lease_expires_at: string | null
  last_error_category: ModelTaskErrorCategory | null
  last_error_message: string | null
  started_at: string | null
  completed_at: string | null
  last_manual_action: 'RETRY' | 'TERMINATE' | null
  last_manual_actor_id: string | null
  last_manual_reason: string | null
  last_manual_at: string | null
  created_at: string
  updated_at: string
}

interface SellerModelTaskListResponse {
  tasks: SellerModelTask[]
}

export async function listSellerModelTasks(): Promise<SellerModelTask[]> {
  const result = await requestJson<SellerModelTaskListResponse>('/api/seller/model-tasks')
  return result.tasks
}

export function retrySellerModelTask(
  taskId: number,
  reason: string | null,
): Promise<SellerModelTask> {
  return requestJson<SellerModelTask>(`/api/seller/model-tasks/${taskId}/retry`, {
    method: 'POST',
    body: JSON.stringify({ reason }),
  })
}

export function terminateSellerModelTask(
  taskId: number,
  reason: string | null,
): Promise<SellerModelTask> {
  return requestJson<SellerModelTask>(`/api/seller/model-tasks/${taskId}/terminate`, {
    method: 'POST',
    body: JSON.stringify({ reason }),
  })
}
