#Requires -Version 5.1
# ============================================================
#  AI 简历引擎 · 完整使用流程演示
#  1)解析岗位JD  2)匹配打分  3)STAR优化  4)自我评价
#  5)HTML预览    6)导出PDF    7)导出Word
#  用法: 在 ai-resume-engine 目录下执行  .\demo.ps1
# ============================================================

param(
    [string]$Server = "http://127.0.0.1:8000",
    [string]$OutDir  = "output"
)

$ErrorActionPreference = "Stop"
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

function Call-API {
    param([string]$Path, [object]$Body)
    return Invoke-RestMethod -Uri "$Server$Path" -Method Post -ContentType "application/json" `
        -Body ($Body | ConvertTo-Json -Depth 20 -Compress)
}

# --- 0. 确保服务运行 ---
if (-not (Test-NetConnection 127.0.0.1 -Port 8000 -WarningAction SilentlyContinue).TcpTestSucceeded) {
    Write-Host "[0/8] 未检测到服务，正在后台启动..." -ForegroundColor Cyan
    Start-Process -FilePath "python" -ArgumentList "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "8000" `
        -WorkingDirectory $ScriptRoot -WindowStyle Hidden
    Start-Sleep -Seconds 6
}
if (-not (Test-NetConnection 127.0.0.1 -Port 8000 -WarningAction SilentlyContinue).TcpTestSucceeded) {
    Write-Host "[错误] 服务启动失败，请手动执行: python -m uvicorn app:app --port 8000" -ForegroundColor Red
    exit 1
}
Write-Host "[0/8] 服务已就绪: $Server" -ForegroundColor Green

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

# --- 1. 岗位 JD ---
Write-Host "[1/8] 解析岗位 JD..." -ForegroundColor Cyan
$jd = @{ job_description = "高级后端工程师，负责高并发交易系统架构设计与开发，要求 5 年以上 Java 经验，熟悉 Spring Cloud、MySQL、Redis、Kafka，具备分布式系统与微服务治理经验，本科及以上学历。" }
$parsed = Call-API -Path "/parse-jd" -Body $jd
Write-Host ("      职位: {0} | 学历: {1} | 年限: {2}" -f $parsed.job_title, $parsed.hard_requirements.education, $parsed.hard_requirements.experience_years)
Write-Host ("      技能: {0}" -f ($parsed.hard_requirements.required_skills -join ", "))

# --- 2. 匹配打分 ---
Write-Host "[2/8] 岗位匹配打分..." -ForegroundColor Cyan
$userResume = @{
    basic     = @{ name = "李四"; job_intention = "高级后端工程师" }
    work      = @(@{ company = "某电商公司"; position = "Java 开发工程师"; start = "2019.07"; end = "至今"; responsibilities = @("负责订单系统核心模块开发与维护") })
    education = @(@{ school = "某大学"; degree = "本科"; major = "计算机科学与技术"; graduation = "2019.06" })
    skills    = @{ skills = @("Java", "Spring Cloud", "MySQL", "Redis"); certificates = @(); languages = @() }
}
$match = Call-API -Path "/match-score" -Body @{ parsed_jd = $parsed; user_resume = $userResume }
Write-Host ("      匹配度: {0}/100" -f $match.total_score)
foreach ($g in $match.gaps) { Write-Host ("      缺口: {0} -> {1}" -f $g.requirement, $g.suggestion) }

# --- 3. STAR 优化 ---
Write-Host "[3/8] STAR 优化经历..." -ForegroundColor Cyan
$star = Call-API -Path "/optimize-star" -Body @{
    job_keywords = @("Java", "高并发", "微服务")
    experiences  = @(@{ text = "负责订单模块开发，处理了大量订单数据" }, @{ text = "参与支付系统重构，提高了系统稳定性" })
}
foreach ($it in $star.optimized_items) { Write-Host ("      - {0}" -f $it.optimized) }

# --- 4. 自我评价 ---
Write-Host "[4/8] 优化自我评价..." -ForegroundColor Cyan
$se = Call-API -Path "/optimize-self-eval" -Body @{
    job_keywords      = @("Java", "高并发")
    self_evaluation   = "我有多年 Java 经验，喜欢学习新技术。"
    resume_highlights = @("订单系统开发", "支付重构")
}
Write-Host ("      {0} (字数 {1})" -f $se.optimized, $se.word_count)

# --- 5. 完整简历（用于导出） ---
$fullResume = @{
    basic     = @{ name = "李四"; phone = "13800000000"; email = "lisi@example.com"; city = "上海"; job_intention = "高级后端工程师" }
    education = @(@{ school = "某大学"; degree = "本科"; major = "计算机科学与技术"; graduation = "2019.06" })
    work      = @(@{ company = "某电商公司"; position = "Java 开发工程师"; start = "2019.07"; end = "至今"; responsibilities = @("负责订单系统核心模块开发与维护", "主导支付系统重构") })
    projects  = @(@{ name = "交易链路重构"; role = "核心开发"; start = "2023.01"; end = "2023.12"; description = @("重构支付核心链路"); achievements = @("稳定性提升 99.99%") })
    skills    = @{ skills = @("Java", "Spring Cloud", "MySQL", "Redis", "Kafka"); certificates = @(); languages = @("英语 CET-6") }
    self_evaluation = "5 年 Java 后端经验，专注高并发系统。"
    sensitive_fields = @("phone", "email")
}

# --- 6. HTML 预览 ---
Write-Host "[5/8] 生成 HTML 预览..." -ForegroundColor Cyan
$htmlPath = Join-Path $OutDir "preview.html"
Invoke-WebRequest -Uri "$Server/render-preview" -Method Post -ContentType "application/json" `
    -Body (@{ resume = $fullResume; template_id = "modern"; hide_sensitive = $true } | ConvertTo-Json -Depth 20 -Compress) -OutFile $htmlPath
Write-Host "      -> $htmlPath"

# --- 7. PDF ---
Write-Host "[6/8] 导出 PDF..." -ForegroundColor Cyan
$pdfPath = Join-Path $OutDir "resume_modern.pdf"
Invoke-WebRequest -Uri "$Server/export-pdf" -Method Post -ContentType "application/json" `
    -Body (@{ resume = $fullResume; template_id = "modern"; hide_sensitive = $true } | ConvertTo-Json -Depth 20 -Compress) -OutFile $pdfPath
Write-Host "      -> $pdfPath"

# --- 8. Word ---
Write-Host "[7/8] 导出 Word..." -ForegroundColor Cyan
$docxPath = Join-Path $OutDir "resume_classic.docx"
Invoke-WebRequest -Uri "$Server/export-docx" -Method Post -ContentType "application/json" `
    -Body (@{ resume = $fullResume; template_id = "classic"; hide_sensitive = $false } | ConvertTo-Json -Depth 20 -Compress) -OutFile $docxPath
Write-Host "      -> $docxPath"

Write-Host ""
Write-Host "[8/8] 全部完成！产物目录: $(Resolve-Path $OutDir)" -ForegroundColor Green