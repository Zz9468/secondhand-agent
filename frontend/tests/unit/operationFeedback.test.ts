import { describe, expect, it } from 'vitest'
import { ApiError } from '../../src/api/client'
import { rejectedOperationMessage } from '../../src/state/operationFeedback'

describe('public operation rejection guidance', () => {
  it('distinguishes capacity from rate limiting', () => {
    expect(rejectedOperationMessage(new ApiError('未完成输入已达到 20 条', 409))).toContain('会话队列已满')
    expect(rejectedOperationMessage(new ApiError('rate limit exceeded', 429))).toContain('请求过于频繁')
  })
  it.each(['展示凭据归属、视图或时间无效', '确认需要该视图已展示报价的回执', '草案已失效或无权访问'])(
    'requires renewed display for %s', detail => {
      const result = rejectedOperationMessage(new ApiError(detail, 409))
      expect(result).toContain('查看最新报价')
      expect(result).toContain('不会自动更换请求编号')
    },
  )
  it('does not label arbitrary conflicts as expired display', () => {
    const result = rejectedOperationMessage(new ApiError('同一请求标识的载荷不一致', 409))
    expect(result).toContain('载荷不一致')
    expect(result).not.toContain('展示依据已失效')
  })
})
