export interface HealthResponse {
  status: 'ok'
  service: string
  environment: string
}

export interface ReadinessResponse {
  status: 'ready' | 'degraded'
  database: 'ok'
  model: 'configured' | 'not_configured'
  authentication: 'configured' | 'not_configured'
}

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path, {
    credentials: 'include',
    headers: { Accept: 'application/json' },
  })

  if (!response.ok) {
    throw new Error(`${path} returned ${response.status}`)
  }

  return (await response.json()) as T
}

export function getHealth(): Promise<HealthResponse> {
  return getJson<HealthResponse>('/api/health')
}

export function getReadiness(): Promise<ReadinessResponse> {
  return getJson<ReadinessResponse>('/api/ready')
}
