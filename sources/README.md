# sources/ — 投料口

把任何「講述你做過什麼」的原始文件丟進這個資料夾，pipeline 會讀它、抽出候選經歷／專案／技能，
寫進 Notion 的待審清單等你確認。

## 支援格式

| 格式 | 怎麼處理 |
| --- | --- |
| `.pdf` | 直接送 LLM（原生 PDF 理解，含掃描件的版面） |
| `.docx` | 先用 python-docx 轉純文字 |
| `.pptx` | 先用 python-pptx 抽投影片文字 |
| `.html` / `.htm` | 用標準庫抽文字：丟掉 `<script>` / `<style>`，表格用 `\|` 串成一列 |
| `.md` / `.txt` | 直接讀 |

> `.html` 適合「網頁另存」「Word 另存為 HTML」「Confluence／Notion 匯出」這類檔案。
> JS 與 CSS 不會進到 LLM —— 那些內容只會讓模型從變數名稱裡編出不存在的專案。

## 怎麼用

```powershell
# 1. 把文件複製進來（PRD、專案報告、結案簡報、舊履歷、自傳、證書…）
# 2. 推上去
.\scripts\push_sources.ps1
```

推上去不會馬上觸發 pipeline。下一次定時執行（台北時間每週一 03:00）會撿到；
想立刻跑就在 Slack 打 `/resume update`。

## 重要行為

- **增量處理**：檔案的 SHA256 記在 `state/sources.json`，同一份檔案不會被重複抽取。
  改了內容再推，會被視為新版本重新處理。
- **不會覆寫你的字**：抽出來的東西一律是 `Status = Pending Review` 且 `Include in Resume`
  不打勾。若命中 Notion 上已經 `Approved` 的項目，pipeline 只會在那一筆下面留 comment 建議，
  不會動你已核准的內容。
- **檔案不會被刪**：這裡是歷史檔案庫，pipeline 只讀不寫。

## 命名建議

檔名會寫進 Notion 的 `Source` 欄位供日後追溯，所以帶上時間與主題會比較好認：

```
2026-08_cathay_bu-agent_prd.md
2026-06_aia_liff-chatbot_結案報告.pptx
2025-10_rocling_camera-ready.pdf
```
