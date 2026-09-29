param(
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

if (-not (Test-Path -LiteralPath ".env")) {
    throw "缺少 .env；请先复制 .env.example 并替换全部 CHANGE_ME。"
}

# 兼容阶段八以前创建的 .env：只在未显式配置容器地址时，将本机数据库主机名
# 改为 Compose 服务名。值只写入当前进程环境，不回写也不输出包含口令的 URL。
if (-not $env:CONTAINER_DATABASE_URL) {
    $containerUrlLine = Get-Content -LiteralPath ".env" |
        Where-Object { $_ -match '^CONTAINER_DATABASE_URL=' } |
        Select-Object -First 1
    if (-not $containerUrlLine) {
        $databaseUrlLine = Get-Content -LiteralPath ".env" |
            Where-Object { $_ -match '^DATABASE_URL=' } |
            Select-Object -First 1
        if (-not $databaseUrlLine) {
            throw "缺少 DATABASE_URL 或 CONTAINER_DATABASE_URL。"
        }
        $databaseUrl = $databaseUrlLine.Substring("DATABASE_URL=".Length)
        $env:CONTAINER_DATABASE_URL = $databaseUrl `
            -replace '@(127\.0\.0\.1|localhost):3306', '@mysql:3306'
    }
}

$gitCommit = (& git rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $gitCommit -notmatch '^[0-9a-fA-F]{40,64}$') {
    throw "无法读取完整 Git 提交号，不能生成可追溯评测报告。"
}
$gitWorktreeState = & git status --porcelain
if ($LASTEXITCODE -ne 0) {
    throw "无法读取 Git 工作区状态。"
}
$env:EVALUATION_GIT_COMMIT = $gitCommit
if ($gitWorktreeState) {
    $env:EVALUATION_GIT_WORKTREE_DIRTY = "true"
}
else {
    $env:EVALUATION_GIT_WORKTREE_DIRTY = "false"
}

$composeArgs = @("compose", "up", "-d")
if (-not $SkipBuild) {
    $composeArgs += "--build"
}
$composeArgs += @("mysql", "migrate", "seed", "api", "worker", "frontend")
& docker @composeArgs
if ($LASTEXITCODE -ne 0) {
    throw "Docker Compose 启动失败。"
}

$ready = $false
for ($attempt = 1; $attempt -le 30; $attempt++) {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/health" -TimeoutSec 2
        if ($health.status -eq "ok") {
            $ready = $true
            break
        }
    }
    catch {
        Start-Sleep -Seconds 2
    }
}
if (-not $ready) {
    throw "API 在 60 秒内未就绪，请执行 docker compose logs api。"
}

$frontendHealth = Invoke-RestMethod `
    -Uri "http://127.0.0.1:5173/api/health" `
    -TimeoutSec 5
if ($frontendHealth.status -ne "ok") {
    throw "前端反向代理未能访问 API。"
}

& docker compose exec -T api python scripts/demo_v3.py --api-url http://127.0.0.1:8000
if ($LASTEXITCODE -ne 0) {
    throw "模式 B 闭环演示失败。"
}

$batchId = "v3-demo-" + [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")
& docker compose exec -T api python -m evaluation.cli `
    --batch-id $batchId `
    --max-samples 24 `
    --max-model-calls 30 `
    --max-tokens 30000 `
    --timeout-seconds 120
if ($LASTEXITCODE -ne 0) {
    throw "确定性评测或安全门禁失败。"
}

$reportPath = "backend/evaluation/results/$batchId/report.md"
if (-not (Test-Path -LiteralPath $reportPath)) {
    throw "评测通过但未找到报告：$reportPath"
}

Write-Output "V3 本地复现完成。"
Write-Output "前端：http://127.0.0.1:5173"
Write-Output "报告：$reportPath"
Write-Output "停止服务：docker compose stop frontend worker api"
