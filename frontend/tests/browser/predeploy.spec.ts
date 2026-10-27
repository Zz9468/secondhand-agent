import { expect, test, type APIRequestContext, type Page, type TestInfo } from '@playwright/test'

interface Seed { sid: number; buyer: string; seller: string; run: string | null }
async function seed(request: APIRequestContext, mode = 'empty'): Promise<Seed> {
  const response = await request.post(`/api/__predeploy/seed/${mode}`)
  expect(response.status()).toBe(200)
  return response.json()
}
async function open(page: Page, data: Seed, sellerPath?: string) {
  await page.setExtraHTTPHeaders({ 'x-predeploy-actor': sellerPath ? data.seller : data.buyer })
  await page.goto(sellerPath ?? `/negotiations/${data.sid}`)
  if (!sellerPath) await expect(page.getByRole('textbox', { name: '聊天消息' })).toBeVisible()
}
async function inspect(request: APIRequestContext, data: Seed) {
  const response = await request.get(`/api/__predeploy/inspect/${data.sid}`)
  expect(response.status()).toBe(200)
  return response.json()
}
async function evidence(info: TestInfo, value: unknown) {
  await info.attach('sanitized-observation', { body: JSON.stringify(value, null, 2), contentType: 'application/json' })
}
async function send(page: Page, content = 'D0 request persistence') {
  await page.getByRole('textbox', { name: '聊天消息' }).fill(content)
  await page.getByRole('button', { name: '发送', exact: true }).click()
}
// Crucially route.fetch waits for the real backend to COMMIT before aborting delivery.
async function loseAcceptedResponse(page: Page, path: string, expectedStatus = 202) {
  await page.route(path, async route => {
    if (route.request().method() !== 'POST') return route.continue()
    const accepted = await route.fetch()
    expect(accepted.status()).toBe(expectedStatus)
    await route.abort('failed')
  }, { times: 1 })
}

test.afterEach(async ({ page }, info) => {
  if (page.url() !== 'about:blank') {
    const screenshot = info.outputPath('state.png')
    await page.screenshot({ path: screenshot, fullPage: true })
    await info.attach('state', { path: screenshot, contentType: 'image/png' })
  }
})

test('R01 offline before submission can retry the same in-memory request', async ({ page, request }, info) => {
  const data = await seed(request)
  await open(page, data)
  let original = ''
  await page.route(`**/api/negotiations/${data.sid}/messages`, async route => {
    if (route.request().method() !== 'POST') return route.continue()
    original = route.request().postDataJSON().request_id
    await route.abort('internetdisconnected')
  }, { times: 1 })
  await send(page)
  await expect(page.getByRole('alert')).toBeVisible()
  expect((await inspect(request, data)).inputs).toHaveLength(0)
  await page.getByRole('button', { name: '发送', exact: true }).click()
  await expect(page.locator('.outcome-banner')).toHaveCount(1)
  const saved = await inspect(request, data)
  expect(saved.inputs).toHaveLength(1)
  expect(saved.inputs[0].request_id).toBe(original)
  await evidence(info, saved)
})

test('R02 accepted response lost then refresh must preserve the original operation', async ({ page, request }, info) => {
  const data = await seed(request)
  await open(page, data)
  await loseAcceptedResponse(page, `**/api/negotiations/${data.sid}/messages`)
  await send(page)
  await expect(page.getByRole('alert')).toBeVisible()
  const before = await inspect(request, data)
  expect(before.inputs).toHaveLength(1)
  await page.reload()
  await expect(page.getByRole('textbox', { name: '聊天消息' })).toBeVisible()
  await send(page)
  await expect(page.locator('.outcome-banner')).toHaveCount(1)
  const after = await inspect(request, data)
  await evidence(info, { before, after, browserVersion: page.context().browser()?.version() })
  test.fail(true, 'D0 DEV-01: refresh loses request identity; the same attempted retry becomes input #2 (D1)')
  expect(after.inputs).toHaveLength(1)
})

