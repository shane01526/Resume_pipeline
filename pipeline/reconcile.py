"""Stage 3: candidates → Notion review rows.

The invariant this module exists to enforce: **an approved row is never modified.**

When a candidate matches something you have already approved, the only action taken is a
Notion comment suggesting the addition. Your wording, your ordering, and your decisions
about what belongs on the resume survive untouched — the pipeline can propose, never edit.

New candidates become rows with `Status = Pending Review` and `Include in Resume`
unchecked, so nothing reaches a rendered resume in the same run that discovered it.

Matching is fuzzy on purpose: a PRD might call the employer "Cathay DDT" where Notion says
"Cathay Financial Holdings — DDT AI". Missing that match creates a duplicate row, which is
the annoying-but-safe failure; a false match would attach a suggestion to the wrong job,
so the threshold is deliberately conservative.
"""

from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from difflib import SequenceMatcher
from typing import Any

import httpx

from pipeline.config import Settings
from pipeline.extract import (
    CandidateExperience,
    CandidateProject,
    CandidatePublication,
    CandidateSkill,
    Extraction,
)
from pipeline.ingest import Source
from pipeline.models import SkillCategory, Status
from pipeline.notion_client import API_BASE, NotionError

log = logging.getLogger(__name__)

# Above this, two names are the same thing. Chosen conservatively: a duplicate row costs
# one rejection, a false match attaches a suggestion to the wrong job.
MATCH_THRESHOLD = 0.82

# An experience match also requires the start dates to be within this window, so two
# separate stints at the same employer stay distinct.
DATE_WINDOW_DAYS = 120


@dataclass(slots=True)
class ReconcileResult:
    created: list[str] = field(default_factory=list)
    commented: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{len(self.created)} new row(s), {len(self.commented)} suggestion(s), "
            f"{len(self.skipped)} skipped"
        )


async def reconcile_candidates(sources: list[Source], settings: Settings) -> ReconcileResult:
    """Extract from each source and file the results into Notion for review."""
    from pipeline.extract import extract_from_source

    result = ReconcileResult()
    token = settings.notion_token.get_secret_value()
    if not token:
        raise NotionError("NOTION_TOKEN is empty — cannot file candidates")

    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": settings.notion_version,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(base_url=API_BASE, timeout=45.0, headers=headers) as client:
        existing = await _load_existing(client, settings)

        for source in sources:
            extraction = await extract_from_source(source, settings)
            if extraction is None:
                result.skipped.append(source.name)
                continue
            await _file_extraction(client, extraction, source, existing, settings, result)

    log.info("reconcile: %s", result.summary())
    return result


# --- existing state ---------------------------------------------------------


@dataclass(slots=True)
class ExistingRow:
    page_id: str
    label: str
    status: str | None
    start: date | None = None
    category: str | None = None

    @property
    def is_approved(self) -> bool:
        return self.status == Status.APPROVED.value


async def _load_existing(
    client: httpx.AsyncClient, settings: Settings
) -> dict[str, list[ExistingRow]]:
    """Every row in the four content databases, regardless of status.

    All statuses, not just approved: a candidate matching a row you already rejected or
    left as a draft should not be filed again either.
    """
    databases = {
        "experiences": (settings.notion_db_experiences, "Role", "Organization"),
        "projects": (settings.notion_db_projects, "Name", None),
        "skills": (settings.notion_db_skills, "Name", None),
        "publications": (settings.notion_db_publications, "Title", None),
    }

    existing: dict[str, list[ExistingRow]] = {}
    for section, (database_id, title_prop, label_prop) in databases.items():
        rows = []
        cursor: str | None = None
        while True:
            payload: dict[str, Any] = {"page_size": 100}
            if cursor:
                payload["start_cursor"] = cursor
            response = await client.post(f"/databases/{database_id}/query", json=payload)
            response.raise_for_status()
            data = response.json()

            for row in data.get("results", []):
                props = row["properties"]
                # Experiences are identified by organization, not role: the org is what a
                # source document reliably names.
                label = _select_value(props, label_prop) or _title_value(props, title_prop) or ""
                rows.append(
                    ExistingRow(
                        page_id=row["id"],
                        label=label,
                        status=_select_value(props, "Status"),
                        start=_date_value(props, "Start"),
                        category=_select_value(props, "Category"),
                    )
                )

            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")

        existing[section] = rows
        log.debug("loaded %d existing %s row(s)", len(rows), section)

    return existing


