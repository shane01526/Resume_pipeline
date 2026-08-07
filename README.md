# Resume Pipeline

依實習與專案經驗自動更新履歷。Notion 是唯一真實來源，中英各一份、三種格式，
**定時執行只會產生待審核的版本，永遠不會自己發布** — 一定要你看過 before/after 才會套用。

```
sources/ 丟文件 ──▶ LLM 抽取 ──▶ Notion 待審 ──▶ 你在 Notion 確認
                                                      │
                                    ┌─────────────────┘
                                    ▼
        讀 Notion(Approved) ──▶ 中英翻譯 ──▶ 產出 6 個檔案 ──▶ diff
                                                                │
                                                Slack 通知 ⏸ 等你核准
                                                                │
                              ┌─────────────────────────────────┘
                              ▼
              git commit + Notion 附件 + 固定下載連結
```

- **Notion**：[履歷資料庫 / Resume Master](https://app.notion.com/p/3b525b0d3537813ebc87e48ed8f823bc)
- **產出**：`output/{en,zh}/resume.pdf`、`resume.latex.pdf`、`resume.docx`

---

## 日常使用

| 情境 | 你做什麼 |
| --- | --- |
| 實習結束、手上有 PRD / 報告 / 結案簡報 | 丟進 `sources/`，跑 `.\scripts\push_sources.ps1` |
| 收到 Slack 通知 | 點 diff 連結看 before/after → **核准** 或 **駁回** |
| Slack 說有新的待審項目 | 開 Notion 篩 `Status = Pending Review` → 改字、勾 `Include in Resume`、改成 `Approved` |
| 臨時要投遞、想立刻更新 | Slack 打 `/resume update` |
| 手機上想到一條沒寫的成果 | 直接在 Notion 對應那筆的頁面內文加一條 bullet |
| 想拿最新檔 | `/resume latest`，或直接開固定連結 |

固定下載連結（永遠指向最新核准版本，可放進 email 簽名或 GitHub profile）：

```
https://<你的-cloud-run-網址>/resume/en.pdf          .../resume/zh.pdf
https://<你的-cloud-run-網址>/resume/en.latex.pdf    .../resume/zh.latex.pdf
https://<你的-cloud-run-網址>/resume/en.docx         .../resume/zh.docx
```

---

## 兩個安全性質

這個系統的設計繞著兩件事轉，改動時請不要破壞：

1. **定時執行不會自己發布。** 排程只產生「待核准 run」，發布只發生在你按核准之後
   （`pipeline/publish.py`）。所以排程可以設得很積極而不會有風險。
2. **pipeline 不會覆寫你核准過的內容。** 抽取到的東西若命中已 `Approved` 的項目，
   只會在那一筆下留 Notion 留言建議，永遠不發 PATCH（`pipeline/reconcile.py`，
   由 `tests/test_reconcile.py` 守住）。

另外：**沒有差異就不發通知**。每週固定收到一則「沒有變化」會訓練你忽略這個頻道。

---

## 初次設定

### 前置條件

| 需要什麼 | 怎麼裝 / 確認 |
| --- | --- |
| **Python 3.12+** | `python --version`（本機開發與跑腳本用） |
| **Git Bash** | Windows 上跑 `deploy_cloudrun.sh` 需要（Git for Windows 內含） |
| **gcloud CLI** | <https://cloud.google.com/sdk/docs/install> → 裝完 `gcloud --version` 確認 |
| **GCP 專案** | 到 <https://console.cloud.google.com> 建一個（免費層不需要綁卡就能用 Cloud Run 的免費額度；但啟用 Cloud Build 會要求綁定帳單帳戶，仍在免費額度內） |
| **Docker Desktop** | 只有想在本機建 image 或跑容器測試時才需要；部署走 Cloud Build，不需要本機 Docker |

跑部署前先做這三件事：

```bash
gcloud auth login                        # 開瀏覽器登入 Google 帳號
gcloud config set project 你的專案ID      # 例如 resume-pipeline-470123
gcloud config list                       # 確認 account 與 project 都對了
```

> 專案 ID 不是專案名稱。在 Console 首頁的專案選單裡可以看到，長得像 `my-project-470123`。

部署腳本會自動啟用這四個 API，不用手動開：
`run.googleapis.com`、`cloudbuild.googleapis.com`、`secretmanager.googleapis.com`、
`artifactregistry.googleapis.com`。

### 憑證的方向：全部只填進 `.env`

這件事最容易搞混，先講清楚：

- **所有 token 只寫在本機的 `.env`**，部署腳本會讀它並存進 GCP Secret Manager。
- **你不需要把任何 token 填回 Slack、Notion 或 GCP 的介面。** 在 Slack 設定頁你只填
  「我的伺服器在哪」（兩個 Request URL）；token 是 Slack 發給你的，方向相反。
  Notion 也只需要知道「這個 integration 可以讀這一頁」。
- 唯一的例外是 **GitHub Actions 的兩個 secret**（`SERVICE_URL`、`TRIGGER_TOKEN`），
  那要在 GitHub 網頁上填，因為 Actions 跑在 GitHub 而不是 Cloud Run。

### 需要你手動準備的六個值

複製 `.env.example` 成 `.env`，填這六個：

| 變數 | 從哪裡拿 | 長相 |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | <https://platform.claude.com> → API keys | `sk-ant-…` |
| `NOTION_TOKEN` | Notion internal integration（下面第 1 步） | `ntn_…` |
| `SLACK_BOT_TOKEN` | Slack app → OAuth & Permissions → Bot User OAuth Token | `xoxb-…` |
| `SLACK_SIGNING_SECRET` | Slack app → Basic Information → App Credentials → Signing Secret | 32 字元 hex |
| `SLACK_DM_CHANNEL` | 你自己的 Slack member ID（頭像 → View full profile → ⋯ → Copy member ID） | `U…` |
| `GITHUB_TOKEN` | GitHub fine-grained PAT，對 Resume_pipeline 要 **Contents: Read and write** | `github_pat_…` |

**這兩個不用填，腳本會產生並寫回你的 `.env`**（因為 `TRIGGER_TOKEN` 之後要複製到
GitHub Actions，Secret Manager 不會再顯示第二次）：

| 變數 | 用途 |
| --- | --- |
| `APPROVAL_HMAC_SECRET` | 簽核准連結，讓猜到 run ID 的人不能發布 |
| `TRIGGER_TOKEN` | 驗證 GitHub Actions 的定時觸發請求 |

**這些完全不用管**，都有預設值或由腳本自動帶入：
`PUBLIC_BASE_URL`（部署後自動填入 Cloud Run 網址）、`STORAGE_BACKEND`（腳本設為 `github`）、
七個 `NOTION_DB_*`、`NOTION_MASTER_PAGE_ID`、`GITHUB_REPO`、`GIT_BRANCH`、`RENDERERS`、
`APPROVAL_TIMEOUT_HOURS`、`LLM_MODEL` 等。**完整 27 個變數的清單見 `.env.example`。**

### 1. Notion integration（必做，否則什麼都讀不到）

1. 到 <https://www.notion.so/my-integrations> 建 **Internal Integration**
   （不要選 Public — OAuth token 會過期需要 refresh），複製 `ntn_` 開頭的 token
2. 開 [Resume Master](https://app.notion.com/p/3b525b0d3537813ebc87e48ed8f823bc) 頁面
   → 右上 `⋯` → **Connections** → 加入該 integration

> 權限會往下繼承給七個 database。**不做第 2 步的話 REST API 一律回 404**，即使 token 完全正確。
> （MCP 有權限但 REST 沒有 — 是兩套認證。）

驗證這一步：`python scripts/check_notion.py` — 會分別檢查 token 有效性、頁面連線、
七個 database、以及有幾筆 row 真的會進履歷。

### 2. Slack app

分兩段做，因為 **Interactivity 按 Save 時 Slack 會即時驗證那個 URL** —— 服務還沒部署會失敗。

**先做（拿 token 用）：**

| 位置 | 設定 |
| --- | --- |
| Create New App | From scratch，名稱 `Resume Pipeline`，選你的 workspace |
| OAuth & Permissions → Bot Token Scopes | `chat:write`、`commands`、`im:write` |
| Install App | Install to Workspace → Allow → 複製 **Bot User OAuth Token**（`xoxb-`） |
| Basic Information → App Credentials | 複製 **Signing Secret** |

把這兩個值填進 `.env`。

**部署完成後再做**（第 3 步會印出網址，用它取代 `<URL>`）：

| 位置 | 設定 |
| --- | --- |
| Slash Commands → Create New Command | Command `/resume`，Request URL `<URL>/slack/commands` |
| Interactivity & Shortcuts | 打開開關，Request URL `<URL>/slack/interactions` |

三個子命令（`update` / `status` / `latest`）共用同一個 URL，不用分別註冊。
只呼叫 `chat.postMessage`，所以不需要 `files:write` — PDF 掛在 Notion 與下載連結。

### 3. Google Cloud Run

確認前置條件都做完（`gcloud auth login` + `gcloud config set project`），然後在 **Git Bash** 裡：

```bash
# 一次性：啟用 API、建立 secret（從 .env 讀值；缺的兩個會產生並寫回 .env）
bash scripts/deploy_cloudrun.sh --secrets

# 之後每次改程式重新部署
bash scripts/deploy_cloudrun.sh
```

第一次 build 約 10 分鐘（要下載 Chromium 與 Tectonic）。腳本結束會印出網址，
以及要填進 Slack 與 GitHub 的確切值 —— 包含產生出來的 `TRIGGER_TOKEN`。

可用環境變數覆寫預設（都有合理預設值，通常不用動）：

```bash
REGION=asia-east1 MEMORY=1Gi bash scripts/deploy_cloudrun.sh
```

**為什麼是 Cloud Run 而不是 Render 免費方案**：Render Free 閒置 15 分鐘後 spin down，
週一的定時觸發會撞上冷啟動；而且 web + cron 兩個 service 會超過 750 免費小時。
Cloud Run scale-to-zero 沒有這個問題，排程則移到 GitHub Actions。

**設定要點**（腳本已處理，列出來供你理解）：

| 設定 | 值 | 原因 |
| --- | --- | --- |
| `--memory` | 1Gi | 實測峰值 54MB + 488MB（Tectonic 是大戶）。512Mi 也夠，留餘裕 |
| `--timeout` | 900s | 渲染六個檔案要數十秒，預設 300s 會被切斷 |
| `--max-instances` | 1 | 兩個 instance 會搶著寫同一份 repo 狀態 |
| `STORAGE_BACKEND` | `github` | **必須** — Cloud Run 磁碟是瞬態的，連續請求可能落在不同 instance |
| secrets | Secret Manager | Cloud Run 的環境變數對有 viewer 權限的人可見，而這些 token 能推 repo、能發 Slack |

### 4. GitHub Actions（定時觸發）

Cloud Run 沒有內建 cron，所以排程用 Actions（完全免費）。

到 repo 的 **Settings → Secrets and variables → Actions** 加兩個 secret：

| Secret | 值 |
| --- | --- |
| `SERVICE_URL` | 部署腳本印出的 Cloud Run 網址 |
| `TRIGGER_TOKEN` | 與 Secret Manager 裡同名的值一致 |

然後 **Actions → Scheduled resume run → Run workflow** 手動跑一次驗證。
之後每週一台北時間 03:00 會自動觸發。

### 5. 驗證

```
curl https://<cloud-run-url>/healthz
```

`missing_credentials` 應該是空的、`publish_ready` 應該是 `true`、
`tools` 三項（`pdftoppm` / `tectonic` / `git`）應該都是 `true`。

> 想改用 Render 或自架，`docs/render.yaml` 是保留的 blueprint；把 `STORAGE_BACKEND`
> 設回 `local` 即可 —— 那裡有持久磁碟，`state/` 就是 committed 目錄。
> 記憶體實測峰值 54MB + 488MB，512MB 方案就夠。

---

## 本機開發

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -e ".[dev]"
.\.venv\Scripts\playwright install chromium
copy .env.example .env      # 填入你的 token

# 用 fixture 產出六個檔案，完全不需要任何憑證
.\.venv\Scripts\python scripts\local_run.py --render-only --png

# 從真的 Notion 讀
.\.venv\Scripts\python scripts\local_run.py

.\.venv\Scripts\python -m uvicorn web.app:app --reload
.\.venv\Scripts\python -m pytest
```

本機沒有 `tectonic` 或 `pdftoppm` 也能跑 — 那兩個 renderer 會被跳過並印警告（Docker 映像檔內含）。
Windows 上沒有 poppler，`pages.py` 會退回用 Playwright 截圖，但那是**一張長圖、沒有分頁**，
看不出換頁問題，只適合本機微調版面。

---

## 排版

三個 renderer 共用一組設計 token。改版面時**三個地方要一起改**，否則會漂移：

| 定義處 | 用途 |
| --- | --- |
| `templates/styles/print.css` 的 `:root` | 主要來源（HTML → PDF） |
| `pipeline/render/latex.py` 的 `GEOMETRY` / `SIZES` / `SPACING` | LaTeX 對應值 |
| `pipeline/render/docx.py` 頂部常數 | docx 對應值 |

`pipeline/render/labels.py` 放段落名稱與日期格式，所以三個 renderer 不會對「這一段叫什麼」有歧見。

| Renderer | 產物 | 備註 |
| --- | --- | --- |
| `html.py` | `resume.pdf` | 主要輸出，Playwright + Chromium |
| `latex.py` | `resume.latex.pdf` | **Tectonic 而非 TeX Live** — 後者會讓 image 到 2.5GB、build 慢 15 分鐘 |
| `docx.py` | `resume.docx` | 犧牲版面精度換可編輯性（部分投遞系統要 .docx、ATS 也更好解析） |

中文不是機器翻譯直出：`ZH Override` 欄位與頁面內文的 `## 中文` 區段永遠優先，
只有沒寫的部分才送 LLM 產草稿。產品名（Python、AWS、LangGraph）不翻譯。

---

## 為什麼用 git 當資料庫

這個系統一週寫一次、只有一個使用者，而免費的託管資料庫大多會被回收
（Render 免費 Postgres 30 天後刪除）。所以狀態直接放在 repo：

- 零成本、天然稽核歷史
- **diff 基準（`state/approved.json`）與它產生的檔案在同一個 commit** — 不會出現不一致
- 不需要額外服務要維護

實作有兩個 backend，都在 `pipeline/storage.py` 後面，由 `STORAGE_BACKEND` 切換：

| Backend | 怎麼運作 | 用在哪 |
| --- | --- | --- |
| `local` | 直接讀寫檔案系統，`state/` 是 committed 目錄 | 本機開發、測試、有持久磁碟的平台 |
| `github` | 透過 GitHub Contents API 讀寫，每次寫入就是一個 commit | **Cloud Run 必須用這個** |

Cloud Run 的磁碟是瞬態的，而且連續兩個請求可能落在不同 instance —— 所以「A 請求建立的 run，
B 請求要讀得到」。`github` backend 解決這件事，代價是每次操作多一次 API 往返
（一週一次的系統，這不痛）。

`tests/test_storage.py` 對兩個 backend 跑同一組 contract 測試，避免它們漂移；
其中一個測試直接驗證「沒有共用磁碟時，A 存的 run B 讀得到」。

---

## 疑難排解

| 症狀 | 原因 |
| --- | --- |
| Notion 讀取回 404 | integration 沒加到 Resume Master 頁面（見上面第 1 步第 2 點） |
| 履歷是空的 / run 失敗說沒內容 | Notion 沒有任何 `Status = Approved` **且** 勾了 `Include in Resume` 的 row |
| 沒收到 Slack 通知 | 可能真的沒有差異（設計如此）。`/resume status` 可以確認 |
| `/resume/en.pdf` 回 404 | 還沒核准過任何 run |
| 少了 `.latex.pdf` | 該環境沒有 Tectonic；`/healthz` 會列出缺哪些工具 |
| diff 頁沒有版面比對圖 | 該環境沒有 `pdftoppm` |
| 核准連結說 invalid signature | `APPROVAL_HMAC_SECRET` 換過了，舊連結會失效 |

**部署階段的問題：**

| 症狀 | 原因 |
| --- | --- |
| `gcloud: command not found` | CLI 沒裝或沒重開終端機。裝完要重開才會進 PATH |
| `no GCP project selected` | 跑 `gcloud config set project 你的專案ID`（是 ID 不是名稱） |
| `PERMISSION_DENIED` 啟用 API 時 | 該專案沒綁帳單帳戶。Cloud Build 需要綁定，但用量仍在免費額度內 |
| `.env not found` | `--secrets` 需要 `.env`。先 `cp .env.example .env` 並填那六個值 |
| 部署腳本說 `skip NOTION_TOKEN` | `.env` 裡那一行還是佔位符或空的 |
| Slack 存 Interactivity 說 URL 無效 | 服務還沒部署。先跑第 3 步拿到網址，再回來設 Slack |
| Actions 的 `Scheduled resume run` 失敗 | `SERVICE_URL` 或 `TRIGGER_TOKEN` 沒設，或值與 Secret Manager 不一致 |
| `bash: scripts/...: No such file` | 在 PowerShell 跑了。要在 **Git Bash** 裡執行 |

`/healthz` 會回報 renderer、外部工具、以及缺哪些憑證 —— 部署後第一個該看的地方。

---

## 架構

```
pipeline/
  config.py       所有環境變數（repo_root 是欄位，測試可指向 temp 目錄）
  models.py       Resume / Experience / Project / ...（stable_key 是 diff 的比對依據）
  state.py        run 狀態、diff 基準、source 索引 — 全部落在 state/
  ingest.py       ① 掃 sources/，用 SHA256 做增量
  extract.py      ② LLM 抽取候選項目（prompt 刻意要求保守）
  reconcile.py    ③ 寫進 Notion 待審 —「絕不覆寫已核准」在這裡
  notion_client.py④ 讀七個 DB → resume.json
  translate.py    ⑤ 中文（人寫的覆寫優先）
  render/         ⑥ 三個引擎 + 共用 labels + 頁面轉圖
  diff.py         ⑦ 欄位級 diff
  notify_slack.py ⑧ Block Kit 通知
  publish.py      ⑨ 唯一會動到外部世界的地方
  runner.py       串起 ①–⑧，結束在「等你核准」
web/
  app.py          FastAPI + /healthz
  routes_runs.py  觸發、diff 頁、核准（HMAC 簽章）
  routes_resume.py固定下載連結
  slack.py        slash command + 按鈕（驗簽 + 3 秒 ack）
  diff_page.py    server-rendered 審核頁，無 JavaScript
```