test('R05 confirmation response lost then refresh must not enqueue another confirmation', async ({ page, request }, info) => {
  const data = await seed(request, 'offer')
  await open(page, data)
  const confirm = page.getByRole('button', { name: '确认交易意向', exact: true })
  await expect(confirm).toBeEnabled()
  await loseAcceptedResponse(page, `**/api/negotiations/${data.sid}/confirm`)
  await confirm.click()
  await expect(page.getByRole('alert')).toBeVisible()
  const before = await inspect(request, data)
  expect(before.inputs).toHaveLength(2)
  await page.reload()
  await expect(confirm).toBeEnabled()
  await confirm.click()
  await expect(page.locator('.outcome-banner')).toHaveCount(1)
  const after = await inspect(request, data)
  await evidence(info, { before, after })
  test.fail(true, 'D0 DEV-01: confirmation identity lost on reload; D1 must retain original view and receipt')
  expect(after.inputs).toHaveLength(2)
})

test('R03 R04 API replay preserves identity and payload conflict is rejected', async ({ request }, info) => {
  const data = await seed(request)
  const path = `/api/negotiations/${data.sid}/messages`
  const payload = { request_id: 'd0-idempotency-request', content: '900自提', client_view_id: 'd0-view' }
  const headers = { 'x-predeploy-actor': data.buyer }
  const first = await request.post(path, { headers, data: payload })
  expect(first.status()).toBe(202)
  const replay = await request.post(path, { headers, data: payload })
  expect(replay.status()).toBe(202)
  expect((await replay.json()).run_id).toBe((await first.json()).run_id)
  expect((await replay.json()).idempotent_replay).toBe(true)
  const conflict = await request.post(path, { headers, data: { ...payload, content: '950自提' } })
  expect(conflict.status()).toBe(409)
  const after = await inspect(request, data)
  expect(after.inputs).toHaveLength(1)
  expect(after.business_receipts).toBe(0)
  await evidence(info, after)
})

test('R12 API caps pending inputs at twenty while accepting idempotent replay', async ({ request }, info) => {
  const data = await seed(request)
  const path = `/api/negotiations/${data.sid}/messages`
  const payload = { request_id: 'd0-queue-request-0', content: 'D0 queue', client_view_id: 'd0-view' }
  const headers = { 'x-predeploy-actor': data.buyer }
  for (let index = 0; index < 20; index++) {
    const response = await request.post(path, { headers, data: { ...payload, request_id: `d0-queue-request-${index}` } })
    expect(response.status()).toBe(202)
  }
  expect((await request.post(path, { headers, data: { ...payload, request_id: 'd0-overflow-request' } })).status()).toBe(409)
  const replay = await request.post(path, { headers, data: payload })
  expect(replay.status()).toBe(202)
  expect((await replay.json()).idempotent_replay).toBe(true)
  const after = await inspect(request, data)
  expect(after.inputs).toHaveLength(20)
  await evidence(info, after)
})

test('R07 storage unavailable must prevent submission before the write', async ({ page, request }, info) => {
  const data = await seed(request)
  await open(page, data)
  await page.evaluate(() => { Storage.prototype.setItem = () => { throw new DOMException('D0 quota', 'QuotaExceededError') } })
  await send(page)
  await expect(page.getByRole('alert')).toBeVisible()
  const after = await inspect(request, data)
  await evidence(info, after)
  test.fail(true, 'D0 DEV-01: writes backend first; cache error misreports accepted operation (D1)')
  expect(after.inputs).toHaveLength(0)
})

test('R08 repeated send click during HTTP submission creates one input', async ({ page, request }, info) => {
  const data = await seed(request)
  await open(page, data)
  let release!: () => void
  const gate = new Promise<void>(resolve => { release = resolve })
  let posts = 0
  await page.route(`**/api/negotiations/${data.sid}/messages`, async route => {
    if (route.request().method() !== 'POST') return route.continue()
    posts += 1
    await gate
    await route.continue()
  })
  await send(page)
  const button = page.locator('.composer button')
  await expect(button).toBeDisabled()
  await button.dispatchEvent('click')
  release()
  await expect(page.locator('.outcome-banner')).toHaveCount(1)
  const after = await inspect(request, data)
  expect(posts).toBe(1)
  expect(after.inputs).toHaveLength(1)
  await evidence(info, after)
})

