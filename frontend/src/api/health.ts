export interface HealthResponse {
  status: 'ok'
  service: string
  environment: string
}

export interface ReadinessResponse {
  status: 'ready'
  database: 'ok'
}

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path, {
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

