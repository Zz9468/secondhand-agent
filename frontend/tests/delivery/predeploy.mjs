// Explicit, charged acceptance check against an isolated production Compose project.
// No test routes, mocked model, stored cookies, or browser traces.
import { chromium, expect } from '@playwright/test'
import { execFileSync } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'

const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'))
if (!/^secondhand-p6-[a-f0-9]{10}$/.test(config.project)) throw new Error('Isolated project required')
const report = { phase: config.phase, status: 'RUNNING', checks: [], requests: [] }
const output = config.output
fs.mkdirSync(output, { recursive: false })
const browser = await chromium.launch({ channel: process.env.PLAYWRIGHT_CHANNEL || undefined })
report.browser = browser.version()
const buyer = await browser.newContext({ baseURL: config.url })
const seller = await browser.newContext({ baseURL: config.url })
const page = await buyer.newPage()
const sellerPage = await seller.newPage()
const checked = name => report.checks.push(name)
const compose = (...args) => {
  const result = execFileSync('docker', ['compose', '--env-file', config.envFile, '-p', config.project, ...args],
    { cwd: config.root, encoding: 'utf8', timeout: 120_000 })
  fs.appendFileSync(path.join(output, 'compose.log'), result)
}
async function api(context, method, url, data) {
  if (url === '/api/auth/login') data = { username: data.username, password: data.password }
  const response = await context.request.fetch(url, { method, data })
  if (!response.ok()) throw new Error(`${method} ${url}: HTTP ${response.status()}`)
  return response.json()
}
const credentialsPath = config.credentials
let credentials
async function send(text) {
  await page.getByRole('textbox', { name: '聊天消息' }).fill(text)
  await page.getByRole('button', { name: '发送', exact: true }).click()
}
async function loseResponse(url, expected) {
  await page.route(url, async route => {
    if (route.request().method() !== 'POST') return route.continue()
    const response = await route.fetch()
    expect(response.status()).toBe(expected)
    const result = await response.json()
    report.requests.push({ request_id: route.request().postDataJSON().request_id, run_id: result.run_id })
    await route.abort('failed')
  }, { times: 1 })
}
async function run() {
  if (config.phase === 'source') {
    credentials = Object.fromEntries(['buyer', 'seller', 'other'].map(role => [role, {
      username: `d5-${role}-${randomBytes(5).toString('hex')}`,
      password: randomBytes(20).toString('hex') + 'Aa1!', display_name: `D5 ${role}`,
    }]))
    fs.writeFileSync(credentialsPath, JSON.stringify(credentials), { flag: 'wx' })
    await api(buyer, 'POST', '/api/auth/register', credentials.buyer)
    await api(seller, 'POST', '/api/auth/register', credentials.seller)
    const product = await api(seller, 'POST', '/api/products', {
      title: 'D5浏览器演示相机', description: '黑色相机，支持自提。', listed_price: '1000', status: 'AVAILABLE',
      policy: { minimum_net_price: '750', auto_accept_threshold: '850' },
    })
    await api(seller, 'PUT', `/api/seller/products/${product.id}/facts/allowed_delivery_methods`,
      { expected_version: 1, value: ['pickup'] })
    credentials.product = product.id
    credentials.sid = (await api(buyer, 'POST', '/api/negotiations', { product_id: product.id })).session_id
    fs.writeFileSync(credentialsPath, JSON.stringify(credentials))
    const sid = credentials.sid
    report.sid = sid
    await page.goto(`/negotiations/${sid}`)
    await expect(page.getByRole('textbox', { name: '聊天消息' })).toBeVisible()
    // Stop the real worker before accepting input, proving refresh recovery while queued.
    compose('stop', 'worker')
    await loseResponse(`**/api/negotiations/${sid}/messages`, 202)
    await send('我出800元自提，请提交这个报价。')
    await expect(page.getByRole('alert')).toBeVisible()
    await page.reload()
    await expect(page.locator('.pending-operations')).toContainText('已核对原操作')
    await expect(page.locator('.outcome-banner')).toContainText('已排队')
    await page.screenshot({ path: path.join(output, 'queued-after-refresh.png'), fullPage: true })
    const original = report.requests[0]
    expect((await api(buyer, 'GET', `/api/negotiations/${sid}/requests/${original.request_id}`)).run_id).toBe(original.run_id)
    checked('accepted_response_lost_refresh_preserves_original_queued_run')
    compose('start', 'worker')
    compose('restart', 'watchdog')
    await expect(page.locator('.approval-waiting')).toBeVisible({ timeout: 120_000 })
    await expect.poll(async () => (await api(buyer, 'GET',
      `/api/negotiations/${sid}/runs/${original.run_id}`)).task_status,
    { timeout: 120_000 }).toBe('SUCCEEDED')
    checked('production_worker_restart_updates_existing_browser')
    await sellerPage.goto('/seller/approvals')
    await sellerPage.locator('.approval-list-item').filter({ hasText: 'D5浏览器演示相机' }).click()
    // Seller decision commits while the actual notification/apply worker is stopped.
    compose('stop', 'worker')
    await sellerPage.getByRole('button', { name: '同意报价', exact: true }).click()
    await expect(sellerPage.locator('.approval-list-item').filter({ hasText: 'D5浏览器演示相机' }))
      .toContainText('已同意')
    await expect(sellerPage.getByText('等待审批结果应用', { exact: false })).toBeVisible()
    await expect(page.getByRole('button', { name: '确认交易意向', exact: true })).toHaveCount(0)
    await sellerPage.screenshot({ path: path.join(output, 'approval-awaiting-worker.png'), fullPage: true })
    checked('seller_decision_does_not_prematurely_enable_confirmation')
    compose('start', 'worker')
    const confirm = page.getByRole('button', { name: '确认交易意向', exact: true })
    await expect(confirm).toBeEnabled({ timeout: 120_000 })
    await page.screenshot({ path: path.join(output, 'notified-confirmable.png'), fullPage: true })
    checked('delayed_approval_notification_arrives_without_page_reload')
    await loseResponse(`**/api/negotiations/${sid}/confirm`, 202)
    await confirm.click()
    await expect(page.getByRole('alert')).toBeVisible()
    await page.reload()
    await expect(page.getByText('已记录交易意向', { exact: true })).toBeVisible({ timeout: 120_000 })
    await expect.poll(async () => (await api(buyer, 'GET',
      `/api/negotiations/${sid}/runs/${report.requests[1].run_id}`)).task_status,
    { timeout: 120_000 }).toBe('SUCCEEDED')
    checked('confirmation_response_lost_refresh_reaches_agreed')
    // Switching real shared cookies must not expose another buyer's operation or history.
    await api(buyer, 'POST', '/api/auth/register', credentials.other)
    await page.reload()
    expect((await buyer.request.get(`/api/negotiations/${sid}`)).status()).toBe(404)
    expect((await buyer.request.get(`/api/negotiations/${sid}/requests/${original.request_id}`)).status()).toBe(404)
    await expect(page.locator('.message-list')).toHaveCount(0)
    await api(buyer, 'POST', '/api/auth/login', credentials.buyer)
    await page.reload()
    await expect(page.getByText('已记录交易意向', { exact: true })).toBeVisible()
    checked('real_cookie_account_switch_isolates_history_and_request_lookup')
  } else {
    credentials = JSON.parse(fs.readFileSync(credentialsPath, 'utf8'))
    report.sid = credentials.sid
    await api(buyer, 'POST', '/api/auth/login', credentials.buyer)
    await api(seller, 'POST', '/api/auth/login', credentials.seller)
    await page.goto(`/negotiations/${credentials.sid}`)
    await expect(page.getByText('已记录交易意向', { exact: true })).toBeVisible()
    const approvals = (await api(seller, 'GET', '/api/seller/approvals')).approvals
    expect(approvals.some(a => a.session_id === credentials.sid && a.status === 'APPROVED')).toBe(true)
    await api(buyer, 'POST', '/api/auth/login', credentials.other)
    expect((await buyer.request.get(`/api/negotiations/${credentials.sid}`)).status()).toBe(404)
    await api(buyer, 'POST', '/api/auth/login', credentials.buyer)
    checked('restored_accounts_history_approval_and_ownership')
    const sid = (await api(buyer, 'POST', '/api/negotiations', { product_id: credentials.product })).session_id
    report.new_sid = sid
    await page.goto(`/negotiations/${sid}`)
    await send('这台相机是什么颜色？')
    await expect(page.locator('.message[data-role="AGENT"]')).toContainText('黑', { timeout: 120_000 })
    const closing = page.waitForResponse(response => response.url().endsWith(`/api/negotiations/${sid}/close`)
      && response.request().method() === 'POST')
    await page.getByRole('button', { name: '结束协商', exact: true }).click()
    const closeResponse = await closing
    expect(closeResponse.status()).toBe(202)
    const closeRun = (await closeResponse.json()).run_id
    await expect(page.getByText('协商已结束', { exact: true })).toBeVisible({ timeout: 120_000 })
    await expect.poll(async () => (await api(buyer, 'GET',
      `/api/negotiations/${sid}/runs/${closeRun}`)).task_status,
    { timeout: 120_000 }).toBe('SUCCEEDED')
    await expect.poll(async () => (await api(buyer, 'GET',
      `/api/negotiations/${sid}/runs/${closeRun}`)).notification_status,
    { timeout: 120_000 }).toBe('PUBLISHED')
    await expect(page.locator('.queue-notice')).toHaveCount(0, { timeout: 15_000 })
    await expect(page.locator('.outcome-banner').last()).toHaveText('输入 #2：协商已结束')
    checked('restored_application_new_real_model_input_and_close')
  }
  await page.screenshot({ path: path.join(output, 'final.png'), fullPage: true })
  report.status = 'PASS'
}
try { await run() } catch (error) {
  report.status = 'FAIL'
  report.error = String(error)
  await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true }).catch(() => {})
  process.exitCode = 1
} finally {
  fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify(report, null, 2))
  await browser.close()
}
