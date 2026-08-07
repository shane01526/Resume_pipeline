"""The review page: what changed, what it looks like, and the two buttons.

Server-rendered rather than a client app. This page exists to be opened once from a Slack
message, on a phone as often as a laptop, and then acted on — a build step and a JS bundle
would add failure modes without adding anything you need.

Three tabs, implemented as radio inputs plus CSS sibling selectors so tab switching needs
no JavaScript at all: if the page loads, the tabs work.
"""

from __future__ import annotations

import html
import json

from pipeline.config import Settings
from pipeline.diff import ChangeKind, ItemChange, ResumeDiff, read_diff
from pipeline.state import Run, RunStatus, RunStore

SECTION_LABELS = {
    "profile": "基本資料",
    "education": "學歷",
    "experiences": "工作經歷",
    "projects": "專案成果",
    "publications": "論文與演講",
    "skills": "專業技能",
}

KIND_LABELS = {
    ChangeKind.ADDED: ("新增", "added"),
    ChangeKind.MODIFIED: ("修改", "modified"),
    ChangeKind.REMOVED: ("移除", "removed"),
}

ARTIFACT_LABELS = {
    "en/resume.pdf": "英文 PDF",
    "zh/resume.pdf": "中文 PDF",
    "en/resume.latex.pdf": "英文 PDF (LaTeX)",
    "zh/resume.latex.pdf": "中文 PDF (LaTeX)",
    "en/resume.docx": "英文 Word",
    "zh/resume.docx": "中文 Word",
}


def render_diff_page(run: Run, store: RunStore, settings: Settings) -> str:
    from web.routes_runs import approval_links

    diff = read_diff(store.run_dir(run.id) / "diff.json")
    links = approval_links(run, settings)
    decidable = run.status is RunStatus.PENDING_APPROVAL

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>履歷更新審核 — {html.escape(run.id)}</title>
<style>{_STYLES}</style>
</head>
<body>
<header class="top">
  <div class="run">
    <h1>履歷更新審核</h1>
    <p class="meta">
      <code>{html.escape(run.id)}</code>
      <span class="badge status-{run.status.value.lower().replace(" ", "-")}">{html.escape(run.status.value)}</span>
      <span>{html.escape(run.trigger.value)}</span>
      <span>{run.created_at:%Y-%m-%d %H:%M} UTC</span>
    </p>
  </div>
  {_counts_block(run)}
</header>

{_error_block(run)}

<div class="tabs">
  <input type="radio" name="tab" id="tab-content" checked>
  <input type="radio" name="tab" id="tab-pages">
  <input type="radio" name="tab" id="tab-json">
  <nav class="tab-bar">
    <label for="tab-content">內容 diff</label>
    <label for="tab-pages">版面 diff</label>
    <label for="tab-json">原始 JSON</label>
  </nav>

  <section class="panel panel-content">{_content_tab(diff)}</section>
  <section class="panel panel-pages">{_pages_tab(run, store)}</section>
  <section class="panel panel-json">{_json_tab(run, store)}</section>
</div>