# --- filing -----------------------------------------------------------------


async def _file_extraction(
    client: httpx.AsyncClient,
    extraction: Extraction,
    source: Source,
    existing: dict[str, list[ExistingRow]],
    settings: Settings,
    result: ReconcileResult,
) -> None:
    for candidate in extraction.experiences:
        match = _match_experience(candidate, existing["experiences"])
        if match:
            await _suggest(client, match, candidate.bullets, source, result)
        else:
            page_id = await _create_experience(client, candidate, source, extraction, settings)
            if page_id:
                result.created.append(f"experience: {candidate.organization}")
                existing["experiences"].append(
                    ExistingRow(
                        page_id=page_id,
                        label=candidate.organization,
                        status=Status.PENDING_REVIEW.value,
                        start=_parse_date(candidate.start),
                    )
                )

    for candidate in extraction.projects:
        match = _match_by_label(candidate.name, existing["projects"])
        if match:
            await _suggest(client, match, candidate.bullets, source, result)
        else:
            page_id = await _create_project(client, candidate, source, extraction, settings)
            if page_id:
                result.created.append(f"project: {candidate.name}")
                existing["projects"].append(
                    ExistingRow(
                        page_id=page_id, label=candidate.name, status=Status.PENDING_REVIEW.value
                    )
                )

    for candidate in extraction.skills:
        # Skills are matched within their category: "Python" as a language and as a tool
        # are different rows.
        pool = [r for r in existing["skills"] if r.category == candidate.category]
        if _match_by_label(candidate.name, pool):
            continue  # a skill needs no suggestion — it either exists or it doesn't
        page_id = await _create_skill(client, candidate, source, extraction, settings)
        if page_id:
            result.created.append(f"skill: {candidate.name}")
            existing["skills"].append(
                ExistingRow(
                    page_id=page_id,
                    label=candidate.name,
                    status=Status.PENDING_REVIEW.value,
                    category=candidate.category,
                )
            )

    for candidate in extraction.publications:
        if _match_by_label(candidate.title, existing["publications"]):
            continue
        page_id = await _create_publication(client, candidate, source, extraction, settings)
        if page_id:
            result.created.append(f"publication: {candidate.title}")
            existing["publications"].append(
                ExistingRow(
                    page_id=page_id, label=candidate.title, status=Status.PENDING_REVIEW.value
                )
            )


async def _suggest(
    client: httpx.AsyncClient,
    row: ExistingRow,
    bullets: list[str],
    source: Source,
    result: ReconcileResult,
) -> None:
    """Leave a comment proposing bullets for an existing row.

    A comment rather than an edit — this is the whole safety property. Even for a row still
    in Draft: you may be mid-edit, and an automated rewrite would clobber that.
    """
    if not bullets:
        return

    lines = "\n".join(f"• {bullet}" for bullet in bullets)
    body = (
        f"🤖 從 {source.name} 抽取到以下內容，供你參考是否加入：\n\n{lines}\n\n"
        f"（pipeline 不會自動修改這一筆，請自行判斷後手動加入。）"
    )

    try:
        response = await client.post(
            "/comments",
            json={
                "parent": {"page_id": row.page_id},
                "rich_text": [{"text": {"content": body[:2000]}}],
            },
        )
        response.raise_for_status()
        result.commented.append(row.label)
        log.info("suggested %d bullet(s) on %r", len(bullets), row.label)
    except httpx.HTTPError as exc:
        log.warning("could not comment on %r: %s", row.label, exc)


# --- row creation -----------------------------------------------------------


async def _create_page(
    client: httpx.AsyncClient,
    database_id: str,
    properties: dict[str, Any],
    bullets: list[str],
    label: str,
) -> str | None:
    """Create a review row, with bullets in the page body per the master page's format."""
    payload: dict[str, Any] = {
        "parent": {"database_id": database_id},
        "properties": properties,
    }
    if bullets:
        payload["children"] = [
            {
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": [{"text": {"content": bullet[:2000]}}]},
            }
            for bullet in bullets
        ]

    try:
        response = await client.post("/pages", json=payload)
        response.raise_for_status()
        page_id = response.json()["id"]
        log.info("created review row for %r", label)
        return page_id
    except (httpx.HTTPError, KeyError) as exc:
        log.warning("could not create row for %r: %s", label, exc)
        return None


