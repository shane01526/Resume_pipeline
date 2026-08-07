"""Stage 8: the Slack notification.

One rule governs everything here: **a notification you can ignore is worse than none.**
The runner already skips this stage when nothing changed; what's left is to make the
message answer "what changed and do I care" without opening anything.

So the message leads with counts, then names the actual entries that changed — not a
generic "your resume was updated". The buttons approve or reject in place; the link is
there for when you want the full before/after.
"""

from __future__ import annotations

import logging

from pipeline.config import Settings
from pipeline.diff import ChangeKind, ResumeDiff
from pipeline.state import Run

log = logging.getLogger(__name__)

# Slack renders long messages with a "show more" fold, which hides the buttons. Naming a
# handful of entries is enough to decide; the rest are on the diff page.
MAX_LISTED_CHANGES = 6

KIND_MARKERS = {
    ChangeKind.ADDED: "＋",
    ChangeKind.MODIFIED: "～",
    ChangeKind.REMOVED: "－",
}

SECTION_LABELS = {
    "profile": "基本資料",
    "education": "學歷",
    "experiences": "工作經歷",
    "projects": "專案成果",
    "publications": "論文",
    "skills": "技能",
}


async def notify_pending(run: Run, diff: ResumeDiff, settings: Settings) -> None:
    """Announce a run that is waiting for approval."""
    from web.slack import post_message

    url = run.preview_url(settings.public_base_url)
    summary = diff.counts.summary()

    # Fallback text: what shows in a push notification and in the sidebar, where blocks
    # are not rendered. It has to stand alone.
    fallback = f"履歷有更新待審核（{summary}）— {url}"

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "📄 履歷有更新待審核", "emoji": True},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*變更*\n{summary}"},
                {"type": "mrkdwn", "text": f"*觸發*\n{run.trigger.value}"},
            ],
        },
    ]

    if listing := _change_listing(diff):
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": listing}})

    if diff.is_first_run:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "ℹ️ 這是第一次執行，沒有比較基準，所有項目都顯示為新增。",
                    }
                ],
            }
        )

    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "approve_run",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "✅ 核准並發布", "emoji": True},
                    "value": run.id,
                    # Slack shows this before firing the action; the guard against a
                    # mis-tap publishing to a public repo.
                    "confirm": {
                        "title": {"type": "plain_text", "text": "確認發布？"},
                        "text": {
                            "type": "mrkdwn",
                            "text": f"將 commit 到 repo、上傳 Notion，並更新下載連結。\n\n{summary}",
                        },
                        "confirm": {"type": "plain_text", "text": "發布"},
                        "deny": {"type": "plain_text", "text": "取消"},
                    },
                },
                {
                    "type": "button",
                    "action_id": "reject_run",
                    "text": {"type": "plain_text", "text": "🚫 駁回", "emoji": True},
                    "value": run.id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🔍 檢視 diff", "emoji": True},
                    "url": url,
                },
            ],
        }
    )

    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"`{run.id}` · {settings.approval_timeout_hours:.0f} 小時內未處理會自動過期"
                    ),
                }
            ],
        }
    )

    await post_message(settings, fallback, blocks)
    log.info("notified slack about run %s", run.id)


def _change_listing(diff: ResumeDiff) -> str:
    """The changed entries, grouped by section, truncated with an honest remainder count."""
    lines: list[str] = []
    shown = 0

    for section, changes in diff.by_section().items():
        section_lines = []
        for change in changes:
            if shown >= MAX_LISTED_CHANGES:
                break
            marker = KIND_MARKERS[change.kind]
            section_lines.append(f"{marker} {change.label}")
            shown += 1
        if section_lines:
            label = SECTION_LABELS.get(section, section)
            lines.append(f"*{label}*\n" + "\n".join(section_lines))
        if shown >= MAX_LISTED_CHANGES:
            break

    if not lines:
        return ""

    # Never imply the list is complete when it isn't — a silently truncated list reads as
    # "that's all that changed".
    if (remaining := diff.counts.total - shown) > 0:
        lines.append(f"_…另有 {remaining} 項變更，請開 diff 頁檢視_")

    return "\n\n".join(lines)


async def notify_published(run: Run, settings: Settings) -> None:
    """Confirm a successful publish, with the fresh download links."""
    from web.slack import post_message

    base = settings.public_base_url
    commit = f"`{run.commit_sha[:8]}`" if run.commit_sha else "(no commit)"
    links = " · ".join(
        f"<{base}/resume/{lang}.pdf|{label} PDF>"
        for lang, label in (("en", "英文"), ("zh", "中文"))
    )
    await post_message(
        settings,
        f"✅ 履歷已發布 {commit}\n{links}\n所有格式：{base}/resume/en.pdf ⋯",
    )


async def notify_expired(run: Run, settings: Settings) -> None:
    """Tell you a run timed out, rather than letting it vanish silently."""
    from web.slack import post_message

    await post_message(
        settings,
        f"⏰ 履歷更新 `{run.id}` 已超過 {settings.approval_timeout_hours:.0f} 小時未處理，"
        f"已自動標記為過期並丟棄產出。\n下次定時執行會重新產生，或用 `/resume update` 立即重跑。",
    )
