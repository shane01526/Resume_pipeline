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
https://<render-url>/resume/en.pdf     https://<render-url>/resume/zh.pdf
https://<render-url>/resume/en.docx    https://<render-url>/resume/zh.docx
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

### 所有憑證只填在 Render 一個地方

先講清楚方向，因為這件事最容易搞混：

- **Notion / Slack / GitHub / Anthropic 的 token 全部只填進 Render 的 Environment。**
- **你不需要把任何 token 填回 Slack 或 Notion。** 在 Slack 設定頁你只填「我的伺服器在哪」
  （兩個 Request URL）；token 是 Slack 發給你的，方向相反。Notion 也只需要知道
  「這個 integration 可以讀這一頁」，不需要知道 Render 存在。

### 需要你手動準備的六個值

| 變數 | 從哪裡拿 |
| --- | --- |
| `ANTHROPIC_API_KEY` | <https://platform.claude.com> → API keys |
| `NOTION_TOKEN` | Notion internal integration（下面第 1 步），`ntn_` 開頭 |
| `SLACK_BOT_TOKEN` | Slack app → OAuth & Permissions → Bot User OAuth Token，`xoxb-` 開頭 |
| `SLACK_SIGNING_SECRET` | Slack app → Basic Information → App Credentials → Signing Secret |
| `SLACK_DM_CHANNEL` | 你自己的 Slack member ID（`U` 開頭；頭像 → View full profile → ⋯ → Copy member ID） |
| `GITHUB_TOKEN` | GitHub fine-grained PAT，對這個 repo 要 **Contents: Read and write**（publish 要 commit 回來） |

其餘變數都不用管：`APPROVAL_HMAC_SECRET` 與 `TRIGGER_TOKEN` 由 `render.yaml` 自動產生，
`PUBLIC_BASE_URL` 自動帶入 Render 網址，七個 `NOTION_DB_*` 與其他選項都有預設值
（完整清單見 `.env.example`）。

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

先部署 Render 拿到網址再做這步：**步驟 4 按 Save 時 Slack 會即時驗證那個 URL**。

| 位置 | 設定 |
| --- | --- |
| Create New App | From scratch，名稱 `Resume Pipeline` |
| OAuth & Permissions → Bot Token Scopes | `chat:write`、`commands`、`im:write` |
| Slash Commands → Create New Command | `/resume` → `https://<render-url>/slack/commands` |
| Interactivity & Shortcuts | 打開開關，Request URL `https://<render-url>/slack/interactions` |
| Install App | Install to Workspace → 複製 Bot User OAuth Token |

三個子命令（`update` / `status` / `latest`）共用同一個 URL，不用分別註冊。
只呼叫 `chat.postMessage`，所以不需要 `files:write` — PDF 掛在 Notion 與下載連結。

### 3. Render

Dashboard → New → **Blueprint** → 指向這個 repo（`render.yaml` 會建 web + cron 兩個 service）。

到 web service 的 **Environment** 填上表那六個值。存檔後會自動重新部署。

驗證：`curl https://<render-url>/healthz` — `missing_credentials` 應該是空的、
`publish_ready` 應該是 `true`。

> **Plan 必須是 Starter，不能用 Free。** Chromium + Tectonic 尖峰約 1GB RAM，Free 的 512MB 會 OOM；
> 而且 Free 會 spin down，週一的 cron 觸發會撞上冷啟動而逾時。

### 4. 本機開發

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

Render 免費 Postgres 30 天後會被刪，web service 磁碟是 ephemeral。
但這個系統一週寫一次、只有一個使用者，所以 `state/` 直接 commit 進 repo：

- 零成本、天然稽核歷史
- **diff 基準（`state/approved.json`）與它產生的檔案在同一個 commit** — 不會出現不一致
- cron 與 web 是不同容器，靠 `git pull` 讀到同一份狀態

真的需要換 Postgres 時，介面都在 `pipeline/state.py` 後面。

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

`/healthz` 會回報 renderer、外部工具、以及缺哪些憑證。

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
