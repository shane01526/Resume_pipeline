"""Section labels and date formatting, shared by all three renderers.

Kept out of the templates so the HTML, LaTeX, and docx outputs cannot disagree about
what a section is called or how a date range reads. Chinese labels follow local resume
convention rather than translating the English headings literally — 工作經歷 rather than
"經驗", 專業技能 rather than "技能".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from pipeline.models import Education, Skill, SkillCategory

Lang = Literal["en", "zh"]

EN_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)  # fmt: skip


@dataclass(frozen=True)
class Labels:
    """Every user-visible string that isn't resume content."""

    document_title: str
    education: str
    experience: str
    projects: str
    publications: str
    skills: str
    present: str
    expected: str
    degree_join: str
    skill_join: str
    skill_categories: dict[str, str] = field(default_factory=dict)


EN = Labels(
    document_title="Resume",
    education="Education",
    experience="Experience",
    projects="Projects",
    publications="Publications & Talks",
    skills="Skills",
    present="Present",
    # Trailing space intentional: "2028 (expected)" needs the gap after the year.
    expected=" (expected)",
    degree_join=" in ",
    skill_join=", ",
    skill_categories={
        SkillCategory.LANGUAGES.value: "Languages",
        SkillCategory.PROGRAMMING.value: "Programming",
        SkillCategory.CLOUD_INFRA.value: "Cloud & Infrastructure",
        SkillCategory.FRAMEWORKS.value: "Frameworks",
        SkillCategory.TOOLS.value: "Tools",
        SkillCategory.CERTIFICATES.value: "Certificates",
    },
)

ZH = Labels(
    document_title="履歷",
    education="學歷",
    experience="工作經歷",
    projects="專案成果",
    publications="論文與演講",
    skills="專業技能",
    present="至今",
    expected="（預計）",
    # "語言學碩士" — the degree follows the field in Chinese, with no joining word.
    degree_join="",
    # Ideographic comma. A Latin ", " next to CJK glyphs sits at the wrong height.
    skill_join="、",
    skill_categories={
        SkillCategory.LANGUAGES.value: "語言",
        SkillCategory.PROGRAMMING.value: "程式語言",
        SkillCategory.CLOUD_INFRA.value: "雲端與基礎架構",
        SkillCategory.FRAMEWORKS.value: "框架",
        SkillCategory.TOOLS.value: "工具",
        SkillCategory.CERTIFICATES.value: "證照",
    },
)


def labels_for(lang: Lang) -> Labels:
    return ZH if lang == "zh" else EN


def fmt_month(value: date | None, lang: Lang = "en") -> str:
    """A month-precision date. Day is never shown — resumes don't use it."""
    if value is None:
        return ""
    if lang == "zh":
        return f"{value.year} 年 {value.month} 月"
    return f"{EN_MONTHS[value.month - 1]} {value.year}"


def fmt_range(start: date | None, end: date | None, lang: Lang = "en") -> str:
    """A date range, with an open end reading as Present / 至今.

    An en dash separates the endpoints in English; Chinese uses a wave dash, which is
    the convention in CJK typography and avoids the dash colliding with adjacent glyphs.
    """
    labels = labels_for(lang)
    dash = "～" if lang == "zh" else "–"
    if start is None:
        return fmt_month(end, lang)
    left = fmt_month(start, lang)
    right = fmt_month(end, lang) if end else labels.present
    return f"{left} {dash} {right}" if lang == "zh" else f"{left} {dash} {right}"


def fmt_education_date(item: Education, lang: Lang = "en") -> str:
    """Education shows a graduation year, not a range.

    The year alone is the convention, and an expected graduation is marked so a future
    date doesn't read as an error.
    """
    labels = labels_for(lang)
    if item.end is None:
        return ""
    year = str(item.end.year)
    return f"{year}{labels.expected}" if item.expected else year


def fmt_skill(skill: Skill, lang: Lang = "en") -> str:
    """A skill as one string: name, with its detail parenthesised if present."""
    if not skill.detail:
        return skill.name
    if lang == "zh":
        return f"{skill.name}（{skill.detail}）"
    return f"{skill.name} ({skill.detail})"


def _has_cjk(text: str) -> bool:
    # Unified ideographs only; enough to tell 語言學 from "Linguistics".
    return any("一" <= char <= "鿿" for char in text)


def fmt_degree(item: Education, lang: Lang = "en") -> str:
    """Degree and field as one line: "Master of Arts in Linguistics" / "語言學碩士".

    Chinese puts the field first with no joining word, but that only works when both
    parts are actually Chinese. If a translation is missing — stage 5 fell back, or the
    row was never given a ZH Override — joining Latin text with an empty string would
    produce "Bachelor of ArtsForeign Languages". Falling back to the English joiner
    keeps a partially-translated resume readable instead of corrupt.
    """
    parts = [p for p in (item.degree, item.field) if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    degree, field_ = parts
    if lang == "zh" and _has_cjk(degree) and _has_cjk(field_):
        # Field precedes degree in Chinese: 語言學 + 碩士.
        return f"{field_}{degree}"
    return f"{degree}{EN.degree_join}{field_}"
