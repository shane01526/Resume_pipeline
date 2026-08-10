"""Compile the LaTeX template for real, and check the PDFs the other engines produce.

These tests exist because of a bug the source-level tests could not catch: the template's
bullet marker used `\\blacksquare`, which needs `amssymb`. The generated `.tex` was
perfectly well-formed, every structural assertion passed, and *every* LaTeX render would
have aborted with "Undefined control sequence" in production. Checking the input is not
checking the output.

Skipped where the toolchain is absent (Tectonic is not installed on Windows), so the suite
still runs locally — but they do run in the Docker image, which is where it matters.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from pipeline.config import Settings, get_settings
from pipeline.models import Resume
from pipeline.render import latex as latex_renderer

FIXTURE = Path(__file__).parent / "fixtures" / "resume.sample.json"

pytestmark = pytest.mark.skipif(
    shutil.which("tectonic") is None, reason="tectonic not installed (present in the Docker image)"
)


@pytest.fixture
def settings() -> Settings:
    return get_settings()


@pytest.fixture
def resume_en() -> Resume:
    return Resume.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def resume_zh(resume_en: Resume) -> Resume:
    """Fully-translated Chinese content, to exercise the xeCJK path end to end."""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data["lang"] = "zh"
    data["profile"]["name"] = "吳雨諠"
    data["education"][0].update(
        institution="國立陽明交通大學",
        degree="碩士",
        field="語言學",
        bullets=["預計修習課程：音韻學、計算語言學"],
    )
    data["education"][1].update(
        institution="國立臺灣大學",
        degree="學士",
        field="外國語文學",
        bullets=["參與臺大計算語言學學程", "修習課程：自然語言處理、計算語言學、音韻學"],
    )
    data["experiences"][0].update(
        organization="國泰金控 DDT AI",
        role="ML/AI 工程師實習生",
        location="台北",
        bullets=[
            "優化 AI News agent 的編排邏輯、資料 ETL、LINE 整合與 AWS 部署架構。",
            "打造以錄音 ASR 為基礎、具 human-in-the-loop 審核的 BRD 自動修訂 agent。",
        ],
    )
    return Resume.model_validate(data)


def compile_to(resume: Resume, settings: Settings, tmp_path: Path) -> Path:
    output = tmp_path / f"resume.{resume.lang}.pdf"
    return latex_renderer.render_pdf(resume, settings, output)


# --- the regression -----------------------------------------------------------


def test_english_resume_compiles(resume_en: Resume, settings: Settings, tmp_path: Path) -> None:
    """The test that would have caught the \\blacksquare bug."""
    pdf = compile_to(resume_en, settings, tmp_path)
    assert pdf.is_file()
    assert pdf.stat().st_size > 10_000  # a real document, not an error stub
    assert pdf.read_bytes().startswith(b"%PDF")


def test_chinese_resume_compiles(resume_zh: Resume, settings: Settings, tmp_path: Path) -> None:
    """xeCJK, the CJK fonts, and the zh line-breaking locale, all for real."""
    pdf = compile_to(resume_zh, settings, tmp_path)
    assert pdf.is_file()
    assert pdf.stat().st_size > 10_000


def test_bullet_marker_needs_no_extra_package(
    resume_en: Resume, settings: Settings, tmp_path: Path
) -> None:
    """The marker must be a literal glyph, not a command from an unloaded package.

    Asserted on the source as well as the compile, so the reason stays visible if someone
    reaches for \\blacksquare again.
    """
    source = latex_renderer.render_tex(resume_en, settings)
    assert "blacksquare" not in source
    assert latex_renderer.BULLET_MARKER in source
    compile_to(resume_en, settings, tmp_path)


# --- content actually reaches the page ----------------------------------------


@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="poppler-utils not installed")
def test_english_text_survives_compilation(
    resume_en: Resume, settings: Settings, tmp_path: Path
) -> None:
    """A PDF can compile and still be missing content — check the text layer."""
    pdf = compile_to(resume_en, settings, tmp_path)
    text = _pdftotext(pdf)

    assert "WU, YU-HSUAN" in text
    assert "Cathay Financial Holdings" in text
    # Section headings are uppercased by titlesec.
    assert "EXPERIENCE" in text
    assert "PROJECTS" in text
    # An ampersand in content is the classic TeX abort; prove it rendered as a character.
    assert "AI Developer & Technical Support" in text


@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="poppler-utils not installed")
def test_chinese_text_survives_compilation(
    resume_zh: Resume, settings: Settings, tmp_path: Path
) -> None:
    """Missing CJK glyphs compile fine and produce blank boxes — only the text shows it."""
    text = _pdftotext(compile_to(resume_zh, settings, tmp_path))

    assert "吳雨諠" in text
    assert "國泰金控" in text
    assert "工作經歷" in text
    # Field-then-degree order, per Chinese convention.
    assert "語言學碩士" in text


@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="poppler-utils not installed")
def test_every_bullet_reaches_the_pdf(
    resume_en: Resume, settings: Settings, tmp_path: Path
) -> None:
    """A silently dropped bullet is worse than a failed compile."""
    text = " ".join(_pdftotext(compile_to(resume_en, settings, tmp_path)).split())
    for item in (*resume_en.experiences, *resume_en.projects):
        for bullet in item.bullets:
            # Compare a prefix: TeX rebreaks lines, so the full string won't match verbatim.
            prefix = " ".join(bullet.split()[:6])
            assert prefix in text, f"missing from PDF: {prefix}"


@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="poppler-utils not installed")
def test_dashes_keep_the_space_before_them(
    resume_en: Resume, resume_zh: Resume, settings: Settings, tmp_path: Path
) -> None:
    """xeCJK classifies — and – as CJK punctuation, and CJK punctuation loses its leading
    space. The English resume rendered "Cathay Financial Holdings —DDT AI" and
    "Feb 2026 –Present" while the HTML render of the same data was correctly spaced.

    Nothing at the source level can see this: the .tex file contains the space, and so does
    the model. It only appears once the page is typeset. Found by rasterizing the compiled
    PDF from the Docker image and looking at it.

    Only " — " sequences are checked. A numeric range writes the dash tight on purpose
    ("5–15 second"), so asserting on every dash in the document fails on correct output —
    which it did on the first run of this test.
    """
    for resume, name in ((resume_en, "en"), (resume_zh, "zh")):
        directory = tmp_path / name
        directory.mkdir()
        rendered = " ".join(_pdftotext(compile_to(resume, settings, directory)).split())

        spaced = [
            field
            for field in _dash_bearing_fields(resume)
            if any(f" {dash}" in field for dash in ("—", "–"))
        ]
        assert spaced, "fixture has no spaced dash to check — this test would be vacuous"

        for field in spaced:
            assert " ".join(field.split()) in rendered, (
                f"{name}: {field!r} was respaced during typesetting — "
                "check the xeCJKDeclareCharClass line in the template"
            )


def _dash_bearing_fields(resume: Resume) -> list[str]:
    """Every string the template actually prints that could carry a spaced dash.

    `Project.affiliation` is deliberately absent: the template prints
    `context or fmt_month(date)`, never affiliation, so asserting on it fails on a correct
    PDF. Checked against the template rather than assumed from the model.
    """
    from pipeline.render.labels import fmt_month, fmt_range

    fields = []
    for item in resume.experiences:
        fields += [item.organization, item.role, fmt_range(item.start, item.end, resume.lang)]
    for project in resume.projects:
        fields += [project.name, project.context or fmt_month(project.date_, resume.lang)]
    return [field for field in fields if field]


@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="poppler-utils not installed")
def test_latex_and_html_agree_on_spacing(
    resume_en: Resume, settings: Settings, tmp_path: Path
) -> None:
    """The two engines must render the same data the same way.

    Asserted on a whole field rather than a character class, because that is the property
    that actually matters: an organization name reads identically in both PDFs. This is the
    check that would have caught the dash bug without knowing dashes were involved.
    """
    from pipeline.render import html as html_renderer

    latex_text = " ".join(_pdftotext(compile_to(resume_en, settings, tmp_path)).split())
    html_source = html_renderer.render_html(resume_en, settings)

    for item in resume_en.experiences:
        if "—" not in item.organization and "–" not in item.organization:
            continue
        assert item.organization in html_source, f"HTML lost {item.organization!r}"
        assert item.organization in latex_text, f"LaTeX respaced {item.organization!r}"


def _pdftotext(pdf: Path) -> str:
    result = subprocess.run(  # noqa: S603 - fixed binary, no shell
        ["pdftotext", "-layout", str(pdf), "-"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=True,
    )
    return result.stdout
