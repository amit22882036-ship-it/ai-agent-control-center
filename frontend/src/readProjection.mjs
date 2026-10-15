// The AbortSignal belongs to one mounted projection identity, not an Agent process.
export async function readProjection(path, signal, publish, fetcher = fetch) {
  try {
    const response = await fetcher(`http://127.0.0.1:8000${path}`, { signal })
    if (!response.ok) throw new Error('Projection unavailable')
    const data = await response.json()
    if (!signal.aborted) publish({ path, data, error: false })
  } catch {
    if (!signal.aborted) publish((previous) => ({ path, data: previous.path === path ? previous.data : null, error: true }))
    return false
  }
}
