"""Local end-to-end dry run, for iterating on templates without touching Notion.

    python scripts/local_run.py --render-only        # fixture -> all six artifacts
    python scripts/local_run.py --render-only --png  # also rasterize page images
    python scripts/local_run.py                      # read Notion, then render

`--render-only` uses `tests/fixtures/resume.sample.json` plus a machine-translated
Chinese counterpart, so it needs no credentials. Output lands in `scratch/`, which is
gitignored — published artifacts only ever come from an approved run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

# Allow `python scripts/local_run.py` from the repo root without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.config import get_settings  # noqa: E402
from pipeline.models import Resume  # noqa: E402
from pipeline.render import docx as docx_renderer  # noqa: E402
from pipeline.render import html as html_renderer  # noqa: E402
from pipeline.render import latex as latex_renderer  # noqa: E402
from pipeline.render.pages import pdf_to_pngs, pdftoppm_available  # noqa: E402

log = logging.getLogger("local_run")

FIXTURE_EN = Path("tests/fixtures/resume.sample.json")
FIXTURE_ZH = Path("tests/fixtures/resume.sample.zh.json")


def load_fixtures() -> tuple[Resume, Resume]:
    """The English fixture plus its hand-written Chinese counterpart."""
    en = Resume.model_validate_json(FIXTURE_EN.read_text(encoding="utf-8"))
    if FIXTURE_ZH.is_file():
        zh = Resume.model_validate_json(FIXTURE_ZH.read_text(encoding="utf-8"))
    else:
        # No Chinese fixture yet: render English content under the Chinese layout so the
        # CJK CSS path is still exercised.
        data = en.model_dump(mode="json", by_alias=True)
        data["lang"] = "zh"
        zh = Resume.model_validate(data)
    return en, zh


async def read_from_notion() -> tuple[Resume, Resume]:
    from pipeline.notion_client import NotionReader
    from pipeline.translate import translate_resume

    settings = get_settings()
    async with NotionReader(settings) as reader:
        en = await reader.read_resume()
        zh = await translate_resume(en, reader, settings)
    return en, zh


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--render-only",
        action="store_true",
        help="skip Notion and use the checked-in fixtures (no credentials needed)",
    )
    parser.add_argument("--png", action="store_true", help="also rasterize page images")
    parser.add_argument(
        "--out", type=Path, default=Path("scratch"), help="output directory (default: scratch/)"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
    settings = get_settings()

    if args.render_only:
        en, zh = load_fixtures()
        log.info("using fixtures (no Notion read)")
    else:
        en, zh = await read_from_notion()
        log.info("read %d experiences from Notion", len(en.experiences))

    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    (out / "resume.json").write_text(
        json.dumps(en.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out / "resume.zh.json").write_text(
        json.dumps(zh.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    written: list[Path] = []

    if "html" in settings.renderers:
        written += await html_renderer.render_pdfs(
            [(en, out / "en" / "resume.pdf"), (zh, out / "zh" / "resume.pdf")], settings
        )

    if "latex" in settings.renderers:
        if latex_renderer.tectonic_available(settings):
            for resume, path in (
                (en, out / "en" / "resume.latex.pdf"),
                (zh, out / "zh" / "resume.latex.pdf"),
            ):
                written.append(latex_renderer.render_pdf(resume, settings, path))
        else:
            log.warning("tectonic not on PATH — skipping the LaTeX renderer")

    if "docx" in settings.renderers:
        for resume, path in ((en, out / "en" / "resume.docx"), (zh, out / "zh" / "resume.docx")):
            written.append(docx_renderer.render_docx(resume, settings, path))

    if args.png:
        if pdftoppm_available():
            for pdf in (p for p in written if p.suffix == ".pdf"):
                pages_dir = out / "pages" / pdf.parent.name
                pdf_to_pngs(pdf, pages_dir, settings, prefix=pdf.stem)
        else:
            log.warning(
                "pdftoppm not found — page images skipped. "
                "The Docker image ships poppler-utils; install it locally for real page diffs."
            )

    print()
    for path in written:
        print(f"  {path.relative_to(out)}  {path.stat().st_size / 1024:>7.1f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
