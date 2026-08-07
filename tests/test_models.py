"""Model invariants: ordering, grouping, key stability, and validation gates."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from pipeline.models import (
    Education,
    Experience,
    Profile,
    Resume,
    Skill,
    SkillCategory,
    slugify,
)

FIXTURE = Path(__file__).parent / "fixtures" / "resume.sample.json"


@pytest.fixture
def sample() -> Resume:
    return Resume.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


def test_fixture_parses(sample: Resume) -> None:
    assert sample.lang == "en"
    assert sample.profile.name == "WU, YU-HSUAN"
    assert len(sample.experiences) == 3
    assert len(sample.projects) == 3
    assert len(sample.education) == 2


def test_ongoing_experience_sorts_first(sample: Resume) -> None:
    """The Cathay role has no end date and must lead the EXPERIENCE section."""
    ordered = sample.sorted_experiences()
    assert ordered[0].organization == "Cathay Financial Holdings — DDT AI"
    assert ordered[0].end is None


def test_experiences_sort_by_priority_then_recency() -> None:
    """Equal priority falls back to recency, with ongoing roles ahead of ended ones."""
    common = {"role": "R", "priority": 50, "bullets": ["b"]}
    older = Experience(organization="Older", start=date(2020, 1, 1), end=date(2021, 1, 1), **common)
    newer = Experience(organization="Newer", start=date(2023, 1, 1), end=date(2024, 1, 1), **common)
    ongoing = Experience(organization="Ongoing", start=date(2019, 1, 1), **common)

    resume = Resume(
        lang="en",
        profile=Profile(name="T"),
        experiences=[older, newer, ongoing],
    )
    assert [e.organization for e in resume.sorted_experiences()] == ["Ongoing", "Newer", "Older"]


def test_skills_grouped_in_declaration_order(sample: Resume) -> None:
    """SKILLS renders one line per category, in SkillCategory order, empties omitted."""
    grouped = sample.skills_by_category()
    assert list(grouped) == [
        SkillCategory.LANGUAGES,
        SkillCategory.PROGRAMMING,
        SkillCategory.CLOUD_INFRA,
        SkillCategory.FRAMEWORKS,
        SkillCategory.TOOLS,
        SkillCategory.CERTIFICATES,
    ]
    # Within a category, higher priority first: Mandarin (100) before English (90).
    assert [s.name for s in grouped[SkillCategory.LANGUAGES]] == ["Mandarin", "English"]


def test_stable_keys_are_unique(sample: Resume) -> None:
    keys = [item.stable_key() for item in sample.all_items()]
    assert len(keys) == len(set(keys)), "duplicate stable keys would collapse rows in the diff"


def test_stable_key_survives_rewording() -> None:
    """A reworded role must diff as 'modified', not as delete+add."""
    before = Experience(
        role="ML/AI Engineer Intern",
        organization="Cathay Financial Holdings — DDT AI",
        start=date(2026, 2, 1),
        bullets=["a"],
    )
    after = before.model_copy(update={"role": "Machine Learning Engineer Intern"})
    assert before.stable_key() == after.stable_key()


def test_stable_key_normalizes_punctuation_width() -> None:
    """NFKC folding means full-width and half-width punctuation share one key."""
    assert slugify("Cathay（DDT）") == slugify("Cathay(DDT)")


def test_stable_key_preserves_cjk() -> None:
    """A Chinese-only organization name must still produce a usable key."""
    key = Experience(
        role="實習生", organization="國泰金控", start=date(2026, 2, 1), bullets=["a"]
    ).stable_key()
    assert "國泰金控" in key


def test_contact_line_skips_empty_fields() -> None:
    profile = Profile(name="T", email="a@b.c", phone=None, github="gh")
    assert profile.contact_line() == ["a@b.c", "gh"]


def test_reversed_dates_rejected() -> None:
    with pytest.raises(ValidationError, match="precedes start"):
        Experience(
            role="R",
            organization="O",
            start=date(2026, 1, 1),
            end=date(2025, 1, 1),
            bullets=["b"],
        )


def test_experience_requires_a_bullet() -> None:
    """An experience with no bullets renders as a dangling header; fail the run instead."""
    with pytest.raises(ValidationError):
        Experience(role="R", organization="O", start=date(2026, 1, 1), bullets=[])


def test_empty_resume_rejected() -> None:
    """Guards the most likely Notion misconfiguration: nothing marked Approved."""
    with pytest.raises(ValidationError, match="Status=Approved"):
        Resume(lang="en", profile=Profile(name="T"))


def test_skills_only_resume_rejected() -> None:
    """Skills alone is not a resume — it means the section gates are misconfigured."""
    with pytest.raises(ValidationError, match="Status=Approved"):
        Resume(
            lang="en",
            profile=Profile(name="T"),
            skills=[Skill(name="Python", category=SkillCategory.PROGRAMMING)],
        )


def test_unknown_field_rejected() -> None:
    """extra='forbid' catches Notion property renames instead of silently dropping data."""
    with pytest.raises(ValidationError):
        Education.model_validate({"institution": "NTU", "graduation_year": 2026})


def test_roundtrip_is_lossless(sample: Resume) -> None:
    """Diff and publish both re-serialize; a lossy round-trip would fake a diff."""
    dumped = sample.model_dump(mode="json", by_alias=True)
    assert Resume.model_validate(dumped) == sample
    assert json.loads(json.dumps(dumped, ensure_ascii=False))  # JSON-safe