def _review_properties(source: Source, extraction: Extraction) -> dict[str, Any]:
    """The bookkeeping every created row carries.

    `Pending Review` plus an unchecked `Include in Resume` are the two gates: nothing
    created here can reach a resume until you flip both.
    """
    return {
        "Status": {"select": {"name": Status.PENDING_REVIEW.value}},
        "Include in Resume": {"checkbox": False},
        "Source": {"rich_text": [{"text": {"content": source.name}}]},
        "Confidence": {"select": {"name": extraction.confidence.value}},
        "Last Synced": {"date": {"start": datetime.now(UTC).date().isoformat()}},
    }


async def _create_experience(
    client: httpx.AsyncClient,
    candidate: CandidateExperience,
    source: Source,
    extraction: Extraction,
    settings: Settings,
) -> str | None:
    properties: dict[str, Any] = {
        "Role": {"title": [{"text": {"content": candidate.role}}]},
        "Organization": {"select": {"name": candidate.organization[:100]}},
        **_review_properties(source, extraction),
    }
    if candidate.location:
        properties["Location"] = {"rich_text": [{"text": {"content": candidate.location}}]}
    if start := _parse_date(candidate.start):
        properties["Start"] = {"date": {"start": start.isoformat()}}
    if end := _parse_date(candidate.end):
        properties["End"] = {"date": {"start": end.isoformat()}}
    if tags := _known_tags(candidate.tags):
        properties["Tags"] = {"multi_select": [{"name": tag} for tag in tags]}

    return await _create_page(
        client,
        settings.notion_db_experiences,
        properties,
        candidate.bullets,
        candidate.organization,
    )


async def _create_project(
    client: httpx.AsyncClient,
    candidate: CandidateProject,
    source: Source,
    extraction: Extraction,
    settings: Settings,
) -> str | None:
    properties: dict[str, Any] = {
        "Name": {"title": [{"text": {"content": candidate.name}}]},
        **_review_properties(source, extraction),
    }
    if candidate.affiliation:
        properties["Affiliation"] = {"select": {"name": candidate.affiliation[:100]}}
    if candidate.context:
        properties["Context"] = {"rich_text": [{"text": {"content": candidate.context}}]}
    if when := _parse_date(candidate.date):
        properties["Date"] = {"date": {"start": when.isoformat()}}
    if tags := _known_tags(candidate.tags):
        properties["Tags"] = {"multi_select": [{"name": tag} for tag in tags]}

    return await _create_page(
        client, settings.notion_db_projects, properties, candidate.bullets, candidate.name
    )


async def _create_skill(
    client: httpx.AsyncClient,
    candidate: CandidateSkill,
    source: Source,
    extraction: Extraction,
    settings: Settings,
) -> str | None:
    # An unknown category would be rejected by Notion's select, and silently dropping the
    # skill is worse than filing it under Tools for you to recategorize.
    category = candidate.category
    if category not in {c.value for c in SkillCategory}:
        log.info("skill %r had unknown category %r; filing under Tools", candidate.name, category)
        category = SkillCategory.TOOLS.value

    properties: dict[str, Any] = {
        "Name": {"title": [{"text": {"content": candidate.name}}]},
        "Category": {"select": {"name": category}},
        **_review_properties(source, extraction),
    }
    if candidate.detail:
        properties["Detail"] = {"rich_text": [{"text": {"content": candidate.detail}}]}

    return await _create_page(client, settings.notion_db_skills, properties, [], candidate.name)


