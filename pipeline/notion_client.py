"""Notion REST client (stage 4: read Notion → Resume).

Uses the REST API rather than MCP because this runs on Render, where MCP is not
available. The Resume Master page must be shared with the integration whose token is
in `NOTION_TOKEN`; without that share step every read returns 404 with a valid token.

Two read paths per database:

1. `POST /databases/{id}/query` for the property values.
2. `GET /blocks/{page_id}/children` for the bullets, which live in page body rather
   than a property — rich_text properties cap at 2000 characters and are awkward to
   edit on a phone.

The body convention is: top-level bulleted list items are English bullets; anything
after a `## 中文` heading is the Chinese override. See the master page for the format.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import Any, Literal

import httpx

from pipeline.config import Settings
from pipeline.models import (
    Confidence,
    Education,
    Experience,
    Profile,
    Project,
    Publication,
    Resume,
    Skill,
    SkillCategory,
    Status,
)

log = logging.getLogger(__name__)

API_BASE = "https://api.notion.com/v1"
ZH_HEADING = "中文"

# Notion caps list responses at 100 items. Every read here paginates, but a single
# resume section exceeding a few hundred rows means something is wrong upstream.
PAGE_SIZE = 100
MAX_PAGES = 20


class NotionError(RuntimeError):
    """A Notion API call failed in a way the run cannot recover from."""


class Bullets:
    """Bullets parsed out of a page body, split by language."""

    __slots__ = ("en", "zh")

    def __init__(self, en: list[str], zh: list[str]) -> None:
        self.en = en
        self.zh = zh

    def for_lang(self, lang: Literal["en", "zh"]) -> list[str]:
        """Chinese falls back to English when no `## 中文` section was written.

        The fallback matters: stage 5 needs to know which bullets still need an LLM
        translation draft, and an empty list would render a header with no content.
        """
        if lang == "zh" and self.zh:
            return self.zh
        return self.en

    def has_zh_override(self) -> bool:
        return bool(self.zh)


class NotionReader:
    """Reads the seven databases and assembles an English `Resume`.

    Chinese is produced later by stage 5, which consults `zh_overrides` for the human
    text and only calls the LLM for what is missing.
    """

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client
        self._owns_client = client is None
        # page_id -> Bullets, populated during read so stage 5 can reuse it without
        # re-fetching every page body.
        self.bullets: dict[str, Bullets] = {}
        # page_id -> ZH Override property text (title/org name, not bullets).
        self.zh_overrides: dict[str, str] = {}

    async def __aenter__(self) -> NotionReader:
        if self._client is None:
            token = self._settings.notion_token.get_secret_value()
            if not token:
                raise NotionError(
                    "NOTION_TOKEN is empty. Create an internal integration at "
                    "https://www.notion.so/my-integrations and share the Resume Master page with it."
                )
            self._client = httpx.AsyncClient(
                base_url=API_BASE,
                timeout=httpx.Timeout(30.0),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Notion-Version": self._settings.notion_version,
                    "Content-Type": "application/json",
                },
            )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # --- HTTP ---------------------------------------------------------------
    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        assert self._client is not None, "use NotionReader as an async context manager"
        for attempt in range(4):
            response = await self._client.request(method, path, **kwargs)
            if response.status_code == 429:
                # Notion sends Retry-After in seconds; honour it rather than guessing.
                delay = float(response.headers.get("Retry-After", 2**attempt))
                log.warning("notion rate limited, retrying in %.1fs", delay)
                await asyncio.sleep(delay)
                continue
            if response.status_code >= 500:
                await asyncio.sleep(2**attempt)
                continue
            if response.status_code == 404:
                raise NotionError(
                    f"404 on {path}. The integration probably isn't connected to the "
                    "Resume Master page — open it, ⋯ → Connections, and add the integration."
                )
            if response.status_code >= 400:
                raise NotionError(f"{response.status_code} on {path}: {response.text[:400]}")
            return response.json()
        raise NotionError(f"{method} {path} still failing after retries")

    async def _query_all(self, database_id: str) -> list[dict[str, Any]]:
        """All rows of a database, following pagination."""
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(MAX_PAGES):
            payload: dict[str, Any] = {"page_size": PAGE_SIZE}
            if cursor:
                payload["start_cursor"] = cursor
            data = await self._request("POST", f"/databases/{database_id}/query", json=payload)
            rows.extend(data.get("results", []))
            if not data.get("has_more"):
                return rows
            cursor = data.get("next_cursor")
        log.warning("database %s exceeded %d pages; truncating", database_id, MAX_PAGES)
        return rows

    async def _fetch_bullets(self, page_id: str) -> Bullets:
        """Parse a page body into English and Chinese bullet lists.

        Only top-level bulleted list items count. Nested children are ignored on
        purpose: a resume bullet that needs sub-bullets is a bullet that needs
        rewriting, and flattening them silently would produce odd output.
        """
        en: list[str] = []
        zh: list[str] = []
        target = en
        cursor: str | None = None

        for _ in range(MAX_PAGES):
            params: dict[str, Any] = {"page_size": PAGE_SIZE}
            if cursor:
                params["start_cursor"] = cursor
            data = await self._request("GET", f"/blocks/{page_id}/children", params=params)

            for block in data.get("results", []):
                block_type = block.get("type")
                if block_type in ("heading_1", "heading_2", "heading_3"):
                    heading = _plain_text(block.get(block_type, {}).get("rich_text", []))
                    # Any heading containing 中文 switches the target; a later heading
                    # switches back, so a trailing "## Notes" section is not treated as
                    # Chinese bullets.
                    target = zh if ZH_HEADING in heading else en
                elif block_type == "bulleted_list_item":
                    if text := _plain_text(block["bulleted_list_item"].get("rich_text", [])):
                        target.append(text)

            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")

        return Bullets(en=en, zh=zh)

    async def _approved_rows(self, database_id: str) -> list[dict[str, Any]]:
        """Rows past both gates: Status=Approved and Include in Resume checked.

        Filtering client-side rather than in the query keeps one code path for the
        Profile database, which has no Status property at all.
        """
        rows = await self._query_all(database_id)
        approved = []
        for row in rows:
            props = row.get("properties", {})
            if not _checkbox(props, "Include in Resume"):
                continue
            status = _select(props, "Status")
            # A database without a Status property (Profile) passes on the checkbox alone.
            if status is not None and status != Status.APPROVED.value:
                continue
            approved.append(row)
        return approved

    async def _load_bullets_for(self, rows: list[dict[str, Any]]) -> None:
        """Fetch page bodies concurrently and record them on `self.bullets`."""
        page_ids = [row["id"] for row in rows]
        if not page_ids:
            return
        results = await asyncio.gather(*(self._fetch_bullets(pid) for pid in page_ids))
        self.bullets.update(dict(zip(page_ids, results, strict=True)))

    def _record_zh_override(self, row: dict[str, Any]) -> None:
        if text := _rich_text(row.get("properties", {}), "ZH Override"):
            self.zh_overrides[row["id"]] = text

    # --- section readers ----------------------------------------------------
    async def read_profile(self) -> Profile:
        """Profile is a key-value database; `Field` is the key.

        `Name EN` is required — a resume with no name is not a resume. Everything else
        renders only if the row exists and is included.
        """
        rows = await self._approved_rows(self._settings.notion_db_profile)
        values: dict[str, tuple[str, str]] = {}
        for row in rows:
            props = row["properties"]
            key = _title(props, "Field")
            if not key:
                continue
            values[key.casefold()] = (
                _rich_text(props, "Value (EN)") or "",
                _rich_text(props, "Value (ZH)") or "",
            )

        def pick(field: str, lang_index: int = 0) -> str | None:
            pair = values.get(field.casefold())
            if not pair:
                return None
            return pair[lang_index] or None

        name = pick("Name EN")
        if not name:
            raise NotionError(
                "Profile has no 'Name EN' row with Include in Resume checked. "
                f"See {self._settings.notion_master_page_id}"
            )
        # Chinese names are kept for stage 5 rather than a second Profile model.
        if zh_name := pick("Name ZH", 1):
            self.zh_overrides["profile:name"] = zh_name
        if zh_summary := pick("Summary", 1):
            self.zh_overrides["profile:summary"] = zh_summary

        return Profile(
            name=name,
            email=pick("Email"),
            phone=pick("Phone"),
            github=pick("GitHub"),
            linkedin=pick("LinkedIn"),
            website=pick("Website"),
            summary=pick("Summary"),
        )

    async def read_experiences(self) -> list[Experience]:
        rows = await self._approved_rows(self._settings.notion_db_experiences)
        await self._load_bullets_for(rows)
        items = []
        for row in rows:
            props = row["properties"]
            self._record_zh_override(row)
            start = _date(props, "Start")
            role = _title(props, "Role")
            organization = _select(props, "Organization") or _rich_text(props, "Organization")
            if not (start and role and organization):
                log.warning(
                    "skipping experience %s: missing Role, Organization, or Start", row["id"]
                )
                continue
            bullets = self.bullets.get(row["id"], Bullets([], [])).en
            if not bullets:
                log.warning("skipping experience %r: page body has no bullets", role)
                continue
            items.append(
                Experience(
                    notion_page_id=row["id"],
                    role=role,
                    organization=organization,
                    location=_rich_text(props, "Location"),
                    start=start,
                    end=_date(props, "End"),
                    type=_select(props, "Type"),
                    tags=_multi_select(props, "Tags"),
                    bullets=bullets,
                    priority=_number(props, "Priority") or 0,
                    highlight=_checkbox(props, "Highlight"),
                    source=_rich_text(props, "Source"),
                    confidence=_confidence(props),
                )
            )
        return items

    async def read_projects(self) -> list[Project]:
        rows = await self._approved_rows(self._settings.notion_db_projects)
        await self._load_bullets_for(rows)
        items = []
        for row in rows:
            props = row["properties"]
            self._record_zh_override(row)
            name = _title(props, "Name")
            if not name:
                log.warning("skipping project %s: no Name", row["id"])
                continue
            bullets = self.bullets.get(row["id"], Bullets([], [])).en
            if not bullets:
                log.warning("skipping project %r: page body has no bullets", name)
                continue
            items.append(
                Project(
                    notion_page_id=row["id"],
                    name=name,
                    affiliation=_select(props, "Affiliation"),
                    context=_rich_text(props, "Context"),
                    date=_date(props, "Date"),
                    repo_url=_url(props, "Repo URL"),
                    tags=_multi_select(props, "Tags"),
                    bullets=bullets,
                    priority=_number(props, "Priority") or 0,
                    highlight=_checkbox(props, "Highlight"),
                    source=_rich_text(props, "Source"),
                    confidence=_confidence(props),
                )
            )
        return items

    async def read_education(self) -> list[Education]:
        rows = await self._approved_rows(self._settings.notion_db_education)
        await self._load_bullets_for(rows)
        items = []
        for row in rows:
            props = row["properties"]
            self._record_zh_override(row)
            institution = _title(props, "Institution")
            if not institution:
                log.warning("skipping education %s: no Institution", row["id"])
                continue
            items.append(
                Education(
                    notion_page_id=row["id"],
                    institution=institution,
                    degree=_rich_text(props, "Degree"),
                    field=_rich_text(props, "Field"),
                    start=_date(props, "Start"),
                    end=_date(props, "End"),
                    expected=_checkbox(props, "Expected"),
                    coursework=_rich_text(props, "Coursework"),
                    programs=_multi_select(props, "Programs"),
                    bullets=self.bullets.get(row["id"], Bullets([], [])).en,
                    priority=_number(props, "Priority") or 0,
                    source=_rich_text(props, "Source"),
                    confidence=_confidence(props),
                )
            )
        return items

    async def read_skills(self) -> list[Skill]:
        rows = await self._approved_rows(self._settings.notion_db_skills)
        items = []
        for row in rows:
            props = row["properties"]
            self._record_zh_override(row)
            name = _title(props, "Name")
            raw_category = _select(props, "Category")
            if not (name and raw_category):
                log.warning("skipping skill %s: missing Name or Category", row["id"])
                continue
            try:
                category = SkillCategory(raw_category)
            except ValueError:
                log.warning("skipping skill %r: unknown Category %r", name, raw_category)
                continue
            items.append(
                Skill(
                    notion_page_id=row["id"],
                    name=name,
                    category=category,
                    detail=_rich_text(props, "Detail"),
                    priority=_number(props, "Priority") or 0,
                    source=_rich_text(props, "Source"),
                    confidence=_confidence(props),
                )
            )
        return items

    async def read_publications(self) -> list[Publication]:
        rows = await self._approved_rows(self._settings.notion_db_publications)
        await self._load_bullets_for(rows)
        items = []
        for row in rows:
            props = row["properties"]
            self._record_zh_override(row)
            title = _title(props, "Title")
            if not title:
                log.warning("skipping publication %s: no Title", row["id"])
                continue
            items.append(
                Publication(
                    notion_page_id=row["id"],
                    title=title,
                    venue=_rich_text(props, "Venue"),
                    date=_date(props, "Date"),
                    authors=_rich_text(props, "Authors"),
                    url=_url(props, "URL"),
                    type=_publication_type(props),
                    bullets=self.bullets.get(row["id"], Bullets([], [])).en,
                    priority=_number(props, "Priority") or 0,
                    source=_rich_text(props, "Source"),
                    confidence=_confidence(props),
                )
            )
        return items

    async def read_resume(self) -> Resume:
        """Assemble the English resume. Sections are read concurrently."""
        profile, education, experiences, projects, publications, skills = await asyncio.gather(
            self.read_profile(),
            self.read_education(),
            self.read_experiences(),
            self.read_projects(),
            self.read_publications(),
            self.read_skills(),
        )
        # Resume's validator raises if every section is empty, which is the loudest
        # signal that the Notion gates are misconfigured.
        return Resume(
            lang="en",
            profile=profile,
            education=education,
            experiences=experiences,
            projects=projects,
            publications=publications,
            skills=skills,
        )


# --- property extractors ----------------------------------------------------
# Notion property payloads are deeply nested and every field is nullable. These
# helpers return None rather than raising so one empty cell can't fail a whole run.


def _plain_text(rich_text: list[dict[str, Any]]) -> str:
    return "".join(part.get("plain_text", "") for part in rich_text).strip()


def _title(props: dict[str, Any], name: str) -> str | None:
    return _plain_text(props.get(name, {}).get("title", [])) or None


def _rich_text(props: dict[str, Any], name: str) -> str | None:
    return _plain_text(props.get(name, {}).get("rich_text", [])) or None


def _select(props: dict[str, Any], name: str) -> str | None:
    option = props.get(name, {}).get("select")
    return option.get("name") if option else None


def _multi_select(props: dict[str, Any], name: str) -> list[str]:
    options = props.get(name, {}).get("multi_select") or []
    return [o["name"] for o in options if o.get("name")]


def _checkbox(props: dict[str, Any], name: str) -> bool:
    return bool(props.get(name, {}).get("checkbox"))


def _number(props: dict[str, Any], name: str) -> int | None:
    value = props.get(name, {}).get("number")
    return int(value) if value is not None else None


def _url(props: dict[str, Any], name: str) -> str | None:
    return props.get(name, {}).get("url") or None


def _date(props: dict[str, Any], name: str) -> date | None:
    payload = props.get(name, {}).get("date")
    if not payload or not payload.get("start"):
        return None
    raw = payload["start"]
    try:
        # Notion sends either "2026-02-01" or a full ISO datetime.
        return datetime.fromisoformat(raw).date() if "T" in raw else date.fromisoformat(raw)
    except ValueError:
        log.warning("unparseable date %r in property %r", raw, name)
        return None


def _confidence(props: dict[str, Any]) -> Confidence | None:
    raw = _select(props, "Confidence")
    if not raw:
        return None
    try:
        return Confidence(raw)
    except ValueError:
        return None


def _publication_type(props: dict[str, Any]) -> Any:
    from pipeline.models import PublicationType

    raw = _select(props, "Type")
    if not raw:
        return None
    try:
        return PublicationType(raw)
    except ValueError:
        return None
