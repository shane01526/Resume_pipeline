<#
.SYNOPSIS
    把 sources/ 裡新丟的文件推上 GitHub。

.DESCRIPTION
    這是「投料」的動作。推上去不會馬上觸發 pipeline — 下一次定時執行（台北時間每週一 03:00）
    會撿到；想立刻跑就在 Slack 打 /resume update。

    刻意只 stage sources/：output/ 與 state/ 是 pipeline 自己 commit 的，
    從本機一起推上去會和 Cloud Run 上的執行搶著寫同一批檔案。

    為什麼一定要 push、光放本機沒用：Cloud Run 上的 container 沒有 git checkout，
    它是透過 GitHub API 讀 sources/ 的。檔案只存在你硬碟上時，服務看不到。

.EXAMPLE
    .\scripts\push_sources.ps1
    .\scripts\push_sources.ps1 -Message "加入 2026 上半年國泰專案文件"
#>
[CmdletBinding()]
param(
    [string]$Message
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

if (-not (Test-Path 'sources')) {
    Write-Error "找不到 sources/ 資料夾。請確認你在 repo 根目錄執行。"
}

# 只看 sources/，其餘變更留給 pipeline 自己處理。
$changes = git status --porcelain -- sources
if (-not $changes) {
    Write-Host "sources/ 沒有新變更。" -ForegroundColor Yellow
    Write-Host "把文件（.pdf / .docx / .pptx / .md / .txt）複製進 sources\ 之後再跑一次。"
    exit 0
}

Write-Host "`n將推送以下變更：" -ForegroundColor Cyan
$changes -split "`n" | ForEach-Object {
    if ($_) { Write-Host "  $_" }
}

if (-not $Message) {
    $count = ($changes -split "`n" | Where-Object { $_ }).Count
    $Message = "Add $count source file(s) for resume extraction"
}

Write-Host ""
git add sources
git commit -m $Message

# 先 pull --rebase：pipeline 會 commit output/ 與 state/，本機幾乎一定落後。
Write-Host "`n同步遠端…" -ForegroundColor Cyan
git pull --rebase --autostash origin main
git push origin main

Write-Host "`n✅ 已推送。" -ForegroundColor Green
Write-Host "   下次定時執行（台北時間週一 03:00）會處理這些文件。"
Write-Host "   想立刻跑：在 Slack 打  /resume update"
Write-Host ""
Write-Host "   接下來會發生什麼：LLM 從這些文件抽出候選項目，寫進 Notion 的"
Write-Host "   Experiences / Projects / Skills，狀態是 Pending Review 且沒勾 Include。"
Write-Host "   要進履歷得你自己在 Notion 改成 Approved 並勾 Include in Resume —"
Write-Host "   同一次執行不會把剛抽到的東西放進履歷。"
