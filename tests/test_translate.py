"""Stage 5: human overrides win, product names survive, failure degrades."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pipeline.config import Settings, get_settings
from pipeline.models import Resume
from pipeline.translate import NEVER_TRANSLATE_CATEGORIES, translate_resume

FIXTURE = Path(__file__).parent / "fixtures" / "resume.sample.json"


class FakeReader:
    """Stands in for NotionReader without a network call."""

    def __init__(
        self,
        overrides: dict[str, str] | None = None,
        bullets: dict[str, Any] | None = None,
    ) -> None:
        self.zh_overrides = overrides or {}
        self.bullets = bullets or {}


class FakeBullets:
    def __init__(self, zh: list[str]) -> None:
        self.zh = zh

    def has_zh_override(self) -> bool:
        return bool(self.zh)


@pytest.fixture
def settings() -> Settings:
    # Every translation call fails here, so the passthrough path is what these tests
    # exercise. That is the point: it proves a broken LLM cannot break a run.
    #
    # What makes it fail is the no-network guard in conftest, NOT a missing key. This
    # comment used to claim the key was absent, which was true until one got exported —
    # then these tests quietly started making real Bedrock calls and two of them failed
    # because the model translated strings they assert come back in English.
    return get_settings()


@pytest.fixture
def resume_en() -> Resume:
    return Resume.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


async def test_lang_is_switched(resume_en: Resume, settings: Settings) -> None:
    zh = await translate_resume(resume_en, FakeReader(), settings)
    assert zh.lang == "zh"


async def test_llm_failure_degrades_to_passthrough(resume_en: Resume, settings: Settings) -> None:
    """A resume in English beats no resume at all.

    You can still read it, spot the untranslated text, and fix it with a ZH Override.
    """
    zh = await translate_resume(resume_en, FakeReader(), settings)
    assert zh.experiences[0].organization == resume_en.experiences[0].organization
    assert len(zh.experiences[0].bullets) == len(resume_en.experiences[0].bullets)


async def test_structure_is_preserved(resume_en: Resume, settings: Settings) -> None:
    """Translation must not add, drop, or reorder entries."""
    zh = await translate_resume(resume_en, FakeReader(), settings)
    assert len(zh.experiences) == len(resume_en.experiences)
    assert len(zh.projects) == len(resume_en.projects)
    assert len(zh.skills) == len(resume_en.skills)
    assert [e.notion_page_id for e in zh.experiences] == [
        e.notion_page_id for e in resume_en.experiences
    ]


async def test_profile_name_override_applied(resume_en: Resume, settings: Settings) -> None:
    zh = await translate_resume(resume_en, FakeReader({"profile:name": "吳雨諠"}), settings)
    assert zh.profile.name == "吳雨諠"


async def test_email_never_translated(resume_en: Resume, settings: Settings) -> None:
    zh = await translate_resume(resume_en, FakeReader(), settings)
    assert zh.profile.email == resume_en.profile.email


async def test_experience_override_splits_org_and_role(
    resume_en: Resume, settings: Settings
) -> None:
    """One Notion property carries both, separated by a bar."""
    page_id = resume_en.experiences[0].notion_page_id
    assert page_id
    zh = await translate_resume(
        resume_en, FakeReader({page_id: "國泰金控 DDT AI | ML/AI 工程師實習生"}), settings
    )
    target = next(e for e in zh.experiences if e.notion_page_id == page_id)
    assert target.organization == "國泰金控 DDT AI"
    assert target.role == "ML/AI 工程師實習生"


async def test_experience_override_without_role_keeps_english_role(
    resume_en: Resume, settings: Settings
) -> None:
    """An override with no bar translates the org and leaves the role to the LLM."""
    page_id = resume_en.experiences[0].notion_page_id
    assert page_id
    zh = await translate_resume(resume_en, FakeReader({page_id: "國泰金控 DDT AI"}), settings)
    target = next(e for e in zh.experiences if e.notion_page_id == page_id)
    assert target.organization == "國泰金控 DDT AI"
    # Passthrough, because there is no API key in tests.
    assert target.role == resume_en.experiences[0].role


async def test_skill_override_splits_name_and_detail(resume_en: Resume, settings: Settings) -> None:
    """Full-width bar separates the two, matching the master page's documented format."""
    skill = next(s for s in resume_en.skills if s.name == "English")
    assert skill.notion_page_id
    zh = await translate_resume(
        resume_en, FakeReader({skill.notion_page_id: "英文｜流利"}), settings
    )
    target = next(s for s in zh.skills if s.notion_page_id == skill.notion_page_id)
    assert target.name == "英文"
    assert target.detail == "流利"


