import { ApiError } from '../api/client'

// Existing APIs expose HTTP status and public detail text, not a structured error code.
// Match only known categories; unrelated 409s must retain their original explanation.
export function rejectedOperationMessage(error: ApiError): string {
  let guidance: string
  if (error.status === 429) {
    guidance = '请求过于频繁，请稍后再试。此次提交未获接受，不会自动重试。'
  } else if (error.status === 409 && error.message === '未完成输入已达到 20 条') {
    guidance = '会话队列已满（20 条未完成输入）。请等待前序完成；若需要人工恢复，请联系卖家处理。'
  } else if (error.status === 409 && /展示|草案已失效/.test(error.message)) {
    guidance = '展示依据已失效或不匹配。请刷新相关方案并查看最新报价，再明确发起操作。'
  } else {
    guidance = `${error.message}。请刷新相关状态后明确发起新操作。`
  }
  return `${guidance}不会自动更换请求编号。`
}
