"""HTML → PDF renderer (Playwright + Chromium).

This is the primary output: the print CSS in `templates/styles/print.css` is the single
source of truth for the layout, and the same rendered HTML is reused by the diff page.

Chromium is launched once per render call rather than per language. Launch is the
expensive part (~1s); a second page in the same browser costs almost nothing.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markupsafe import Markup

from pipeline.config import Settings
from pipeline.models import Resume
from pipeline.render.labels import (
    fmt_degree,
    fmt_education_date,
    fmt_month,
    fmt_range,
    fmt_skill,
    labels_for,
)

log = logging.getLogger(__name__)

TEMPLATE_NAME = "resume.html.j2"
STYLESHEET = Path("styles") / "print.css"


class RenderError(RuntimeError):
    """Rendering failed in a way the run cannot recover from."""


def _environment(templates_dir: Path) -> Environment:
    # StrictUndefined so a renamed model field fails loudly instead of rendering a
    # blank where a job title should be.
    return Environment(
        loader=FileSystemLoader(templates_dir),
        undefined=StrictUndefined,
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_html(resume: Resume, settings: Settings) -> str:
    """Render the resume to a standalone HTML string with the CSS inlined."""
    templates_dir = settings.repo_root / "templates"
    stylesheet_path = templates_dir / STYLESHEET
    if not stylesheet_path.is_file():
        raise RenderError(f"stylesheet missing: {stylesheet_path}")

    env = _environment(templates_dir)
    lang = resume.lang
    # Filters are bound to this resume's language so templates never pass `lang` around.
    env.filters["format_skill"] = lambda skill: fmt_skill(skill, lang)

    template = env.get_template(TEMPLATE_NAME)
    return template.render(
        resume=resume,
        labels=labels_for(lang),
        # Markup, not a plain str: autoescape is on to protect resume *content*, but it
        # would turn every `"` in the stylesheet into `&#34;`, silently breaking font
        # stacks and every `content:` value (bullet markers, the skill-label colon).
        # This file is ours, not user input.
        stylesheet=Markup(stylesheet_path.read_text(encoding="utf-8")),
        fmt_range=lambda start, end: fmt_range(start, end, lang),
        fmt_month=lambda value: fmt_month(value, lang),
        fmt_education_date=lambda item: fmt_education_date(item, lang),
        fmt_degree=lambda item: fmt_degree(item, lang),
    )


async def render_pdf(resume: Resume, settings: Settings, output: Path) -> Path:
    """Render one resume to PDF. Returns the written path."""
    written = await render_pdfs([(resume, output)], settings)
    return written[0]


async def render_pdfs(jobs: list[tuple[Resume, Path]], settings: Settings) -> list[Path]:
    """Render several resumes in one browser launch.

    Each job is a `(resume, output_path)` pair. HTML is written to a temp file and loaded
    via `file://` so Chromium can resolve local font files; passing the markup through
    `set_content` would leave the page with no base URL.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - environment problem, not logic
        raise RenderError(
            "playwright is not installed. Run: pip install playwright && playwright install chromium"
        ) from exc

    written: list[Path] = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(args=["--no-sandbox"])
        try:
            for resume, output in jobs:
                html = render_html(resume, settings)
                output.parent.mkdir(parents=True, exist_ok=True)

                # delete=False because Chromium needs to open the path by name; the
                # finally block removes it.
                with tempfile.NamedTemporaryFile(
                    "w", suffix=".html", delete=False, encoding="utf-8"
                ) as handle:
                    handle.write(html)
                    temp_path = Path(handle.name)

                page = await browser.new_page()
                try:
                    await page.goto(temp_path.as_uri(), wait_until="load")
                    # Webfonts resolve after `load`; without this the first render can
                    # fall back to a default face and shift every line.
                    await page.evaluate("document.fonts.ready")
                    await page.pdf(
                        path=str(output),
                        format="A4",
                        print_background=True,
                        # Margins live in the @page rule so the three renderers share
                        # one definition of the page box.
                        prefer_css_page_size=True,
                    )
                finally:
                    await page.close()
                    temp_path.unlink(missing_ok=True)

                log.info("rendered %s (%.1f KB)", output.name, output.stat().st_size / 1024)
                written.append(output)
        finally:
            await browser.close()

    return written
