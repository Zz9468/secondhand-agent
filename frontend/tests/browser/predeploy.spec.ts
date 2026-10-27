import { expect, test, type APIRequestContext, type Page, type TestInfo } from '@playwright/test'

interface Seed { sid: number; buyer: string; seller: string; run: string | null }
async function seed(request: APIRequestContext, mode = 'empty'): Promise<Seed> {
  const response = await request.post(`/api/__predeploy/seed/${mode}`)
  expect(response.status()).toBe(200)
  return response.json()
}
async function login(request: APIRequestContext, username: string) {
  const result = await request.post('/api/auth/login', { data: { username, password: 'integration-test-password' } })
  expect(result.status()).toBe(200)
}
async function open(page: Page, data: Seed, sellerPath?: string) {
  await login(page.request, sellerPath ? data.seller : data.buyer)
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

test('R01 offline before submission and refresh preserve the same request', async ({ page, request }, info) => {
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
  await page.reload()
  await page.getByRole('button', { name: '重试原操作', exact: true }).click()
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
  await expect(page.locator('.pending-operations')).toContainText('已核对原操作')
  await expect(page.locator('.outcome-banner')).toHaveCount(1)
  const after = await inspect(request, data)
  await evidence(info, { before, after, browserVersion: page.context().browser()?.version() })
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
  await expect(page.locator('.pending-operations')).toContainText('已核对原操作')
  await expect(page.locator('.outcome-banner')).toHaveCount(1)
  const after = await inspect(request, data)
  await evidence(info, { before, after })
  expect(after.inputs).toHaveLength(2)
})

test('R03 R04 API replay preserves identity and payload conflict is rejected', async ({ request }, info) => {
  const data = await seed(request)
  const path = `/api/negotiations/${data.sid}/messages`
  const payload = { request_id: 'd0-idempotency-request', content: '900自提', client_view_id: 'd0-view' }
  await login(request, data.buyer)
  const headers = { 'X-Expected-User': data.buyer }
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
  await login(request, data.buyer)
  const headers = { 'X-Expected-User': data.buyer }
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
  await expect(page.locator('.graph-recovery [role=alert]')).toBeVisible()
  const before = await inspect(request, data)
  expect(before.recovery_actions).toBe(1)
  expect(before.inputs[0].manual_retry_count).toBe(1)
  await page.reload()
  await expect(page.getByText('当前没有需要人工处置的协商任务。')).toBeVisible()
  const after = await inspect(request, data)
  expect(after.recovery_actions).toBe(1)
  expect(after.inputs).toEqual(before.inputs)
  await evidence(info, { before, after })
  await expect(page.locator('.graph-recovery')).toContainText('已登记恢复操作')
})

test('R03 accepted identical text is a distinct explicit new intent', async ({ page, request }, info) => {
  const data = await seed(request)
  await open(page, data)
  await send(page, 'same independent intent')
  await expect(page.locator('.outcome-banner')).toHaveCount(1)
  await send(page, 'same independent intent')
  await expect(page.locator('.outcome-banner')).toHaveCount(2)
  const result = await inspect(request, data)
  expect(result.inputs).toHaveLength(2)
  expect(result.inputs[0].request_id).not.toBe(result.inputs[1].request_id)
  await evidence(info, result)
})

test('R05 confirmation before acceptance replays the exact old view and receipt after refresh', async ({ page, request }, info) => {
  const data = await seed(request, 'offer')
  await open(page, data)
  let original: unknown
  await page.route(`**/api/negotiations/${data.sid}/confirm`, async route => {
    original = route.request().postDataJSON()
    await route.abort('failed')
  }, { times: 1 })
  await page.getByRole('button', { name: '确认交易意向', exact: true }).click()
  await expect(page.getByRole('alert')).toBeVisible()
  expect((await inspect(request, data)).inputs).toHaveLength(1)
  await page.reload()
  let replay: unknown
  await page.route(`**/api/negotiations/${data.sid}/confirm`, async route => {
    replay = route.request().postDataJSON()
    await route.continue()
  }, { times: 1 })
  await page.getByRole('button', { name: '重试原操作', exact: true }).click()
  await expect(page.locator('.outcome-banner')).toHaveCount(1)
  expect(replay).toEqual(original)
  const after = await inspect(request, data)
  expect(after.inputs).toHaveLength(2)
  const executed = await request.post(`/api/__predeploy/execute/${data.sid}/${after.inputs[1].run_id}`)
  expect(executed.status()).toBe(200)
  await page.reload()
  await expect(page.getByText('已记录交易意向', { exact: true })).toBeVisible()
  const completed = await inspect(request, data)
  expect(completed.business_receipts).toBe(2)
  await evidence(info, { after, completed, replayPreservedAllFields: true })
})

test('R02 close response lost recovers the original operation', async ({ page, request }, info) => {
  const data = await seed(request)
  await open(page, data)
  await loseAcceptedResponse(page, `**/api/negotiations/${data.sid}/close`)
  await page.getByRole('button', { name: '结束协商', exact: true }).click()
  await expect(page.getByRole('alert')).toBeVisible()
  await page.reload()
  await expect(page.locator('.pending-operations')).toContainText('已核对原操作')
  expect((await inspect(request, data)).inputs).toHaveLength(1)
  await evidence(info, await inspect(request, data))
})

test('R04 rejected changed payload never silently receives a replacement id', async ({ page, request }, info) => {
  const data = await seed(request)
  await open(page, data)
  let posts = 0
  await page.route(`**/api/negotiations/${data.sid}/messages`, async route => {
    if (route.request().method() !== 'POST') return route.continue()
    posts++
    await route.fulfill({ status: 409, json: { detail: '展示依据失效，请刷新' } })
  })
  await send(page)
  await expect(page.getByRole('alert')).toContainText('不会自动更换请求编号')
  await expect(page.getByRole('button', { name: '重试原操作', exact: true })).toHaveCount(0)
  await page.reload()
  await expect(page.locator('.pending-operations')).toContainText('本次提交未获接受')
  expect(posts).toBe(1)
  expect((await inspect(request, data)).inputs).toHaveLength(0)
  await evidence(info, { source: 'synthetic 409 UI policy; real payload conflicts covered by R03/R04 API test', posts })
})

test('R07 accepted response followed by cache failure remains accepted', async ({ page, request }, info) => {
  const data = await seed(request)
  await open(page, data)
  await page.route(`**/api/negotiations/${data.sid}/messages`, async route => {
    if (route.request().method() !== 'POST') return route.continue()
    const result = await route.fetch()
    expect(result.status()).toBe(202)
    await page.evaluate(() => { Storage.prototype.setItem = () => { throw new Error('quota after acceptance') } })
    await route.fulfill({ response: result })
  }, { times: 1 })
  await send(page)
  await expect(page.locator('.outcome-banner')).toHaveCount(1)
  await expect(page.locator('.pending-operations')).toContainText('服务端已接受原操作，但本地记录更新失败')
  await expect(page.getByRole('alert')).toHaveCount(0)
  await page.reload()
  await expect(page.locator('.pending-operations')).toContainText('已核对原操作')
  const after = await inspect(request, data)
  expect(after.inputs).toHaveLength(1)
  const cache = await page.evaluate(() => Object.entries(sessionStorage).filter(([key]) => key.startsWith('secondhand-operations:')))
  expect(JSON.stringify(cache)).not.toContain('D0 request persistence')
  await evidence(info, after)
})

for (const damage of ['corrupt', 'expired']) {
  test(`R07 ${damage} cache cannot block server history or prove failure`, async ({ page, request }, info) => {
    const data = await seed(request, 'offer')
    await open(page, data)
    await page.evaluate(({ account, damage }) => {
      sessionStorage.setItem(`secondhand-operations:v1:${encodeURIComponent(account)}`, damage === 'corrupt' ? '{broken' : JSON.stringify([
        { version: 1, account, target: '1', kind: 'close', requestId: 'old-request', createdAt: 1, status: 'unknown', payload: { request_id: 'old-request', client_view_id: 'old-view' } },
      ]))
    }, { account: data.buyer, damage })
    await page.reload()
    await expect(page.locator('.message-list .offer-card')).toBeVisible()
    await expect(page.locator('.pending-operations')).toContainText('不能据此认定旧操作失败')
    expect((await inspect(request, data)).inputs).toHaveLength(1)
    await evidence(info, { damage, historyLoaded: true })
  })
}

for (const kind of ['approval', 'recovery']) {
  test(`R11 ${kind} before acceptance survives refresh and replays once`, async ({ page, request }, info) => {
    const data = await seed(request, kind === 'approval' ? 'approval' : 'failed')
    await open(page, data, kind === 'approval' ? '/seller/approvals' : '/seller/model-tasks')
    if (kind === 'recovery') await page.getByPlaceholder('填写本次恢复原因').fill('original reason')
    let original: unknown
    const path = kind === 'approval' ? '**/api/seller/approvals/*/approve' : '**/api/seller/graph-recovery/*'
    await page.route(path, async route => {
      if (route.request().method() !== 'POST') return route.continue()
      original = route.request().postDataJSON()
      await route.abort('failed')
    }, { times: 1 })
    await page.getByRole('button', { name: kind === 'approval' ? '同意报价' : '重试一次', exact: true }).click()
    await expect(page.getByRole('alert')).toBeVisible()
    await page.reload()
    let replay: unknown
    await page.route(path, async route => {
      if (route.request().method() !== 'POST') return route.continue()
      replay = route.request().postDataJSON()
      await route.continue()
    }, { times: 1 })
    await page.getByRole('button', { name: '重试原操作', exact: true }).click()
    await expect(page.locator('.pending-operations')).toContainText(kind === 'approval' ? '已核对原操作' : '已登记恢复操作')
    expect(replay).toEqual(original)
    const after = await inspect(request, data)
    expect(kind === 'approval' ? after.approval_decisions : after.recovery_actions).toBe(1)
    if (kind === 'recovery') expect(after.inputs[0].manual_retry_count).toBe(1)
    await evidence(info, { after, originalPayloadPreserved: true })
  })
}

async function uiLogin(page: Page, username: string) {
  await page.getByLabel('用户名', { exact: true }).fill(username)
  await page.getByLabel('密码', { exact: true }).fill('integration-test-password')
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.locator('.account-chip')).toContainText(username)
}

