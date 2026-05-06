// Thin fetch wrapper. Always sends cookies; surfaces 401 to the auth store
// so any view can simply `await api.get(...)` and the redirect handles itself.

export class ApiError extends Error {
  status: number
  detail: unknown
  constructor(status: number, detail: unknown, message: string) {
    super(message)
    this.status = status
    this.detail = detail
  }
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown
): Promise<T> {
  const init: RequestInit = {
    method,
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' }
  }
  if (body !== undefined) {
    init.body = JSON.stringify(body)
  }

  const res = await fetch(path, init)
  if (res.status === 204) {
    return undefined as T
  }

  const text = await res.text()
  let data: unknown = null
  if (text) {
    try {
      data = JSON.parse(text)
    } catch {
      data = text
    }
  }

  if (!res.ok) {
    const detail =
      (data && typeof data === 'object' && 'detail' in data
        ? (data as { detail: unknown }).detail
        : data) ?? res.statusText
    throw new ApiError(res.status, data, String(detail))
  }
  return data as T
}

export const api = {
  get: <T>(path: string) => request<T>('GET', path),
  post: <T>(path: string, body?: unknown) => request<T>('POST', path, body),
  put: <T>(path: string, body?: unknown) => request<T>('PUT', path, body),
  del: <T>(path: string) => request<T>('DELETE', path)
}
