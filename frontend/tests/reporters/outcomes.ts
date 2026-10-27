import fs from 'node:fs'
import path from 'node:path'
import type { FullResult, Reporter, TestCase, TestResult } from '@playwright/test/reporter'

// Playwright's default "passed" total includes expected failures. Keep these distinct.
export default class Outcomes implements Reporter {
  private cases: { title: string; result: string; durationMs: number }[] = []
  onTestEnd(test: TestCase, result: TestResult) {
    const outcome = test.expectedStatus === 'failed' && result.status === 'failed' ? 'expected-failure'
      : test.expectedStatus === 'passed' && result.status === 'passed' ? 'passed' : 'unexpected'
    this.cases.push({ title: test.title, result: outcome, durationMs: result.duration })
  }
  onEnd(result: FullResult) {
    const counts = { passed: 0, expectedFailure: 0, unexpected: 0 }
    for (const item of this.cases) {
      if (item.result === 'passed') counts.passed++
      else if (item.result === 'expected-failure') counts.expectedFailure++
      else counts.unexpected++
    }
    const report = path.resolve(process.env.PREDEPLOY_REPORT_DIR ?? '../artifacts/predeploy/local-browser')
    fs.mkdirSync(report, { recursive: true })
    fs.writeFileSync(path.join(report, 'outcomes.json'), JSON.stringify({ status: result.status, counts, cases: this.cases }, null, 2))
    process.stdout.write(`\nPredeploy: ${counts.passed} passed; ${counts.expectedFailure} expected failures (unfixed defects); ${counts.unexpected} unexpected.\n`)
  }
}
