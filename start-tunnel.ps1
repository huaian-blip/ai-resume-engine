# 一键启动后端 + Cloudflare 临时隧道，并自动更新 docs/config.js
#
# 用法：
#   .\start-tunnel.ps1            # 只起服务并更新本地 config.js
#   .\start-tunnel.ps1 -Push      # 额外 git commit + push，线上立即生效
#
# 说明：
#   - 隧道为 trycloudflare 临时地址，每次运行都会变化；
#   - 脚本会清理旧的 cloudflared 进程并重新起隧道；
#   - 后端若已在 8000 端口运行则复用，不会重复启动。

param(
    [switch]$Push
)

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
$logDir = Join-Path $env:TEMP 'opencode\deploy'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null

# cloudflared 可能不在当前进程 PATH（如安装后未重启），显式解析可执行文件
function Get-CloudflaredExe {
    $candidates = @(
        (Join-Path ${env:ProgramFiles(x86)} 'cloudflared\cloudflared.exe'),
        (Join-Path ${env:ProgramFiles} 'cloudflared\cloudflared.exe'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\cloudflared.exe')
    )
    foreach ($c in $candidates) {
        if ($c -and (Test-Path -LiteralPath $c)) { return $c }
    }
    $env:Path = [System.Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [System.Environment]::GetEnvironmentVariable('Path', 'User')
    $found = (Get-Command cloudflared.exe -ErrorAction SilentlyContinue | Select-Object -First 1).Source
    if ($found) { return $found }
    return 'cloudflared'
}
$cfExe = Get-CloudflaredExe

# ---------- 1. 后端 ----------
$listening = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if (-not $listening) {
    Write-Host "[1/3] 启动后端 uvicorn ..."
    Start-Process -FilePath "python" -ArgumentList "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "8000" `
        -WorkingDirectory $Root `
        -RedirectStandardOutput "$logDir\uvicorn.log" `
        -RedirectStandardError "$logDir\uvicorn.err.log" `
        -WindowStyle Hidden
    Start-Sleep -Seconds 6
    if (-not (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue)) {
        Write-Host "   [!] 后端启动失败，日志：$logDir\uvicorn.err.log" -ForegroundColor Red
        exit 1
    }
    Write-Host "   后端已启动 (127.0.0.1:8000)"
} else {
    Write-Host "[1/3] 后端已在运行 (127.0.0.1:8000)，复用"
}

# ---------- 2. 隧道 ----------
Get-Process cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 1
Write-Host "[2/3] 启动 cloudflared 隧道 ..."
Start-Process -FilePath $cfExe -ArgumentList "tunnel", "--url", "http://127.0.0.1:8000" `
    -RedirectStandardOutput "$logDir\cf.log" `
    -RedirectStandardError "$logDir\cf.err.log" `
    -WindowStyle Hidden

$url = $null
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 2
    $m = Select-String -Path "$logDir\cf.err.log" -Pattern "https://[a-z0-9-]+\.trycloudflare\.com" -AllMatches -ErrorAction SilentlyContinue
    if ($m) { $url = $m[0].Matches[0].Value; break }
}
if (-not $url) {
    Write-Host "   [!] 隧道启动失败，日志：$logDir\cf.err.log" -ForegroundColor Red
    exit 1
}
Write-Host "   隧道地址: $url"

# ---------- 3. 更新 config.js（不写 BOM，避免影响 JS 解析） ----------
$cfg = Join-Path $Root 'docs\config.js'
$text = Get-Content $cfg -Raw -Encoding UTF8
$text = $text -replace "(window\.__API_BASE__\s*=\s*')[^']*(')", "`$1$url`$2"
[System.IO.File]::WriteAllText($cfg, $text, (New-Object System.Text.UTF8Encoding($false)))
Write-Host "[3/3] 已更新 docs/config.js"

# ---------- 4. 可选推送 ----------
if ($Push) {
    Write-Host "   提交并推送 GitHub Pages ..."
    Push-Location $Root
    git add docs/config.js
    git commit -m "chore: update tunnel url" --quiet
    git push --quiet
    Pop-Location
    Write-Host "   已推送，约 1 分钟后线上生效"
}

Write-Host ""
Write-Host "网站: https://huaian-blip.github.io/ai-resume-engine/"
Write-Host "隧道: $url （后台运行，保持 cloudflared 进程存活即可）"