test('R06 real cookie A B A switch ignores late accepted response and clears old hints', async ({ page, request }, info) => {
  const a = await seed(request), b = await seed(request)
  await open(page, a)
  let release!: () => void, accepted!: () => void
  const gate = new Promise<void>(resolve => { release = resolve })
  const committed = new Promise<void>(resolve => { accepted = resolve })
  await page.route(`**/api/negotiations/${a.sid}/messages`, async route => {
    if (route.request().method() !== 'POST') return route.continue()
    const result = await route.fetch()
    expect(result.status()).toBe(202)
    accepted(); await gate
    await route.fulfill({ response: result })
  }, { times: 1 })
  await send(page, 'old account pending message')
  await committed
  await page.getByRole('button', { name: '退出', exact: true }).click()
  await uiLogin(page, b.buyer)
  await page.getByRole('button', { name: '退出', exact: true }).click()
  await uiLogin(page, a.buyer)
  await page.getByRole('link', { name: '买家模式', exact: true }).click()
  await page.getByRole('link', { name: '我的协商', exact: true }).click()
  await page.locator(`a[href="/negotiations/${a.sid}"]`).click()
  await expect(page.getByRole('textbox', { name: '聊天消息' })).toBeVisible()
  release()
  await page.waitForResponse(r => r.url().endsWith(`/api/negotiations/${a.sid}/messages`) && r.request().method() === 'POST')
  await expect(page.locator('.operation-status')).toHaveCount(0)
  await expect(page.locator('.outcome-banner')).toHaveCount(0)
  const cache = await page.evaluate(() => Object.entries(sessionStorage).filter(([key]) => key.startsWith('secondhand-operations:')))
  expect(cache).toEqual([])
  expect((await inspect(request, a)).inputs).toHaveLength(1)
  expect((await inspect(request, b)).inputs).toHaveLength(0)
  await evidence(info, { a: await inspect(request, a), b: await inspect(request, b), accountCacheKeys: cache.length })
})

