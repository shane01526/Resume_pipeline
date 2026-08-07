"""Canonical resume data model.

Every stage of the pipeline speaks in these types. Stage 4 (read Notion) produces a
`Resume`; stage 5 (translate) produces a second `Resume` with `lang="zh"`; stage 6
(render) consumes one and emits files; stage 7 (diff) compares two.

Design notes:

- English is canonical. Chinese values live in the same shape, produced either from a
  human-written `ZH Override` / `## 中文` section or from an LLM draft.
- Every item carries `notion_page_id` so diffs can point back at the row you edit,
  and `source` so you can trace where a machine-extracted claim came from.
- `stable_key()` is what the diff engine matches on across runs. It must not include
  free text that you might reword, or every copy-edit would read as delete+add.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Lang = Literal["en", "zh"]


class Status(StrEnum):
    """Gate 1. Only `APPROVED` rows reach a rendered resume."""

    DRAFT = "Draft"
    PENDING_REVIEW = "Pending Review"
    APPROVED = "Approved"
    ARCHIVED = "Archived"


class Confidence(StrEnum):
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


class SkillCategory(StrEnum):
    """Each category renders as one line in the SKILLS section, in this order."""

    LANGUAGES = "Languages"
    PROGRAMMING = "Programming"
    CLOUD_INFRA = "Cloud & Infra"
    FRAMEWORKS = "Frameworks"
    TOOLS = "Tools"
    CERTIFICATES = "Certificates"


class PublicationType(StrEnum):
    CONFERENCE_PAPER = "Conference Paper"
    POSTER = "Poster"
    TALK = "Talk"
    PREPRINT = "Preprint"
    JOURNAL_ARTICLE = "Journal Article"


def slugify(text: str) -> str:
    """Stable, language-agnostic key fragment.

    CJK is preserved rather than stripped, so a Chinese-only organization name still
    produces a usable key. NFKC normalization first so that full-width and half-width
    punctuation don't produce two different keys for the same string.
    """
    text = unicodedata.normalize("NFKC", text).casefold()
    text = re.sub(r"[^\w一-鿿]+", "-", text)
    return text.strip("-")


class ResumeItem(BaseModel):
    """Fields shared by every section entry."""

    model_config = ConfigDict(extra="forbid")

    notion_page_id: str | None = Field(
        default=None, description="Notion row this came from; None for hand-built fixtures"
    )
    priority: int = Field(default=0, description="Higher sorts first within its section")
    highlight: bool = False
    source: str | None = Field(
        default=None, description="Source file a machine extraction came from"
    )
    confidence: Confidence | None = None

    def stable_key(self) -> str:  # pragma: no cover - overridden by every subclass
        raise NotImplementedError


class Experience(ResumeItem):
    role: str
    organization: str
    location: str | None = None
    start: date
    end: date | None = Field(default=None, description="None renders as 'Present'")
    type: str | None = None
    tags: list[str] = Field(default_factory=list)
    bullets: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _dates_ordered(self) -> Self:
        if self.end and self.end < self.start:
            raise ValueError(f"{self.organization}: end {self.end} precedes start {self.start}")
        return self

    def stable_key(self) -> str:
        # Organization + start month. Role wording gets edited; the job identity doesn't.
        return f"exp:{slugify(self.organization)}:{self.start:%Y-%m}"


class Project(ResumeItem):
    name: str
    affiliation: str | None = None
    context: str | None = Field(default=None, description="Right-hand line, e.g. 'ROCLING 2025'")
    date_: date | None = Field(default=None, alias="date")
    repo_url: str | None = None
    tags: list[str] = Field(default_factory=list)
    bullets: list[str] = Field(min_length=1)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    def stable_key(self) -> str:
        return f"proj:{slugify(self.name)}"


class Education(ResumeItem):
    institution: str
    degree: str | None = None
    field: str | None = None
    start: date | None = None
    end: date | None = None
    expected: bool = Field(default=False, description="Renders the year as '2028 (expected)'")
    coursework: str | None = None
    programs: list[str] = Field(default_factory=list)
    bullets: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _dates_ordered(self) -> Self:
        if self.start and self.end and self.end < self.start:
            raise ValueError(f"{self.institution}: end {self.end} precedes start {self.start}")
        return self

    def stable_key(self) -> str:
        return f"edu:{slugify(self.institution)}"


class Skill(ResumeItem):
    name: str
    category: SkillCategory
    detail: str | None = None

    def stable_key(self) -> str:
        return f"skill:{self.category.value}:{slugify(self.name)}"


class Publication(ResumeItem):
    title: str
    venue: str | None = None
    date_: date | None = Field(default=None, alias="date")
    authors: str | None = None
    url: str | None = None
    type: PublicationType | None = None
    bullets: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    def stable_key(self) -> str:
        return f"pub:{slugify(self.title)}"


class Profile(BaseModel):
    """Header block. `name` is the only required field; the rest render if present."""

    model_config = ConfigDict(extra="forbid")

    name: str
    email: str | None = None
    phone: str | None = None
    github: str | None = None
    linkedin: str | None = None
    website: str | None = None
    summary: str | None = None

    def contact_line(self) -> list[str]:
        """Non-empty contact values, in the order they appear under the name."""
        return [v for v in (self.email, self.phone, self.github, self.linkedin, self.website) if v]


class Resume(BaseModel):
    """One language's worth of resume content, ready to render."""

    model_config = ConfigDict(extra="forbid")

    lang: Lang
    profile: Profile
    education: list[Education] = Field(default_factory=list)
    experiences: list[Experience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    publications: list[Publication] = Field(default_factory=list)
    skills: list[Skill] = Field(default_factory=list)

    @model_validator(mode="after")
    def _not_empty(self) -> Self:
        if not (self.education or self.experiences or self.projects):
            raise ValueError(
                "resume has no education, experience, or projects — "
                "check that Notion rows are Status=Approved and Include in Resume is checked"
            )
        return self

    def sorted_education(self) -> list[Education]:
        return sorted(self.education, key=lambda e: (-e.priority, _end_sort_key(e.end)))

    def sorted_experiences(self) -> list[Experience]:
        # Ongoing roles (end=None) first, then most recent end date.
        return sorted(
            self.experiences,
            key=lambda e: (-e.priority, _end_sort_key(e.end), -e.start.toordinal()),
        )

    def sorted_projects(self) -> list[Project]:
        return sorted(self.projects, key=lambda p: (-p.priority, _end_sort_key(p.date_)))

    def sorted_publications(self) -> list[Publication]:
        return sorted(self.publications, key=lambda p: (-p.priority, _end_sort_key(p.date_)))

    def skills_by_category(self) -> dict[SkillCategory, list[Skill]]:
        """Grouped for rendering, in `SkillCategory` declaration order. Empty groups omitted."""
        grouped: dict[SkillCategory, list[Skill]] = {}
        for category in SkillCategory:
            members = [s for s in self.skills if s.category is category]
            if members:
                grouped[category] = sorted(members, key=lambda s: (-s.priority, s.name))
        return grouped

    def all_items(self) -> list[ResumeItem]:
        """Every section entry, for diffing. Order is irrelevant; keys are what match."""
        return [
            *self.education,
            *self.experiences,
            *self.projects,
            *self.publications,
            *self.skills,
        ]


def _end_sort_key(value: date | None) -> int:
    """Sort key that puts `None` (ongoing / undated) first, then newest first.

    `None` means "present" for an experience and "undated" elsewhere; in both cases it
    belongs at the top. Returning a very small number for None and the negated ordinal
    otherwise gives newest-first among real dates.
    """
    if value is None:
        return -(10**9)
    return -value.toordinal()