{_decision_block(run, links, decidable)}
</body>
</html>
"""


# --- header -----------------------------------------------------------------


def _counts_block(run: Run) -> str:
    counts = run.counts
    return f"""<div class="counts">
    <span class="count added">+{counts.added} 新增</span>
    <span class="count modified">~{counts.modified} 修改</span>
    <span class="count removed">-{counts.removed} 移除</span>
  </div>"""


def _error_block(run: Run) -> str:
    if not run.error:
        return ""
    return f'<div class="error"><strong>執行失敗</strong><pre>{html.escape(run.error)}</pre></div>'


# --- tab 1: content ---------------------------------------------------------


def _content_tab(diff: ResumeDiff | None) -> str:
    if diff is None:
        return '<p class="empty">找不到 diff 資料，這次執行可能尚未完成。</p>'
    if not diff.changes:
        return '<p class="empty">與上次核准的版本沒有差異。</p>'

    banner = (
        '<p class="note">這是第一次執行，沒有比較基準，因此所有項目都顯示為新增。</p>'
        if diff.is_first_run
        else ""
    )

    sections = []
    for section, changes in diff.by_section().items():
        rows = "".join(_change_block(change) for change in changes)
        sections.append(
            f'<h2 class="section">{html.escape(SECTION_LABELS.get(section, section))}</h2>{rows}'
        )
    return banner + "".join(sections)


def _change_block(change: ItemChange) -> str:
    label, css = KIND_LABELS[change.kind]
    notion = (
        f' <a class="notion" href="https://notion.so/{change.notion_page_id.replace("-", "")}" '
        f'target="_blank" rel="noopener">在 Notion 開啟 ↗</a>'
        if change.notion_page_id
        else ""
    )

    body = ""
    if change.fields:
        rows = "".join(
            f"<tr><th>{html.escape(field.field)}</th>"
            f'<td class="{_cell_class("before", field.before)}">{_cell(field.before)}</td>'
            f'<td class="{_cell_class("after", field.after)}">{_cell(field.after)}</td></tr>'
            for field in change.fields
        )
        body += (
            '<table class="fields"><thead><tr><th>欄位</th><th>修改前</th><th>修改後</th>'
            f"</tr></thead><tbody>{rows}</tbody></table>"
        )

    if change.bullet_diff:
        lines = "".join(
            f'<div class="dl {_bullet_class(line)}">{html.escape(line)}</div>'
            for line in change.bullet_diff
        )
        body += f'<div class="bullets"><div class="bullets-title">條列內容</div>{lines}</div>'

    return f"""<article class="change {css}">
  <div class="change-head">
    <span class="tag {css}">{label}</span>
    <strong>{html.escape(change.label)}</strong>{notion}
  </div>
  {body}
</article>"""


def _cell(value: str | None) -> str:
    if value is None:
        return '<span class="none">—</span>'
    return html.escape(value)


def _cell_class(side: str, value: str | None) -> str:
    """Colour a cell only when it holds something.

    An empty "before" on an added entry is not a deletion, so tinting it red misreads as
    "this was removed" and makes an addition look like a replacement.
    """
    return "empty" if value is None else side


def _bullet_class(line: str) -> str:
    if line.startswith("+"):
        return "add"
    if line.startswith("-"):
        return "del"
    return ""


# --- tab 2: page images -----------------------------------------------------


def _pages_tab(run: Run, store: RunStore) -> str:
    pages_root = store.pages_dir(run.id)
    if not pages_root.is_dir() or not any(pages_root.rglob("*.png")):
        return (
            '<p class="empty">沒有頁面影像。<br>'
            "本機環境若沒有安裝 <code>poppler-utils</code>（提供 <code>pdftoppm</code>），"
            "就無法產生版面比對圖；Docker 映像檔已內含。</p>"
        )

    blocks = []
    for lang_dir in sorted(pages_root.iterdir()):
        if not lang_dir.is_dir():
            continue
        for stem in sorted({p.name.rsplit("-", 1)[0] for p in lang_dir.glob("*.png")}):
            pages = sorted(lang_dir.glob(f"{stem}-*.png"))
            label = ARTIFACT_LABELS.get(f"{lang_dir.name}/{stem}.pdf", f"{lang_dir.name}/{stem}")
            images = "".join(
                f'<figure><img src="/runs/{run.id}/pages/{lang_dir.name}/{page.name}" '
                f'alt="{html.escape(label)} 第 {index} 頁" loading="lazy">'
                f"<figcaption>第 {index} 頁</figcaption></figure>"
                for index, page in enumerate(pages, 1)
            )
            blocks.append(
                f'<div class="artifact"><h2 class="section">{html.escape(label)}'
                f' <span class="pagecount">{len(pages)} 頁</span></h2>'
                f'<div class="pages">{images}</div></div>'
            )

    note = (
        '<p class="note">目前顯示的是<strong>本次產出</strong>的頁面。'
        "上一版的並排比對會在下一次核准後可用（需要已發布的基準頁面影像）。</p>"
    )
    return note + "".join(blocks)


# --- tab 3: raw JSON --------------------------------------------------------


def _json_tab(run: Run, store: RunStore) -> str:
    path = store.artifacts_dir(run.id) / "resume.json"
    if not path.is_file():
        return '<p class="empty">這次執行沒有產生 resume.json。</p>'
    try:
        payload = json.dumps(
            json.loads(path.read_text(encoding="utf-8")), ensure_ascii=False, indent=2
        )
    except ValueError as exc:
        return f'<p class="empty">resume.json 無法解析：{html.escape(str(exc))}</p>'
    return f"<pre class='json'>{html.escape(payload)}</pre>"


# --- decision ---------------------------------------------------------------


def _decision_block(run: Run, links: dict[str, str], decidable: bool) -> str:
    if not decidable:
        extra = f"<br>commit <code>{html.escape(run.commit_sha)}</code>" if run.commit_sha else ""
        return (
            f'<footer class="decide done">這次執行已是 <strong>{html.escape(run.status.value)}</strong>'
            f"，無需再處理。{extra}</footer>"
        )
    # GET links, so this works from any device and survives Slack being unavailable. The
    # token is HMAC-signed over run ID + action, so a guessed run ID cannot publish.
    return f"""<footer class="decide">
  <a class="btn approve" href="{html.escape(links["approve"])}">✅ 核准並發布</a>
  <a class="btn reject" href="{html.escape(links["reject"])}">🚫 駁回</a>
  <p class="hint">核准後會 commit 到 repo、上傳 Notion，並更新固定下載連結。</p>
