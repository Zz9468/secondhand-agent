export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message)
  }
}

export async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: 'include',
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
      ...init?.headers,
    },
  })

  if (!response.ok) {
    let detail = `请求失败（${response.status}）`
    try {
      const body = (await response.json()) as {
        detail?: string | Array<{ msg?: string }>
      }
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
