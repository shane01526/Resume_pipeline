"""Stable download links: /resume/en.pdf, /resume/zh.docx, and so on.

These always point at the latest approved artifact, so the URL can go in an email
signature or a GitHub profile and never needs updating.

Files are served from the container's `output/` directory — the same working copy the
publish stage commits to. Serving from disk rather than proxying GitHub keeps the path
short and avoids depending on raw.githubusercontent's cache behaviour; if the container
is fresh and `output/` is empty, the endpoint says so rather than 404ing blankly.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from pipeline.config import get_settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/resume", tags=["downloads"])

# Extension → (filename in output/<lang>/, MIME type).
FORMATS = {
    "pdf": ("resume.pdf", "application/pdf"),
    "latex.pdf": ("resume.latex.pdf", "application/pdf"),
    "docx": (
        "resume.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    # The LaTeX source, for editing in Overleaf. `application/x-tex` rather than
    # `text/plain` so a browser downloads it instead of rendering it as a wall of text.
    "tex": ("resume.tex", "application/x-tex"),
}

LANGS = {"en", "zh"}


@router.get("/{filename:path}")
async def download(filename: str) -> FileResponse:
    """Serve the current approved artifact, e.g. `en.pdf` or `zh.latex.pdf`.

    The whole name is captured and split here rather than matched as `{lang}.{fmt}`.
    That pattern is greedy on the first segment, so `zh.latex.pdf` bound `lang="zh.latex"`
    and 404'd — the two-dot format is the reason this endpoint needs manual parsing.
    """
    lang, _, fmt = filename.partition(".")
    if lang not in LANGS:
        raise HTTPException(404, f"unknown language {lang!r}; expected one of {sorted(LANGS)}")
    if fmt not in FORMATS:
        raise HTTPException(404, f"unknown format {fmt!r}; expected one of {sorted(FORMATS)}")

    settings = get_settings()
    filename, media_type = FORMATS[fmt]
    path = settings.output_dir / lang / filename

    if not path.is_file():
        raise HTTPException(
            404,
            f"no published {lang}.{fmt} yet. Approve a run first — "
            f"{settings.public_base_url}/ lists anything pending.",
        )

    # A recruiter-facing filename, not "resume.pdf".
    download_name = _download_name(settings.output_dir, lang, fmt)
    return FileResponse(
        path,
        media_type=media_type,
        filename=download_name,
        # Short cache: the file changes only on approval, but a stale copy in someone's
        # browser after an update is worse than an extra request.
        headers={"Cache-Control": "public, max-age=300"},
    )


def _download_name(output_dir: Path, lang: str, fmt: str) -> str:
    """Build a filename from the published resume's own name field.

    Falls back to a generic name if resume.json is missing or unreadable — a download
    with an odd filename is better than a 500.
    """
    stem = "Resume"
    source = output_dir / ("resume.zh.json" if lang == "zh" else "resume.json")
    if source.is_file():
        try:
            import json

            name = json.loads(source.read_text(encoding="utf-8"))["profile"]["name"]
            # "WU, YU-HSUAN" → "WU_YU-HSUAN"
            stem = name.replace(",", "").replace(" ", "_").strip("_")
        except (OSError, ValueError, KeyError) as exc:
            log.warning("could not read name from %s: %s", source.name, exc)

    suffix = {"pdf": "pdf", "latex.pdf": "latex.pdf", "docx": "docx", "tex": "tex"}[fmt]
    return f"{stem}_Resume_{lang.upper()}.{suffix}"