test('R06 changed shared cookie cannot replay an old account operation', async ({ page, request }, info) => {
  const a = await seed(request, 'failed'), b = await seed(request)
  await open(page, a, '/seller/model-tasks')
  await page.getByPlaceholder('填写本次恢复原因').fill('cookie race')
  await page.route('**/api/seller/graph-recovery/*', async route => {
    if (route.request().method() !== 'POST') return route.continue()
    await route.abort('failed')
  }, { times: 1 })
  await page.getByRole('button', { name: '重试一次', exact: true }).click()
  await expect(page.getByRole('alert')).toBeVisible()
  // Change the shared HttpOnly cookie without notifying the old tab.
  await login(page.request, b.seller)
  let posts = 0
  page.on('request', r => { if (r.method() === 'POST' && r.url().includes('/graph-recovery/')) posts++ })
  await page.getByRole('button', { name: '重试原操作', exact: true }).click()
  await expect(page.getByLabel('用户名', { exact: true })).toBeVisible()
  expect(posts).toBe(0)
  expect((await inspect(request, a)).recovery_actions).toBe(0)
  await evidence(info, { posts, a: await inspect(request, a) })
})

test('R02 lookup 404 while original POST is in flight permits only identical explicit replay', async ({ page, request }, info) => {
  const data = await seed(request)
  await open(page, data)
  let id = ''
  await page.route(`**/api/negotiations/${data.sid}/messages`, async route => {
    if (route.request().method() !== 'POST') return route.continue()
    id = route.request().postDataJSON().request_id
    expect((await request.post(`/api/__predeploy/gate/${data.sid}/${id}/create`)).status()).toBe(200)
    await route.continue()
  }, { times: 1 })
  await send(page)
  await expect.poll(async () => id && (await (await request.post(`/api/__predeploy/gate/${data.sid}/${id}/status`)).json()).entered).toBe(true)
  expect((await inspect(request, data)).inputs).toHaveLength(0)
  await page.reload()
  await expect(page.locator('.pending-operations')).toContainText('暂未查到原请求')
  const lookupCount = (await inspect(request, data)).inputs.length
  await page.getByRole('button', { name: '重试原操作', exact: true }).click()
  await expect(page.locator('.outcome-banner')).toHaveCount(1)
  expect((await request.post(`/api/__predeploy/gate/${data.sid}/${id}/release`)).status()).toBe(200)
  const after = await inspect(request, data)
  expect(after.inputs).toHaveLength(1)
  expect(after.inputs[0].request_id).toBe(id)
  await evidence(info, { lookupCount, after, originalAndReplayReachedRealAPI: true })
})

