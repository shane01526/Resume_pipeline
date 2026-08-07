"""LaTeX renderer: escaping correctness and template structure.

These run without Tectonic installed — they check the generated `.tex` source. The
compile itself is covered by `test_render_golden.py`, which skips when Tectonic is
absent.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pipeline.config import Settings, get_settings
from pipeline.models import Resume
from pipeline.render.latex import SIZES, SIZES_ZH, render_tex, tex_escape

FIXTURE = Path(__file__).parent / "fixtures" / "resume.sample.json"


@pytest.fixture
def settings() -> Settings:
    return get_settings()


@pytest.fixture
def resume_en() -> Resume:
    return Resume.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def resume_zh(resume_en: Resume) -> Resume:
    """A partially-translated Chinese resume — the realistic stage 5 output."""
    data = resume_en.model_dump(mode="json", by_alias=True)
    data["lang"] = "zh"
    data["profile"]["name"] = "吳雨諠"
    data["experiences"][0]["organization"] = "國泰金控 DDT AI"
    data["experiences"][0]["role"] = "ML/AI 工程師實習生"
    data["experiences"][0]["bullets"] = [
        "優化 AI News agent 的編排邏輯、資料 ETL 與 AWS 部署架構。"
    ]
    data["education"][0]["institution"] = "國立陽明交通大學"
    data["education"][0]["degree"] = "碩士"
    data["education"][0]["field"] = "語言學"
    return Resume.model_validate(data)


# --- escaping ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("AI & Data", r"AI \& Data"),
        ("str_replace_editor", r"str\_replace\_editor"),
        ("50% coverage", r"50\% coverage"),
        ("$5 per token", r"\$5 per token"),
        ("C#", r"C\#"),
        ("{braces}", r"\{braces\}"),
        ("~tilde", r"\textasciitilde{}tilde"),
        ("2^10", r"2\textasciicircum{}10"),
        ("a\\b", r"a\textbackslash{}b"),
        (None, ""),
        ("", ""),
    ],
)
def test_tex_escape(raw: str | None, expected: str) -> None:
    assert tex_escape(raw) == expected


def test_backslash_escaped_first() -> None:
    """Escaping order matters: a naive pass re-escapes its own replacements' backslashes."""
    assert tex_escape("&") == r"\&"
    # If "\\" were replaced last, this would become \textbackslash{}\textbackslash{}&.
    assert tex_escape("\\&") == r"\textbackslash{}\&"


def test_cjk_passes_through_unescaped() -> None:
    """xeCJK handles these natively; escaping would corrupt them."""
    for text in ("國泰金控", "、", "（預計）", "～", "工作經歷"):
        assert tex_escape(text) == text


# --- template structure ------------------------------------------------------


def test_no_unrendered_jinja_tags(resume_en: Resume, settings: Settings) -> None:
    """A leftover \\BLOCK or \\VAR means a tag was malformed and TeX would choke."""
    source = render_tex(resume_en, settings)
    assert not re.findall(r"\\(?:BLOCK|VAR)\{", source)
    assert r"\#{" not in source


def test_sections_present_in_order(resume_en: Resume, settings: Settings) -> None:
    source = render_tex(resume_en, settings)
    sections = re.findall(r"\\section\{([^}]+)\}", source)
    assert sections == ["Education", "Experience", "Projects", "Skills"]


def test_every_entry_rendered(resume_en: Resume, settings: Settings) -> None:
    """Two education + three experience + three project entries = eight heads."""
    source = render_tex(resume_en, settings)
    assert len(re.findall(r"\\entryhead\{", source)) == 8


def test_bullets_rendered(resume_en: Resume, settings: Settings) -> None:
    source = render_tex(resume_en, settings)
    expected = sum(
        len(item.bullets)
        for item in (*resume_en.education, *resume_en.experiences, *resume_en.projects)
    )
    assert len(re.findall(r"^\s*\\item ", source, flags=re.MULTILINE)) >= expected


def test_ampersand_in_content_is_escaped(resume_en: Resume, settings: Settings) -> None:
    """The AIA role contains 'AI Developer & Technical Support' — a bare & aborts TeX.

    Guarded structurally rather than by counting: tabularx uses & as a column separator,
    so the check is that no & appears immediately after a letter (content) rather than
    inside a table row.
    """
    source = render_tex(resume_en, settings)
    assert not re.search(r"[A-Za-z]&", source), "unescaped ampersand in body text"


def test_document_is_complete(resume_en: Resume, settings: Settings) -> None:
    source = render_tex(resume_en, settings)
    assert source.lstrip().startswith(r"\documentclass")
    assert r"\begin{document}" in source
    assert source.rstrip().endswith(r"\end{document}")


# --- language switching ------------------------------------------------------


def test_zh_uses_chinese_labels(resume_zh: Resume, settings: Settings) -> None:
    source = render_tex(resume_zh, settings)
    assert re.findall(r"\\section\{([^}]+)\}", source) == [
        "學歷",
        "工作經歷",
        "專案成果",
        "專業技能",
    ]


def test_zh_field_precedes_degree(resume_zh: Resume, settings: Settings) -> None:
    """Chinese convention is 語言學碩士, not 碩士 語言學."""
    assert "語言學碩士" in render_tex(resume_zh, settings)


def test_zh_partial_translation_stays_readable(resume_zh: Resume, settings: Settings) -> None:
    """An untranslated row must not have its Latin words mashed together.

    The NTU row keeps its English degree and field; joining with the Chinese empty
    separator would produce 'Bachelor of ArtsForeign Languages'.
    """
    source = render_tex(resume_zh, settings)
    assert "Bachelor of Arts in Foreign Languages" in source
    assert "ArtsForeign" not in source


def test_zh_drops_italics(resume_zh: Resume, settings: Settings) -> None:
    """Synthesised obliques on CJK faces look broken; weight carries the distinction."""
    assert r"\itshape" not in render_tex(resume_zh, settings)
    assert r"\itshape" in render_tex(resume_zh.model_copy(update={"lang": "en"}), get_settings())


def test_zh_uses_looser_leading() -> None:
    """Denser CJK glyphs read as cramped at the Latin leading."""
    assert SIZES_ZH["body_leading"] != SIZES["body_leading"]
    assert float(SIZES_ZH["body_leading"].removesuffix("pt")) > float(
        SIZES["body_leading"].removesuffix("pt")
    )


def test_zh_skill_join_is_ideographic(resume_zh: Resume, settings: Settings) -> None:
    """A Latin ', ' next to CJK glyphs sits at the wrong height."""
    source = render_tex(resume_zh, settings)
    assert "Mandarin（Native）、English（Fluent）" in source
