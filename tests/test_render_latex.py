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
from pipeline.render.latex import FONTS, SIZES, SIZES_ZH, render_tex, tex_escape, write_tex

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


def test_template_comments_contain_no_closing_brace() -> None:
    """A `}` inside a Jinja comment silently ends it and leaks prose into the document.

    The comment delimiter is a single closing brace, so writing `\\MakeUppercase{#1}` or
    `\\VAR{x}` inside explanatory prose terminates the comment there. The remainder becomes
    body text before `\\begin{document}`, and TeX reports "Missing \\begin{document}" at a
    line that looks perfectly fine in the template — which is exactly how this cost an
    afternoon.
    """
    template = (Path(__file__).parent.parent / "templates" / "resume.tex.j2").read_text(
        encoding="utf-8"
    )

    offenders = []
    for match in re.finditer(r"\\#\{", template):
        close = template.index("}", match.end())
        # Anything non-whitespace after the closing brace on the same line means the
        # comment ended earlier than the author intended.
        line_end = template.find("\n", close)
        trailing = template[close + 1 : line_end if line_end != -1 else None]
        if trailing.strip():
            line_number = template[: match.start()].count("\n") + 1
            offenders.append((line_number, trailing.strip()[:60]))

    assert not offenders, "Jinja comments truncated by an inner '}': " + "; ".join(
        f"line {n}: leaked {text!r}" for n, text in offenders
    )


def test_generated_preamble_has_no_leaked_prose(resume_en: Resume, settings: Settings) -> None:
    """No English sentence fragments may precede \\begin{document}.

    Catches leaked comment prose whatever its cause. Checked by looking for runs of plain
    words rather than by whitelisting TeX syntax — the preamble legitimately contains
    continuation lines like `a4paper,` inside a multi-line \\usepackage, so a
    "must start with a backslash" rule produces false positives.
    """
    preamble = render_tex(resume_en, settings).split(r"\begin{document}")[0]

    offenders = []
    for index, line in enumerate(preamble.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("%") or "\\" in stripped:
            continue
        # Three or more consecutive plain words is prose, not a TeX option list.
        if re.match(r"^[A-Za-z][a-z]*(\s+[A-Za-z][a-z]*){2,}", stripped):
            offenders.append((index, stripped[:60]))

    assert not offenders, "prose leaked into the preamble: " + "; ".join(
        f"line {n}: {text!r}" for n, text in offenders
    )


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
    # Leading `%` lines are the compiler magic comment and the Overleaf notice, both of
    # which are content of the published file — so this looks for the first real line.
    first_code = next(
        line for line in source.splitlines() if line.strip() and not line.startswith("%")
    )
    assert first_code.startswith(r"\documentclass")
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


# --- Overleaf portability ----------------------------------------------------
# The .tex is a published artifact, opened in Overleaf by hand. Overleaf ships TeX Live,
# not this image's Linux font set, so the source has to survive the absence of every Noto
# font. These assert on the generated preamble because that file is the deliverable.


def test_declares_xelatex_on_the_first_line(resume_en: Resume, settings: Settings) -> None:
    """Overleaf reads this magic comment to pick the compiler.

    Without it the default is pdfLaTeX, which cannot load fontspec — the first thing a
    reader would see is a compile error, not a resume.
    """
    assert render_tex(resume_en, settings).splitlines()[0] == "% !TEX program = xelatex"


@pytest.mark.parametrize(
    ("command", "primary", "fallbacks"),
    [
        ("setmainfont", "serif", ["serif_fallback"]),
        ("setsansfont", "sans", ["sans_fallback"]),
        ("setCJKmainfont", "cjk_serif", ["cjk_serif_fallback", "cjk_serif_fallback2"]),
        ("setCJKsansfont", "cjk_sans", ["cjk_sans_fallback", "cjk_sans_fallback2"]),
    ],
)
def test_every_font_family_is_guarded_with_a_fallback(
    command: str, primary: str, fallbacks: list[str], resume_en: Resume, settings: Settings
) -> None:
    """An unguarded \\setmainfont aborts the whole compile where the font is missing."""
    preamble = render_tex(resume_en, settings).split(r"\begin{document}")[0]

    assert rf"\IfFontExistsTF{{{FONTS[primary]}}}" in preamble
    for key in fallbacks:
        assert rf"\{command}{{{FONTS[key]}}}" in preamble, f"{key} is not reachable"


def test_cjk_fallback_prefers_a_traditional_face(resume_zh: Resume, settings: Settings) -> None:
    """Fandol is Simplified-only, so it must be the last resort, not the first.

    This resume is Traditional Chinese; falling straight to Fandol would typeset the
    characters it lacks as tofu — a compile that succeeds and a document that is unusable,
    which is worse than an error.
    """
    preamble = render_tex(resume_zh, settings).split(r"\begin{document}")[0]
    assert preamble.index(FONTS["cjk_serif_fallback"]) < preamble.index(
        FONTS["cjk_serif_fallback2"]
    )
    assert "Fandol" in FONTS["cjk_serif_fallback2"]


def test_symbol_commands_are_defined_on_both_branches(
    resume_en: Resume, settings: Settings
) -> None:
    """`\\bulletmark` and the arrows are emitted by tex_escape, so both branches need them.

    The fallback branch must also avoid \\newfontfamily: that command errors on a missing
    font, which would defeat the guard it sits inside.
    """
    preamble = render_tex(resume_en, settings).split(r"\begin{document}")[0]
    guard = preamble.index(rf"\IfFontExistsTF{{{FONTS['symbol']}}}")
    with_font, without_font = preamble[guard:].split(r"\newcommand{\bulletmark}")[0:3:2]

    assert r"\newfontfamily\symbolfont" in with_font
    assert r"\newfontfamily" not in without_font
    for command in (r"\bulletmark", r"\arrowright", r"\arrowboth"):
        assert preamble.count(rf"\newcommand{{{command}}}") == 2, f"{command} misses a branch"


def test_overleaf_instructions_reach_the_generated_file(
    resume_en: Resume, settings: Settings
) -> None:
    """The guidance must be a LaTeX comment, not a Jinja one.

    Written as `\\#{...}` it is stripped during rendering, so the person who opens the
    published .tex — the only audience for it — never sees a word.
    """
    preamble = render_tex(resume_en, settings).split(r"\begin{document}")[0]
    assert "Overleaf" in preamble
    assert "XeLaTeX" in preamble
    # That hand edits do not survive the next run is the part worth stating.
    assert "resume.tex.j2" in preamble


def test_write_tex_writes_utf8_with_lf(
    resume_zh: Resume, settings: Settings, tmp_path: Path
) -> None:
    """.gitattributes normalises the repo to LF; CRLF would rewrite the file every publish."""
    output = write_tex(resume_zh, settings, tmp_path / "nested" / "resume.tex")

    assert output.is_file()
    raw = output.read_bytes()
    assert b"\r\n" not in raw
    assert raw.decode("utf-8").startswith("% !TEX program = xelatex")
    assert "國泰金控 DDT AI" in raw.decode("utf-8")