</footer>"""


_STYLES = """
:root{--ink:#111;--muted:#666;--line:#e3e3e3;--bg:#fafafa;
--add:#0a7f3f;--add-bg:#eaf7ef;--del:#b3261e;--del-bg:#fdeceb;--mod:#8a6100;--mod-bg:#fdf6e3;}
*{box-sizing:border-box}
body{margin:0;padding:0 16px 120px;font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",
"Noto Sans TC",sans-serif;color:var(--ink);background:#fff;max-width:1080px;margin-inline:auto}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.88em;
background:var(--bg);padding:1px 5px;border-radius:3px}
.top{display:flex;flex-wrap:wrap;gap:16px;justify-content:space-between;align-items:flex-start;
padding:24px 0 16px;border-bottom:2px solid var(--ink)}
h1{margin:0 0 6px;font-size:20px}
.meta{margin:0;display:flex;flex-wrap:wrap;gap:10px;align-items:center;color:var(--muted);font-size:13px}
.badge{padding:2px 8px;border-radius:10px;font-size:12px;font-weight:600;background:var(--bg)}
.status-pending-approval{background:var(--mod-bg);color:var(--mod)}
.status-approved{background:var(--add-bg);color:var(--add)}
.status-rejected,.status-failed{background:var(--del-bg);color:var(--del)}
.counts{display:flex;gap:14px;font-size:14px;font-weight:600}
.count.added{color:var(--add)}.count.modified{color:var(--mod)}.count.removed{color:var(--del)}
.error{margin:16px 0;padding:12px 14px;background:var(--del-bg);border-left:3px solid var(--del);
border-radius:0 4px 4px 0}
.error pre{margin:6px 0 0;white-space:pre-wrap;font-size:13px}
/* Tabs via radio + sibling selectors: no JS, so if the page loads the tabs work. */
.tabs input{position:absolute;opacity:0;pointer-events:none}
.tab-bar{display:flex;gap:4px;margin:20px 0 0;border-bottom:1px solid var(--line)}
.tab-bar label{padding:9px 15px;cursor:pointer;font-size:14px;color:var(--muted);
border-bottom:2px solid transparent;margin-bottom:-1px}
.tab-bar label:hover{color:var(--ink)}
.panel{display:none;padding-top:20px}
#tab-content:checked~.tab-bar label[for=tab-content],
#tab-pages:checked~.tab-bar label[for=tab-pages],
#tab-json:checked~.tab-bar label[for=tab-json]{color:var(--ink);font-weight:600;
border-bottom-color:var(--ink)}
#tab-content:checked~.panel-content,
#tab-pages:checked~.panel-pages,
#tab-json:checked~.panel-json{display:block}
.section{margin:22px 0 10px;font-size:13px;font-weight:700;letter-spacing:.08em;color:var(--muted)}
.pagecount{font-weight:400;letter-spacing:0}
.change{margin:0 0 12px;border:1px solid var(--line);border-left-width:3px;border-radius:0 5px 5px 0;
padding:11px 14px}
.change.added{border-left-color:var(--add)}
.change.modified{border-left-color:var(--mod)}
.change.removed{border-left-color:var(--del)}
.change-head{display:flex;flex-wrap:wrap;gap:9px;align-items:center}
.tag{font-size:11px;font-weight:700;padding:2px 7px;border-radius:3px}
.tag.added{background:var(--add-bg);color:var(--add)}
.tag.modified{background:var(--mod-bg);color:var(--mod)}
.tag.removed{background:var(--del-bg);color:var(--del)}
.notion{font-size:12px;color:var(--muted);text-decoration:none}
.notion:hover{color:var(--ink);text-decoration:underline}
table.fields{width:100%;border-collapse:collapse;margin-top:9px;font-size:13px;table-layout:fixed}
table.fields th,table.fields td{text-align:left;padding:5px 8px;border-top:1px solid var(--line);
vertical-align:top;word-break:break-word}
table.fields thead th{font-size:11px;color:var(--muted);border-top:0}
table.fields tbody th{width:22%;font-weight:600;color:var(--muted)}
td.before{color:var(--del);background:var(--del-bg)}
td.after{color:var(--add);background:var(--add-bg)}
td.empty{background:transparent}
.none{color:var(--muted)}
.bullets{margin-top:9px;font-size:13px}
.bullets-title{font-size:11px;color:var(--muted);margin-bottom:4px}
.dl{padding:2px 8px;font-family:ui-monospace,Menlo,monospace;white-space:pre-wrap;
border-radius:2px;word-break:break-word}
.dl.add{background:var(--add-bg);color:var(--add)}
.dl.del{background:var(--del-bg);color:var(--del)}
.pages{display:flex;flex-wrap:wrap;gap:14px}
figure{margin:0}
figure img{max-width:min(420px,100%);border:1px solid var(--line);box-shadow:0 1px 5px rgba(0,0,0,.09)}
figcaption{font-size:11px;color:var(--muted);margin-top:4px}
pre.json{background:var(--bg);padding:14px;border-radius:5px;overflow-x:auto;font-size:12px;
line-height:1.5}
.empty,.note{color:var(--muted);font-size:14px;padding:10px 0}
.note{background:var(--bg);padding:10px 13px;border-radius:4px}
.decide{position:fixed;bottom:0;left:0;right:0;background:#fff;border-top:1px solid var(--line);
padding:13px 16px;display:flex;flex-wrap:wrap;gap:11px;align-items:center;justify-content:center;
box-shadow:0 -2px 10px rgba(0,0,0,.06)}
.btn{display:inline-block;padding:10px 22px;border-radius:6px;font-weight:600;font-size:15px;
text-decoration:none}
.btn.approve{background:var(--add);color:#fff}
.btn.reject{background:#fff;color:var(--del);border:1px solid var(--del)}
.hint{margin:0;flex-basis:100%;text-align:center;font-size:12px;color:var(--muted)}
.decide.done{color:var(--muted);font-size:14px;display:block;text-align:center}
@media(max-width:640px){
  .top{flex-direction:column}
  table.fields{table-layout:auto}
  table.fields tbody th{width:auto}
  .btn{flex:1;text-align:center}
}
"""
