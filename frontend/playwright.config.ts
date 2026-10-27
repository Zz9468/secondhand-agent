import { defineConfig } from '@playwright/test'
import path from 'node:path'

if (!process.env.PREDEPLOY_API_URL) {
  throw new Error('Use backend: python -m scripts.run_p0_tests tests/graph/test_predeploy_browser.py (isolated databases required)')
}
const report = path.resolve(process.env.PREDEPLOY_REPORT_DIR ?? '../artifacts/predeploy/local-browser')
export default defineConfig({
  testDir: './tests/browser', workers: 1, retries: 0, timeout: 30_000,
  expect: { timeout: 5_000 },
  reporter: [['list'], ['./tests/reporters/outcomes.ts'], ['json', { outputFile: path.join(report, 'browser.json') }], ['junit', { outputFile: path.join(report, 'browser.xml') }]],
  outputDir: path.join(report, 'browser-results'),
  use: { baseURL: 'http://127.0.0.1:15175', browserName: 'chromium',
    channel: process.env.PLAYWRIGHT_CHANNEL || undefined,
    // No traces: they could retain full HTTP payloads and seller policy responses.
    trace: 'off', screenshot: 'only-on-failure' },
  webServer: { command: 'npm run dev -- --host 127.0.0.1 --port 15175 --strictPort',
    url: 'http://127.0.0.1:15175', reuseExistingServer: false,
    env: { VITE_API_PROXY_TARGET: process.env.PREDEPLOY_API_URL } },
})
