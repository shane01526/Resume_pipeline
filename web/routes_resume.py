"""Stable download links: /resume/en.pdf, /resume/zh.docx, and so on.

These always point at the latest approved artifact, so the URL can go in an email
signature or a GitHub profile and never needs updating.

Files are served from the container's `output/` directory, falling back to durable
storage when it is not there. The fallback is not belt-and-braces: on Cloud Run the
Dockerfile creates `output/en` and `output/zh` *empty* and never copies published files
into the image, so only the instance that happened to run the publish has them. Every
link 404s after that instance recycles — or after any redeploy — until the next publish.
Measured, not theoretical: a redeploy turned four working links into 404s.

The repo is the durable copy (see `pipeline/state.py` on git-as-database), so a miss is
resolved by reading `output/...` through the storage backend and caching it on disk for
subsequent requests. Which is also why the `.tex` published by hand works immediately.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from pipeline.config import Settings, get_settings
from pipeline.storage import StorageError, build_storage

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

    if not path.is_file() and not _restore(settings, f"output/{lang}/{filename}", path):
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


def _restore(settings: Settings, key: str, path: Path) -> bool:
    """Fetch one published file from durable storage onto disk. Returns whether it is there.

    Best-effort by design: a storage failure should read as "not published yet" — the
    message the caller already produces — rather than a 500 on a link that may be sitting
    in someone's email signature. With the local backend this is a no-op in effect, since
    the durable copy *is* the file just checked.
    """
    storage = None
    try:
        storage = build_storage(settings)
        data = storage.read(key)
        if data is None:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        log.info("restored %s from storage (%.1f KB)", key, len(data) / 1024)
        return True
    except (StorageError, OSError) as exc:
        log.warning("could not restore %s from storage: %s", key, exc)
        return False
    finally:
        if close := getattr(storage, "close", None):
            close()


def _download_name(output_dir: Path, lang: str, fmt: str) -> str:
    """Build a filename from the published resume's own name field.

    Falls back to a generic name if resume.json is missing or unreadable — a download
    with an odd filename is better than a 500.
    """
    stem = "Resume"
    source = output_dir / ("resume.zh.json" if lang == "zh" else "resume.json")
    if not source.is_file():
        # Same reason as the artifact itself: a fresh container has no output/ contents.
        _restore(get_settings(), f"output/{source.name}", source)
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