test('R05 draft selection retains exact draft version and view after response loss', async ({ page, request }, info) => {
  const data = await seed(request, 'draft')
  await open(page, data)
  await loseAcceptedResponse(page, `**/api/negotiations/${data.sid}/messages`)
  await page.getByRole('button', { name: '选择并继续此方案', exact: true }).click()
  await expect(page.getByRole('alert')).toBeVisible()
  const before = await inspect(request, data)
  expect(before.inputs).toHaveLength(2)
  await page.reload()
  await expect(page.locator('.pending-operations')).toContainText('已核对原操作')
  const after = await inspect(request, data)
  expect(after.inputs).toEqual(before.inputs)
  await evidence(info, { before, after })
})

test('R06 same-account session switch isolates late response and restores original on return', async ({ page, request }, info) => {
  const a = await seed(request)
  const related = await request.post(`/api/__predeploy/seed/empty?buyer_sid=${a.sid}`)
  expect(related.status()).toBe(200)
  const b: Seed = await related.json()
  await open(page, a)
  let release!: () => void, accepted!: () => void
  const gate = new Promise<void>(resolve => { release = resolve })
  const committed = new Promise<void>(resolve => { accepted = resolve })
  await page.route(`**/api/negotiations/${a.sid}/messages`, async route => {
    if (route.request().method() !== 'POST') return route.continue()
    const result = await route.fetch()
    expect(result.status()).toBe(202)
    accepted(); await gate
    await route.fulfill({ response: result })
  }, { times: 1 })
  await send(page)
  await committed
  await page.getByRole('link', { name: '← 我的协商', exact: true }).click()
  await page.locator(`a[href="/negotiations/${b.sid}"]`).click()
  await expect(page.getByRole('textbox', { name: '聊天消息' })).toBeVisible()
  const lateResponse = page.waitForResponse(r => r.url().endsWith(`/api/negotiations/${a.sid}/messages`) && r.request().method() === 'POST')
  release(); await lateResponse
  await expect(page.locator('.operation-status')).toHaveCount(0)
  await expect(page.locator('.outcome-banner')).toHaveCount(0)
  await page.getByRole('link', { name: '← 我的协商', exact: true }).click()
  await page.locator(`a[href="/negotiations/${a.sid}"]`).click()
  await expect(page.locator('.pending-operations')).toContainText('已核对原操作')
  expect((await inspect(request, a)).inputs).toHaveLength(1)
  expect((await inspect(request, b)).inputs).toHaveLength(0)
  await evidence(info, { a: await inspect(request, a), b: await inspect(request, b) })
})

