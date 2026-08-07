"""Stage 5: English resume → Chinese resume.

Human text wins. For every field there are three possibilities, checked in this order:

1. A `ZH Override` property or a `## 中文` section in the Notion page body — used as-is.
2. Nothing written, so the LLM drafts it.
3. Content that shouldn't be translated at all (Python, AWS, LangGraph, an email) — kept
   verbatim.

The point of (1) is that Chinese resume convention differs from English: 國泰金控 rather
than a transliteration, 語言學碩士 rather than "碩士 of 語言學". A machine translation is a
starting draft, not the deliverable, so anything you write by hand is never overwritten.

If no API key is configured, translation degrades to passthrough rather than failing the
run: a Chinese resume showing English text is recoverable, a failed run is not.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from pipeline.config import Settings
from pipeline.llm import LLMError, structured
from pipeline.models import Resume

if TYPE_CHECKING:
    from pipeline.notion_client import NotionReader

log = logging.getLogger(__name__)

SYSTEM = """\
You translate résumé content from English into Traditional Chinese for a Taiwanese \
technical audience.

Rules:
- Traditional Chinese (zh-TW), never Simplified.
- Keep technical terms, product names, and acronyms in English: LLM, RAG, AWS Lambda, \
LangGraph, ETL, Python, Slack, LINE, PostgresSaver, schwa, mora. Taiwanese engineers \
read these in English; translating them makes the résumé harder to scan, not easier.
- Keep numbers, percentages, and version strings exactly as written.
- Use 、 to separate items in a list, not a Latin comma.
- Match the register of a professional Taiwanese résumé: concise, factual, no marketing \
adjectives the English did not have.
- Translate meaning, not word order. A bullet that reads naturally in English and \
awkwardly in Chinese has been translated wrongly.
- Return exactly as many strings as you were given, in the same order."""

# Categories whose values are proper nouns — translating them would be actively wrong.
NEVER_TRANSLATE_CATEGORIES = frozenset({"Programming", "Cloud & Infra", "Frameworks", "Tools"})


class TranslatedStrings(BaseModel):
    """A batch of translations, positionally matched to the input."""

    translations: list[str] = Field(description="One translation per input string, same order")


async def translate_batch(texts: list[str], settings: Settings) -> list[str]:
    """Translate a list of strings in one call.

    Batched deliberately: one call for a whole resume gives the model cross-bullet
    context, so repeated project names stay consistent. Per-string calls would be slower,
    costlier, and less consistent.
    """
    if not texts:
        return []

    numbered = "\n".join(f"{index}. {text}" for index, text in enumerate(texts, 1))
    prompt = (
        f"Translate these {len(texts)} résumé strings into Traditional Chinese.\n\n"
        f"{numbered}\n\n"
        f"Return exactly {len(texts)} translations in the same order."
    )

    result = await structured(prompt, TranslatedStrings, settings, system=SYSTEM)

    if len(result.translations) != len(texts):
        # A length mismatch would silently shift every subsequent field onto the wrong
        # entry, which is worse than leaving the resume in English.
        raise LLMError(
            f"expected {len(texts)} translations, got {len(result.translations)}"
        )
    return result.translations


async def translate_resume(
    resume: Resume,
    reader: NotionReader | None,
    settings: Settings,
) -> Resume:
    """Build the Chinese resume from `resume`, honouring every human override."""
    data = resume.model_dump(mode="json", by_alias=True)
    data["lang"] = "zh"

    overrides = reader.zh_overrides if reader else {}
    bullets_by_page = reader.bullets if reader else {}

    # Collected as (setter, text) pairs so one LLM call covers the whole resume; the
    # setters are applied once the translations come back.
    pending: list[tuple[str, list[str | int], str]] = []

    def queue(text: str | None, path: list[str | int]) -> None:
        if text and text.strip():
            pending.append(("set", path, text))

    # --- Profile: name and summary have dedicated override keys ---
    if zh_name := overrides.get("profile:name"):
        data["profile"]["name"] = zh_name
    else:
        queue(resume.profile.name, ["profile", "name"])

    if zh_summary := overrides.get("profile:summary"):
        data["profile"]["summary"] = zh_summary
    else:
        queue(resume.profile.summary, ["profile", "summary"])

    # Contact details are never translated — an email is an email.

    # --- Education ---
    for index, item in enumerate(resume.education):
        _apply_title_override(
            data["education"][index], overrides.get(item.notion_page_id or ""), ("institution",)
        )
        if not overrides.get(item.notion_page_id or ""):
            queue(item.institution, ["education", index, "institution"])
        queue(item.degree, ["education", index, "degree"])
        queue(item.field, ["education", index, "field"])
        queue(item.coursework, ["education", index, "coursework"])
        _handle_bullets(data["education"][index], item, bullets_by_page, pending, ["education", index])

    # --- Experiences ---
    for index, item in enumerate(resume.experiences):
        override = overrides.get(item.notion_page_id or "")
        if override:
            # "國泰金控 DDT AI | ML/AI 工程師實習生" — organization and role in one field,
            # because a single Notion property has to carry both.
            org, _, role = override.partition("|")
            data["experiences"][index]["organization"] = org.strip()
            if role.strip():
                data["experiences"][index]["role"] = role.strip()
            else:
                queue(item.role, ["experiences", index, "role"])
        else:
            queue(item.organization, ["experiences", index, "organization"])
            queue(item.role, ["experiences", index, "role"])
        queue(item.location, ["experiences", index, "location"])
        _handle_bullets(
            data["experiences"][index], item, bullets_by_page, pending, ["experiences", index]
        )

    # --- Projects ---
    for index, item in enumerate(resume.projects):
        override = overrides.get(item.notion_page_id or "")
        if override:
            data["projects"][index]["name"] = override
        else:
            queue(item.name, ["projects", index, "name"])
        queue(item.context, ["projects", index, "context"])
        _handle_bullets(data["projects"][index], item, bullets_by_page, pending, ["projects", index])

    # --- Publications ---
    for index, item in enumerate(resume.publications):
        override = overrides.get(item.notion_page_id or "")
        if override:
            data["publications"][index]["title"] = override
        else:
            queue(item.title, ["publications", index, "title"])
        queue(item.venue, ["publications", index, "venue"])
        _handle_bullets(
            data["publications"][index], item, bullets_by_page, pending, ["publications", index]
        )

    # --- Skills ---
    for index, item in enumerate(resume.skills):
        override = overrides.get(item.notion_page_id or "")
        if override:
            # "英文｜流利" — name and detail, separated by a full-width bar.
            name, _, detail = override.partition("｜")
            data["skills"][index]["name"] = name.strip()
            if detail.strip():
                data["skills"][index]["detail"] = detail.strip()
        elif item.category.value in NEVER_TRANSLATE_CATEGORIES:
            # Python stays Python. Details like "Lambda, Bedrock, OpenSearch" are product
            # names too, so only the separator is localized.
            if item.detail:
                data["skills"][index]["detail"] = item.detail.replace(", ", "、")
        else:
            queue(item.name, ["skills", index, "name"])
            queue(item.detail, ["skills", index, "detail"])

    if not pending:
        log.info("every field had a human override — no LLM call needed")
        return Resume.model_validate(data)

    texts = [text for _, _, text in pending]
    log.info("translating %d field(s) in one call", len(texts))

    try:
        translations = await translate_batch(texts, settings)
    except LLMError as exc:
        # Passthrough beats a failed run: you can still read the Chinese resume, see the
        # English text, and fix it with a ZH Override.
        log.error("translation failed, leaving %d field(s) in English: %s", len(texts), exc)
        return Resume.model_validate(data)

    for (_, path, _), translated in zip(pending, translations, strict=True):
        _assign(data, path, translated)

    return Resume.model_validate(data)


def _apply_title_override(target: dict, override: str | None, keys: tuple[str, ...]) -> None:
    if override:
        target[keys[0]] = override


def _handle_bullets(
    target: dict,
    item: object,
    bullets_by_page: dict,
    pending: list[tuple[str, list[str | int], str]],
    path: list[str | int],
) -> None:
    """Use a `## 中文` section if the page has one; otherwise queue the bullets."""
    page_id = getattr(item, "notion_page_id", None)
    bullets = getattr(item, "bullets", [])
    if page_id and (parsed := bullets_by_page.get(page_id)) and parsed.has_zh_override():
        target["bullets"] = parsed.zh
        return
    for index, bullet in enumerate(bullets):
        if bullet.strip():
            pending.append(("set", [*path, "bullets", index], bullet))


def _assign(data: dict, path: list[str | int], value: str) -> None:
    """Write `value` at a nested path like ["experiences", 0, "bullets", 2]."""
    node: object = data
    for key in path[:-1]:
        node = node[key]  # type: ignore[index]
    node[path[-1]] = value  # type: ignore[index]
