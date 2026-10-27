import { afterEach, expect, test, vi } from 'vitest'
import { createPolling } from '../../src/state/polling'

afterEach(() => vi.useRealTimers())

test('R13 failed reads back off, cap at 30s and reset after recovery', async () => {
  vi.useFakeTimers()
  let fail = true
  const sync = vi.fn(async () => { if (fail) throw new Error('offline') })
  const error = vi.fn()
  const loop = createPolling({ sync, delay: () => 2000, onError: error })
  loop.start()
  await vi.advanceTimersByTimeAsync(0)
  for (const delay of [4000, 8000, 16000, 30000, 30000]) {
    const before = sync.mock.calls.length
    await vi.advanceTimersByTimeAsync(delay - 1)
    expect(sync).toHaveBeenCalledTimes(before)
    await vi.advanceTimersByTimeAsync(1)
    expect(sync).toHaveBeenCalledTimes(before + 1)
  }
  fail = false
  loop.refresh()
  await vi.advanceTimersByTimeAsync(0)
  const recovered = sync.mock.calls.length
  await vi.advanceTimersByTimeAsync(2000)
  expect(sync).toHaveBeenCalledTimes(recovered + 1)
  expect(error).toHaveBeenCalledTimes(6)
  loop.stop()
})

test('R06 cancelled old cycle cannot block or update the new page', async () => {
  vi.useFakeTimers()
  const cycles: { signal: AbortSignal; alive: () => boolean; finish: () => void }[] = []
  const loop = createPolling({ sync: (signal, alive) => new Promise<void>(finish => cycles.push({ signal, alive, finish })), delay: () => 2000, onError: vi.fn() })
  loop.start()
  loop.start()
  expect(cycles[0]!.signal.aborted).toBe(true)
  expect(cycles[0]!.alive()).toBe(false)
  expect(cycles[1]!.alive()).toBe(true)
  cycles[0]!.finish()
  await vi.advanceTimersByTimeAsync(10_000)
  expect(cycles).toHaveLength(2)
  cycles[1]!.finish()
  await vi.advanceTimersByTimeAsync(2000)
  expect(cycles).toHaveLength(3)
  loop.stop()
  expect(cycles[2]!.alive()).toBe(false)
  cycles[2]!.finish()
  await vi.advanceTimersByTimeAsync(60_000)
  expect(cycles).toHaveLength(3)
})

test('R13 hung GET times out and does not hold the polling lock', async () => {
  vi.useFakeTimers()
  const sync = vi.fn((signal: AbortSignal) => new Promise<void>((_, reject) => signal.addEventListener('abort', () => reject(new Error('aborted')))))
  const error = vi.fn()
  const loop = createPolling({ sync, delay: () => 2000, onError: error })
  loop.start()
  await vi.advanceTimersByTimeAsync(15_000)
  expect(error).toHaveBeenCalledTimes(1)
  await vi.advanceTimersByTimeAsync(4000)
  expect(sync).toHaveBeenCalledTimes(2)
  loop.stop()
  await vi.advanceTimersByTimeAsync(0)
  expect(error).toHaveBeenCalledTimes(1)
})

test('R13 multiple wake requests during a read coalesce into one follow-up', async () => {
  vi.useFakeTimers()
  let finish!: () => void
  const sync = vi.fn(() => new Promise<void>(resolve => { finish = resolve }))
  const loop = createPolling({ sync, delay: () => 2000, onError: vi.fn() })
  loop.start(); loop.refresh(); loop.refresh()
  expect(sync).toHaveBeenCalledTimes(1)
  finish()
  await vi.advanceTimersByTimeAsync(0)
  expect(sync).toHaveBeenCalledTimes(2)
  loop.stop(); finish()
})
