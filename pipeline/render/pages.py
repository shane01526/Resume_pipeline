"""PDF → page PNGs, for the visual half of the diff page.

Two backends:

- `pdftoppm` (poppler-utils) is the primary. It rasterizes the actual PDF, so what you
  compare is what a recruiter opens. This is what runs on Render.
- Playwright screenshots the HTML instead. This is a *fallback for local development on
  Windows*, where poppler isn't installed by default. It re-renders from source rather
  than from the PDF, so it cannot catch a PDF-generation bug — never rely on it in CI.

The distinction matters: a page-break regression is visible in the pdftoppm output and
invisible in the Playwright fallback, because the fallback produces one tall image with
no page boundaries at all.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from pipeline.config import Settings
from pipeline.models import Resume

log = logging.getLogger(__name__)

PAGE_STEM = "page"


class PageRenderError(RuntimeError):
    pass


def pdftoppm_available() -> bool:
    return shutil.which("pdftoppm") is not None


def pdf_to_pngs(pdf: Path, out_dir: Path, settings: Settings, *, prefix: str = PAGE_STEM) -> list[Path]:
    """Rasterize every page of `pdf` into `out_dir` as `<prefix>-01.png`, ....

    Returns the written paths in page order. Raises if poppler is unavailable — callers
    that want the degraded local path should check `pdftoppm_available()` first.
    """
    if not pdftoppm_available():
        raise PageRenderError(
            "pdftoppm not found. Install poppler-utils (the Docker image includes it), "
            "or use html_to_png() for a local approximation."
        )
    if not pdf.is_file():
        raise PageRenderError(f"no such pdf: {pdf}")

    out_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(  # noqa: S603 - fixed binary, no shell
        [
            "pdftoppm",
            "-png",
            "-r",
            str(settings.pdftoppm_dpi),
            # Zero-padded page numbers so lexical sort equals page order past page 9.
            "-sep",
            "-",
            str(pdf),
            str(out_dir / prefix),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if result.returncode != 0:
        raise PageRenderError(f"pdftoppm failed (exit {result.returncode}): {result.stderr[:500]}")

    pages = sorted(out_dir.glob(f"{prefix}-*.png"))
    if not pages:
        raise PageRenderError(f"pdftoppm wrote no pages for {pdf.name}")
    log.info("rasterized %s into %d page(s)", pdf.name, len(pages))
    return pages


async def html_to_png(resume: Resume, settings: Settings, output: Path) -> Path:
    """Screenshot the resume HTML at A4 width. Local-development fallback only.

    Produces ONE tall image with no page breaks, so it cannot show pagination problems.
    Use `pdf_to_pngs` wherever poppler is available.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover
        raise PageRenderError("playwright is not installed") from exc

    from pipeline.render.html import render_html

    html = render_html(resume, settings)
    output.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(args=["--no-sandbox"])
        try:
            # 794px ≈ 210mm at 96dpi, so the screenshot matches the print column width.
            page = await browser.new_page(viewport={"width": 794, "height": 1123})
            await page.set_content(html, wait_until="load")
            await page.evaluate("document.fonts.ready")
            # Print CSS is what the PDF uses; without this the @media screen block
            # applies and the shadow/margins make the screenshot misleading.
            await page.emulate_media(media="print")
            await page.screenshot(path=str(output), full_page=True)
        finally:
            await browser.close()

    log.info("screenshotted %s (%.1f KB)", output.name, output.stat().st_size / 1024)
    return output