test('R04 rejecting a retry cannot erase an original POST that later commits', async ({ page, request }, info) => {
  const data = await seed(request)
  await open(page, data)
  let id = ''
  await page.route(`**/api/negotiations/${data.sid}/messages`, async route => {
    if (route.request().method() !== 'POST') return route.continue()
    id = route.request().postDataJSON().request_id
    expect((await request.post(`/api/__predeploy/gate/${data.sid}/${id}/create`)).status()).toBe(200)
    await route.continue()
  }, { times: 1 })
  await send(page)
  await expect.poll(async () => id && (await (await request.post(`/api/__predeploy/gate/${data.sid}/${id}/status`)).json()).entered).toBe(true)
  await page.reload()
  await page.route(`**/api/negotiations/${data.sid}/messages`, async route => {
    if (route.request().method() !== 'POST') return route.continue()
    await route.fulfill({ status: 409, json: { detail: '本次重试冲突' } })
  }, { times: 1 })
  await page.getByRole('button', { name: '重试原操作', exact: true }).click()
  await expect(page.locator('.pending-operations')).toContainText('此次重试已被拒绝，原操作仍需核对')
  await expect(page.getByRole('button', { name: '重试原操作', exact: true })).toHaveCount(0)
  await page.reload()
  await expect(page.getByRole('button', { name: '核对原操作', exact: true })).toBeEnabled()
  expect((await request.post(`/api/__predeploy/gate/${data.sid}/${id}/release`)).status()).toBe(200)
  await page.getByRole('button', { name: '核对原操作', exact: true }).click()
  await expect(page.locator('.pending-operations')).toContainText('已核对原操作')
  expect((await inspect(request, data)).inputs).toHaveLength(1)
  await evidence(info, { source: 'real original POST delayed at enqueue; retry response injected as 409', after: await inspect(request, data) })
})

for (const kind of ['approval', 'recovery']) {
  test(`R11 ${kind} still in flight and its explicit replay share one decision`, async ({ page, request }, info) => {
    const data = await seed(request, kind === 'approval' ? 'approval' : 'failed')
    const before = await inspect(request, data)
    const target = kind === 'approval' ? String(before.approvals[0].id) : data.run!
    await open(page, data, kind === 'approval' ? '/seller/approvals' : '/seller/model-tasks')
    if (kind === 'recovery') await page.getByPlaceholder('填写本次恢复原因').fill('in flight original reason')
    let id = ''
    const control = (action: string) => `/api/__predeploy/seller-gate/${kind}/${target}/${id}/${action}`
    const path = kind === 'approval' ? '**/api/seller/approvals/*/approve' : '**/api/seller/graph-recovery/*'
    await page.route(path, async route => {
      if (route.request().method() !== 'POST') return route.continue()
      id = route.request().postDataJSON().request_id
      expect((await request.post(control('create'))).status()).toBe(200)
      await route.continue()
    }, { times: 1 })
    await page.getByRole('button', { name: kind === 'approval' ? '同意报价' : '重试一次', exact: true }).click()
    await expect.poll(async () => id && (await (await request.post(control('status'))).json()).entered).toBe(true)
    await page.reload()
    await expect(page.locator('.pending-operations')).toContainText('暂未查到原请求')
    await page.getByRole('button', { name: '重试原操作', exact: true }).click()
    await expect(page.locator('.pending-operations')).toContainText(kind === 'approval' ? '已核对原操作' : '已登记恢复操作')
    expect((await request.post(control('release'))).status()).toBe(200)
    const after = await inspect(request, data)
    expect(kind === 'approval' ? after.approval_decisions : after.recovery_actions).toBe(1)
    if (kind === 'recovery') {
      expect(after.inputs[0].manual_retry_count).toBe(1)
      expect(after.inputs[0].config_hash).toBe(before.inputs[0].config_hash)
    }
    await evidence(info, { before, after, originalAndReplayReachedRealAPI: true })
  })
}