async def _create_publication(
    client: httpx.AsyncClient,
    candidate: CandidatePublication,
    source: Source,
    extraction: Extraction,
    settings: Settings,
) -> str | None:
    properties: dict[str, Any] = {
        "Title": {"title": [{"text": {"content": candidate.title}}]},
        **_review_properties(source, extraction),
    }
    if candidate.venue:
        properties["Venue"] = {"rich_text": [{"text": {"content": candidate.venue}}]}
    if candidate.authors:
        properties["Authors"] = {"rich_text": [{"text": {"content": candidate.authors}}]}
    if when := _parse_date(candidate.date):
        properties["Date"] = {"date": {"start": when.isoformat()}}

    return await _create_page(
        client, settings.notion_db_publications, properties, [], candidate.title
    )


# --- matching ---------------------------------------------------------------


def normalize(text: str) -> str:
    """Fold case, width, and punctuation so near-identical names compare equal.

    NFKC first, because a full-width parenthesis from a Chinese document and an ASCII one
    from a PRD are the same character to a reader.
    """
    text = unicodedata.normalize("NFKC", text).casefold()
    return "".join(char for char in text if char.isalnum() or char.isspace()).strip()


def similarity(left: str, right: str) -> float:
    """0.0-1.0 similarity, with a containment bonus.

    SequenceMatcher alone scores "Cathay DDT" against "Cathay Financial Holdings — DDT AI"
    around 0.5, because the second string is far longer. But one being a prefix or subset
    of the other is exactly how abbreviated references appear in source documents, so
    containment is treated as a strong signal.
    """
    a, b = normalize(left), normalize(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.95

    # Token overlap catches word-order differences that a character-level ratio misses.
    a_tokens, b_tokens = set(a.split()), set(b.split())
    if a_tokens and b_tokens:
        overlap = len(a_tokens & b_tokens) / min(len(a_tokens), len(b_tokens))
        if overlap >= 0.8:
            return 0.9

    return SequenceMatcher(None, a, b).ratio()


def _match_by_label(name: str, rows: list[ExistingRow]) -> ExistingRow | None:
    """Best match above the threshold, or None."""
    best: tuple[float, ExistingRow] | None = None
    for row in rows:
        score = similarity(name, row.label)
        if score >= MATCH_THRESHOLD and (best is None or score > best[0]):
            best = (score, row)
    return best[1] if best else None


def _match_experience(
    candidate: CandidateExperience, rows: list[ExistingRow]
) -> ExistingRow | None:
    """Match on organization, with a date window to separate repeat stints.

    Two internships at the same company a year apart are different jobs. Without the date
    check, the second would attach its bullets to the first.
    """
    candidate_start = _parse_date(candidate.start)
    best: tuple[float, ExistingRow] | None = None

    for row in rows:
        score = similarity(candidate.organization, row.label)
        if score < MATCH_THRESHOLD:
            continue
        # Only applied when both sides have a date: most source documents state none, and
        # requiring one would create a duplicate row for every undated candidate.
        if (
            candidate_start
            and row.start
            and abs((candidate_start - row.start).days) > DATE_WINDOW_DAYS
        ):
            continue
        if best is None or score > best[0]:
            best = (score, row)

    return best[1] if best else None


# --- property helpers -------------------------------------------------------

# Only tags that exist in the Notion schema; Notion rejects an unknown multi_select option.
KNOWN_TAGS = frozenset(
    {
        "LLM", "RAG", "AWS", "LangGraph", "NLP", "Phonology", "Frontend",
        "Slack", "LINE", "ETL", "Prompt Engineering",
    }
)  # fmt: skip


def _known_tags(tags: list[str]) -> list[str]:
    """Keep only tags the schema already defines, matched case-insensitively."""
    lookup = {tag.casefold(): tag for tag in KNOWN_TAGS}
    return [lookup[tag.casefold()] for tag in tags if tag.casefold() in lookup]


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        log.debug("unparseable candidate date %r", value)
        return None


def _title_value(props: dict[str, Any], name: str | None) -> str | None:
    if not name:
        return None
    parts = props.get(name, {}).get("title", [])
    return "".join(p.get("plain_text", "") for p in parts).strip() or None


def _select_value(props: dict[str, Any], name: str | None) -> str | None:
    if not name:
        return None
    option = props.get(name, {}).get("select")
    return option.get("name") if option else None


def _date_value(props: dict[str, Any], name: str) -> date | None:
    payload = props.get(name, {}).get("date")
    if not payload or not payload.get("start"):
        return None
    return _parse_date(payload["start"])
