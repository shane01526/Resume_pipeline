"""LaTeX → PDF renderer (Tectonic).

Tectonic rather than a full TeX Live install: `texlive-xetex` plus `latex-extra` pushes
the Docker image past 2.5 GB and adds 15 minutes to every build. Tectonic is a single
~50 MB binary on the XeTeX engine that downloads only the packages a document actually
uses, and it supports `xeCJK`, which is the requirement that rules out pdfLaTeX.

The geometry tokens below mirror `templates/styles/print.css`. They are duplicated
rather than parsed out of the CSS because the two engines express the same measurement
in different units, and a regex over a stylesheet is a worse failure mode than a
constant that a test compares.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from pipeline.config import Settings
from pipeline.models import Resume
from pipeline.render.html import RenderError
from pipeline.render.labels import (
    fmt_degree,
    fmt_education_date,
    fmt_month,
    fmt_range,
    fmt_skill,
    labels_for,
)

log = logging.getLogger(__name__)

TEMPLATE_NAME = "resume.tex.j2"

# --- Design tokens, mirroring print.css -------------------------------------
GEOMETRY = {"margin_x": "14mm", "margin_y": "12mm"}

SIZES = {
    "name": "17pt",
    "name_leading": "20pt",
    "contact": "9pt",
    "contact_leading": "11pt",
    "section": "10.5pt",
    "section_leading": "13pt",
    "item_title": "10.5pt",
    "item_title_leading": "13pt",
    "item_meta": "9.5pt",
    "item_meta_leading": "12pt",
    "body": "9.5pt",
    "body_leading": "13pt",
}

# LaTeX has no line-height multiplier, so CSS's --leading-body-zh (1.5 vs 1.36) becomes
# an absolute leading here: 9.5pt * 1.5 ≈ 14.3pt.
SIZES_ZH = {**SIZES, "body_leading": "14.3pt"}

SPACING = {
    "section": "4.2mm",
    "item": "2.6mm",
    "bullet": "0.7mm",
    "bullet_top": "1mm",
    "bullet_indent": "3.4mm",
    "after_name": "1.2mm",
}

RULES = {"weight": "0.5pt", "gap": "1.1mm"}

FONTS = {
    "serif": "Noto Serif",
    "sans": "Noto Sans",
    "cjk_serif": "Noto Serif CJK TC",
    "cjk_sans": "Noto Sans CJK TC",
}

# --- TeX escaping -----------------------------------------------------------
# Replacements must be applied in ONE pass. Sequential str.replace() calls corrupt each
# other: `\` → `\textbackslash{}` inserts braces that a later `{` → `\{` rule then
# escapes, yielding `\textbackslash\{\}`.
_TEX_REPLACEMENTS = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}

_TEX_SPECIALS = re.compile("|".join(re.escape(char) for char in _TEX_REPLACEMENTS))

# Note on non-ASCII punctuation (— – → 、（）～): these are deliberately NOT escaped.
# Under XeTeX with fontspec/xeCJK they render natively from the font, whereas LaTeX's
# ASCII fallbacks are wrong — `--` for an en dash inside a date range, for instance.


def tex_escape(value: str | None) -> str:
    """Escape a string for LaTeX body text.

    Every model field goes through this. Without it a single `&` in an organization name
    (`AI & Data`) or a `_` in a repo path aborts the compile.
    """
    if not value:
        return ""
    return _TEX_SPECIALS.sub(lambda match: _TEX_REPLACEMENTS[match.group()], value)


def _environment(templates_dir: Path) -> Environment:
    """Jinja with LaTeX-safe delimiters.

    Default `{{ }}` and `{% %}` collide with LaTeX's own braces, so statements become
    `\\BLOCK{}`, expressions `\\VAR{}`, comments `\\#{}`.
    """
    return Environment(
        loader=FileSystemLoader(templates_dir),
        undefined=StrictUndefined,
        block_start_string=r"\BLOCK{",
        block_end_string="}",
        variable_start_string=r"\VAR{",
        variable_end_string="}",
        comment_start_string=r"\#{",
        comment_end_string="}",
        # autoescape is HTML-specific and would emit &amp; into TeX; tex_escape is the
        # escaping mechanism here and every field is passed through it explicitly.
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_tex(resume: Resume, settings: Settings) -> str:
    """Render the resume to a LaTeX source string."""
    templates_dir = settings.repo_root / "templates"
    env = _environment(templates_dir)
    lang = resume.lang
    is_zh = lang == "zh"

    def fmt_skill_tex(skill: object) -> str:
        # Escape after formatting, so the parentheses fmt_skill adds are literal text.
        return tex_escape(fmt_skill(skill, lang))  # type: ignore[arg-type]

    # Registered as filters, not just globals: Jinja's `map` resolves its argument as a
    # filter name, so `map(tex)` fails unless `tex` is in env.filters.
    env.filters["tex"] = tex_escape
    env.filters["fmt_skill_tex"] = fmt_skill_tex

    template = env.get_template(TEMPLATE_NAME)
    return template.render(
        resume=resume,
        labels=labels_for(lang),
        geometry=GEOMETRY,
        sizes=SIZES_ZH if is_zh else SIZES,
        spacing=SPACING,
        rules=RULES,
        fonts=FONTS,
        # Upright rather than italic for Chinese: synthesised obliques on CJK faces
        # look broken, matching the `font-style: normal` rule in print.css.
        emph_open="" if is_zh else r"\itshape ",
        emph_close="",
        # Separators are passed in pre-escaped: a `join` argument inside a \VAR{} tag
        # cannot contain a closing brace, which rules out inlining a tex() call.
        separators={"contact": r" \textperiodcentered\ "},
        skill_join=tex_escape(labels_for(lang).skill_join),
        tex=tex_escape,
        fmt_skill_tex=fmt_skill_tex,
        fmt_range=lambda start, end: fmt_range(start, end, lang),
        fmt_month=lambda value: fmt_month(value, lang),
        fmt_education_date=lambda item: fmt_education_date(item, lang),
        fmt_degree=lambda item: fmt_degree(item, lang),
    )


def tectonic_available(settings: Settings) -> bool:
    return shutil.which(settings.tectonic_bin) is not None


def render_pdf(resume: Resume, settings: Settings, output: Path) -> Path:
    """Compile the resume to PDF via Tectonic. Returns the written path."""
    if not tectonic_available(settings):
        raise RenderError(
            f"{settings.tectonic_bin!r} not found on PATH. "
            "Install from https://tectonic-typesetting.github.io or drop 'latex' from RENDERERS."
        )

    source = render_tex(resume, settings)
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="resume-tex-") as tmp:
        tmp_dir = Path(tmp)
        tex_path = tmp_dir / "resume.tex"
        tex_path.write_text(source, encoding="utf-8")

        result = subprocess.run(  # noqa: S603 - fixed binary, no shell
            [
                settings.tectonic_bin,
                "--chatter",
                "minimal",
                # Keep going on warnings; a missing glyph warning must not fail the run,
                # but a genuine error still returns non-zero.
                "--keep-logs",
                "--outdir",
                str(tmp_dir),
                str(tex_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            cwd=tmp_dir,
        )

        produced = tmp_dir / "resume.pdf"
        if result.returncode != 0 or not produced.is_file():
            raise RenderError(
                f"tectonic failed (exit {result.returncode}):\n{_tex_error_summary(result.stderr, tmp_dir)}"
            )

        shutil.copyfile(produced, output)

    log.info("rendered %s (%.1f KB)", output.name, output.stat().st_size / 1024)
    return output


def _tex_error_summary(stderr: str, tmp_dir: Path) -> str:
    """Pull the useful lines out of TeX's output.

    A LaTeX log is thousands of lines; the `!`-prefixed error lines and their context
    are what identify the problem. Falling back to raw stderr keeps unexpected failures
    (a missing font, a network error fetching a package) visible.
    """
    log_path = tmp_dir / "resume.log"
    if log_path.is_file():
        text = log_path.read_text(encoding="utf-8", errors="replace")
        errors = re.findall(r"^!.*(?:\n.*){0,3}", text, flags=re.MULTILINE)
        if errors:
            return "\n".join(errors[:5])[:2000]
    return (stderr or "no stderr").strip()[:2000]