async def test_zh_bullet_section_used_verbatim(resume_en: Resume, settings: Settings) -> None:
    """A `## 中文` section in the page body replaces the bullets entirely."""
    page_id = resume_en.experiences[0].notion_page_id
    assert page_id
    written = ["優化 AI News agent 的編排邏輯。", "打造 BRD 自動修訂 agent。"]
    zh = await translate_resume(
        resume_en, FakeReader(bullets={page_id: FakeBullets(written)}), settings
    )
    target = next(e for e in zh.experiences if e.notion_page_id == page_id)
    assert target.bullets == written


# --- product names ----------------------------------------------------------


async def test_product_name_skills_untouched(resume_en: Resume, settings: Settings) -> None:
    """Python is Python. Translating it makes the resume harder to scan, not easier."""
    zh = await translate_resume(resume_en, FakeReader(), settings)
    for name in ("Python", "AWS", "LangGraph", "Microsoft Office"):
        assert any(s.name == name for s in zh.skills), f"{name} was altered"


async def test_product_detail_separator_localized(resume_en: Resume, settings: Settings) -> None:
    """The values stay English; only the separator becomes ideographic."""
    zh = await translate_resume(resume_en, FakeReader(), settings)
    aws = next(s for s in zh.skills if s.name == "AWS")
    assert aws.detail == "Lambda、Bedrock、OpenSearch"


def test_never_translate_covers_product_categories() -> None:
    """Languages and Certificates are prose and DO get translated; the rest do not."""
    assert "Programming" in NEVER_TRANSLATE_CATEGORIES
    assert "Cloud & Infra" in NEVER_TRANSLATE_CATEGORIES
    assert "Frameworks" in NEVER_TRANSLATE_CATEGORIES
    assert "Tools" in NEVER_TRANSLATE_CATEGORIES
    assert "Languages" not in NEVER_TRANSLATE_CATEGORIES
    assert "Certificates" not in NEVER_TRANSLATE_CATEGORIES


# --- batching contract ------------------------------------------------------


async def test_length_mismatch_is_rejected(settings: Settings) -> None:
    """A short batch would shift every later field onto the wrong entry.

    Silent corruption is the worst outcome here — worse than an untranslated resume —
    so translate_batch treats a count mismatch as a hard error.
    """
    import pipeline.translate as translate_module
    from pipeline.llm import LLMError
    from pipeline.translate import TranslatedStrings, translate_batch

    async def short_response(*_args: object, **_kwargs: object) -> TranslatedStrings:
        return TranslatedStrings(translations=["一"])  # asked for three

    original = translate_module.structured
    translate_module.structured = short_response  # type: ignore[assignment]
    try:
        with pytest.raises(LLMError, match="expected 3 translations, got 1"):
            await translate_batch(["a", "b", "c"], settings)
    finally:
        translate_module.structured = original  # type: ignore[assignment]


async def test_empty_batch_skips_the_call(settings: Settings) -> None:
    from pipeline.translate import translate_batch

    assert await translate_batch([], settings) == []


async def test_all_overrides_means_no_llm_call(resume_en: Resume, settings: Settings) -> None:
    """A fully hand-written Chinese resume must not spend a token.

    Verified by monkeypatching structured() to raise: if it is called, the test fails.
    """
    import pipeline.translate as translate_module

    async def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("structured() was called despite full overrides")

    # A minimal resume where every translatable field has an override.
    minimal = Resume.model_validate(
        {
            "lang": "en",
            "profile": {"name": "WU, YU-HSUAN", "email": "a@b.c"},
            "experiences": [
                {
                    "notion_page_id": "page-1",
                    "role": "Intern",
                    "organization": "Cathay",
                    "start": "2026-02-01",
                    "bullets": ["Did a thing."],
                }
            ],
            "skills": [{"notion_page_id": "page-2", "name": "Python", "category": "Programming"}],
        }
    )
    reader = FakeReader(
        overrides={"profile:name": "吳雨諠", "page-1": "國泰金控 | 實習生"},
        bullets={"page-1": FakeBullets(["做了一件事。"])},
    )

    original = translate_module.structured
    translate_module.structured = explode  # type: ignore[assignment]
    try:
        zh = await translate_resume(minimal, reader, settings)
    finally:
        translate_module.structured = original  # type: ignore[assignment]

    assert zh.profile.name == "吳雨諠"
    assert zh.experiences[0].bullets == ["做了一件事。"]


async def test_works_without_a_reader(resume_en: Resume, settings: Settings) -> None:
    """Stage 5 must run on a fixture, with no Notion reader at all."""
    zh = await translate_resume(resume_en, None, settings)
    assert zh.lang == "zh"
    assert len(zh.experiences) == len(resume_en.experiences)