test('R08 later unsent text survives the earlier submission response', async ({ page, request }, info) => {
  const data = await seed(request)
  await open(page, data)
  let release!: () => void
  const gate = new Promise<void>(resolve => { release = resolve })
  await page.route(`**/api/negotiations/${data.sid}/messages`, async route => {
    if (route.request().method() !== 'POST') return route.continue()
    const response = await route.fetch()
    expect(response.status()).toBe(202)
    await gate
    await route.fulfill({ response })
  })
  await send(page)
  const input = page.getByRole('textbox', { name: '聊天消息' })
  await input.fill('这条还未发送，请保留')
  release()
  await expect(page.locator('.outcome-banner')).toHaveCount(1)
  const after = await inspect(request, data)
  expect(after.inputs).toHaveLength(1)
  await evidence(info, after)
  test.fail(true, 'D0 DEV-04: successful earlier response clears the newer draft (D3)')
  await expect(input).toHaveValue('这条还未发送，请保留')
})

test('R10 synthetic successful task must follow pending notification to published', async ({ page, request }, info) => {
  const data = await seed(request)
  await open(page, data)
  let published = false
  let polls = 0
  await page.route(`**/api/negotiations/${data.sid}/runs/*`, async route => {
    polls += 1
    await route.fulfill({ json: { run_id: route.request().url().split('/').pop(), input_seq: 1,
      task_status: 'SUCCEEDED', business_status: 'COMMITTED', business_outcome: 'OFFER_ACCEPTED',
      notification_status: published ? 'PUBLISHED' : 'PENDING', blocked_by_input_seq: null,
      public_error_category: published ? null : 'RESULT_DELIVERY_DELAYED', recovery_required: false } })
  })
  await send(page)
  await expect(page.locator('.outcome-banner')).toContainText('结果通知恢复中')
  published = true
  // Observe two real periodic detail refreshes instead of relying on an arbitrary sleep.
  for (let i = 0; i < 2; i++) await page.waitForResponse(r => r.url().endsWith(`/api/negotiations/${data.sid}`))
  await evidence(info, { source: 'synthetic public run statuses, real Vue polling', polls, published,
    database: await inspect(request, data) })
  test.fail(true, 'D0 DEV-03: SUCCEEDED stops querying notification updates (D2)')
  await expect(page.locator('.outcome-banner')).not.toContainText('结果通知恢复中')
})

test('R11 approval response lost can be recovered from existing seller detail', async ({ page, request }, info) => {
  const data = await seed(request, 'approval')
  await open(page, data, '/seller/approvals')
  const approve = page.getByRole('button', { name: '同意报价', exact: true })
  await expect(approve).toBeEnabled()
  await loseAcceptedResponse(page, '**/api/seller/approvals/*/approve', 200)
  await approve.click()
  await expect(page.getByRole('alert')).toBeVisible()
  const before = await inspect(request, data)
  await page.reload()
  await expect(page.locator('.approval-editor .approval-status')).toHaveText('已同意')
  await expect(approve).toHaveCount(0)
  const after = await inspect(request, data)
  expect(after.approval_decisions).toBe(1)
  expect(after.approvals).toEqual(before.approvals)
  await evidence(info, { before, after })
})

test('R11 recovery response lost must remain attributable after refresh', async ({ page, request }, info) => {
  const data = await seed(request, 'failed')
  await open(page, data, '/seller/model-tasks')
  await expect(page.getByRole('button', { name: '重试一次', exact: true })).toBeVisible()
  await page.getByPlaceholder('填写本次恢复原因').fill('D0 local fixture retry')
  await loseAcceptedResponse(page, '**/api/seller/graph-recovery/*', 200)
  await page.getByRole('button', { name: '重试一次', exact: true }).click()
  await expect(page.locator('.graph-recovery [role=status]')).toBeVisible()
  const before = await inspect(request, data)
  expect(before.recovery_actions).toBe(1)
  expect(before.inputs[0].manual_retry_count).toBe(1)
  await page.reload()
  await expect(page.getByText('当前没有需要人工处置的协商任务。')).toBeVisible()
  const after = await inspect(request, data)
  expect(after.recovery_actions).toBe(1)
  expect(after.inputs).toEqual(before.inputs)
  await evidence(info, { before, after })
  test.fail(true, 'D0 DEV-05: item disappears with no saved operation or result to verify (D1); no duplicate recovery proven')
  await expect(page.locator('.graph-recovery')).toContainText('已登记恢复操作')
})
