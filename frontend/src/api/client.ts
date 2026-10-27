export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message)
  }
}

let unauthorizedHandler: (() => void) | undefined
let requestAccount: string | null = null
let identityVersion = 0

export function setRequestAccount(account: string | null): void {
  requestAccount = account
  identityVersion++
}

export function setUnauthorizedHandler(handler: () => void): void {
  unauthorizedHandler = handler
}

export async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const version = identityVersion
  const response = await fetch(path, {
    ...init,
    credentials: 'include',
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
      ...(requestAccount && !path.startsWith('/api/auth/') ? { 'X-Expected-User': requestAccount } : {}),
      ...init?.headers,
    },
  })

  if (!response.ok) {
    if (response.status === 401 && version === identityVersion) unauthorizedHandler?.()
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

  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}
