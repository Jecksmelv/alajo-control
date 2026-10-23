// Thin API client. All figures shown in the UI come from these endpoints (SQLite / measured logs).

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type Any = any

async function handle(r: Response): Promise<Any> {
  const text = await r.text()
  let body: Any = null
  try { body = text ? JSON.parse(text) : null } catch { body = { message: text } }
  if (!r.ok) {
    const msg = body?.message || body?.detail || `HTTP ${r.status}`
    const err = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg)) as Error & { body?: Any; status?: number }
    err.body = body
    err.status = r.status
    throw err
  }
  return body
}

export const api = {
  get: (path: string) => fetch(`/api${path}`).then(handle),
  post: (path: string, body?: unknown) =>
    fetch(`/api${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    }).then(handle),
}
