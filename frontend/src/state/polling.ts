// One cancellable read cycle per page generation. Writes never use this loop.
export function createPolling(options: {
  sync: (signal: AbortSignal, alive: () => boolean) => Promise<void>
  delay: () => number
  onError: () => void
}) {
  let enabled = false
  let generation = 0
  let failures = 0
  let requested = false
  let timer: ReturnType<typeof setTimeout> | undefined
  let active: AbortController | undefined

  async function tick() {
    if (!enabled || active) return
    const version = generation
    const controller = new AbortController()
    active = controller
    const alive = () => enabled && version === generation && active === controller
    // An unresponsive GET must not hold this page's polling lock indefinitely.
    const timeout = setTimeout(() => controller.abort(), 15_000)
    try {
      await options.sync(controller.signal, alive)
      if (alive()) failures = 0
    } catch {
      if (alive()) { failures++; options.onError() }
    } finally {
      clearTimeout(timeout)
      if (alive()) {
        active = undefined
        const delay = requested ? 0 : failures ? Math.min(30_000, 2_000 * 2 ** Math.min(failures, 4)) : options.delay()
        requested = false
        timer = setTimeout(() => void tick(), delay)
      }
    }
  }
  function stop() {
    enabled = false; generation++; requested = false; failures = 0
    clearTimeout(timer); active?.abort(); active = undefined
  }
  function start() {
    stop(); enabled = true
    void tick()
  }
  function refresh() {
    if (!enabled) return
    if (active) { requested = true; return }
    clearTimeout(timer)
    void tick()
  }
  return { start, stop, refresh }
}
