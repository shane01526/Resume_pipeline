"""Run orchestration: stages 1-8, ending at "waiting for your approval".

The single most important property: **a run never publishes by itself.** It renders,
diffs, notifies, and stops. Publishing happens only from `pipeline.publish`, called when
you approve. That is why the schedule can be aggressive without being risky.

Stage order and the reason for it:

    1-3  ingest → extract → reconcile   write candidates to Notion for review
    4    read Notion                    Approved rows only → resume.json
    5    translate                      → resume.zh.json
    6    render                          six artifacts
    7    diff                            vs the last approved snapshot
    8    notify                          Slack, but only if something changed

Stages 1-3 come first so newly-extracted rows *could* be included, but they only ever
create `Pending Review` rows — so their output cannot reach this run's resume. That is
intentional: nothing enters your resume in the same run that discovered it.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pipeline.config import Settings, get_settings
from pipeline.models import Resume
from pipeline.state import Run, RunStatus, RunStore

log = logging.getLogger(__name__)


async def execute_run(run_id: str, store: RunStore | None = None) -> None:
    """Run the pipeline for `run_id`. Never raises — failures are recorded on the run.

    `store` should be the one that created the run. Passing it is not an optimisation: with
    the GitHub backend, re-reading a just-created run through the Contents API returns 404,
    because that API is only eventually consistent for read-after-write. Every run died
    immediately with "execute_run called for unknown run" while the record was sitting in
    the repository, written and committed. Reusing the caller's store also reuses its cache,
    which already holds the record from the write.

    The `None` default keeps the by-id entry point usable (a retry, a shell, a future queue
    worker), where a fresh read is both necessary and safe — by then the write has settled.
    """
    settings = get_settings()
    store = store or RunStore(settings)

    run = store.load(run_id)
    if run is None:
        log.error("execute_run called for unknown run %s", run_id)
        return

    try:
        await _execute(run, store, settings)
    except Exception as exc:  # noqa: BLE001 - a background task must not die silently
        log.exception("run %s failed", run_id)
        run.status = RunStatus.FAILED
        run.error = f"{type(exc).__name__}: {exc}"[:1000]
        store.save(run)
        store.discard(run_id)
        await _notify_failure(run, settings)


async def _execute(run: Run, store: RunStore, settings: Settings) -> None:
    from pipeline.diff import compute_diff
    from pipeline.notion_client import NotionReader
    from pipeline.translate import translate_resume

    artifacts_dir = store.artifacts_dir(run.id)

    # --- Stages 1-3: harvest new material into Notion's review queue -------
    # Best-effort: a malformed source file must not block a resume rebuild from rows you
    # have already approved.
    try:
        from pipeline.ingest import ingest_sources, mark_processed
        from pipeline.reconcile import reconcile_candidates

        if changed := ingest_sources(store, settings):
            log.info("ingest found %d new or changed source file(s)", len(changed))
            outcome = await reconcile_candidates(changed, settings)
            # Marked only after reconcile returns, so a crash mid-extraction leaves the
            # file pending for the next run rather than silently skipped forever.
            # Sources whose extraction failed are excluded, for the same reason.
            succeeded = [s for s in changed if s.name not in outcome.skipped]
            mark_processed(succeeded, store, settings)
            if outcome.created or outcome.commented:
                await _notify_review_queue(outcome, settings)
        else:
            log.info("no new source files")
    except Exception as exc:  # noqa: BLE001
        log.warning("source ingestion skipped: %s", exc)

    # --- Stage 4: read the approved rows ------------------------------------
    async with NotionReader(settings) as reader:
        resume_en = await reader.read_resume()
        log.info(
            "read %d experience(s), %d project(s), %d skill(s) from Notion",
            len(resume_en.experiences),
            len(resume_en.projects),
            len(resume_en.skills),
        )
        # --- Stage 5: translate (reader carries the human overrides) --------
        resume_zh = await translate_resume(resume_en, reader, settings)

    _write_json(artifacts_dir / "resume.json", resume_en)
    _write_json(artifacts_dir / "resume.zh.json", resume_zh)

    # --- Stage 7 (before rendering): is there anything to review? -----------
    # Diffing first means an unchanged resume costs no render time at all.
    previous = store.load_approved_snapshot()
    diff = compute_diff(previous, resume_en)
    run.counts = diff.counts
    store.save(run)

    if diff.counts.total == 0 and previous is not None:
        # No diff, no notification. A weekly "nothing changed" ping trains you to ignore
        # the channel, which defeats the purpose of the notification.
        log.info("no changes since the last approved resume")
        run.status = RunStatus.NO_CHANGE
        store.save(run)
        store.discard(run.id)
        return

    # --- Stage 6: render ----------------------------------------------------
    written = await _render_all(resume_en, resume_zh, artifacts_dir, settings)
    run.artifacts = [str(path.relative_to(store.run_dir(run.id))) for path in written]

    # --- Stage 7 (rest): page images for the visual comparison --------------
    _rasterize(written, store, run, settings)
    # Through the store, not a path: the diff page may be served by a different instance
    # than the one that rendered this run.
    store.save_diff(run.id, diff.model_dump(mode="json"))

    run.status = RunStatus.PENDING_APPROVAL
    store.save(run)

    # --- Stage 8: notify ----------------------------------------------------
    from pipeline.notify_slack import notify_pending

    await notify_pending(run, diff, settings)
    log.info("run %s awaiting approval — %s", run.id, diff.counts.summary())


async def _render_all(
    resume_en: Resume, resume_zh: Resume, out: Path, settings: Settings
) -> list[Path]:
    """Render every configured engine. Returns the artifacts written."""
    from pipeline.render import docx as docx_renderer
    from pipeline.render import html as html_renderer
    from pipeline.render import latex as latex_renderer

    written: list[Path] = []

    if "html" in settings.renderers:
        written += await html_renderer.render_pdfs(
            [(resume_en, out / "en" / "resume.pdf"), (resume_zh, out / "zh" / "resume.pdf")],
            settings,
        )

    if "latex" in settings.renderers:
        if latex_renderer.tectonic_available(settings):
            for resume, path in (
                (resume_en, out / "en" / "resume.latex.pdf"),
                (resume_zh, out / "zh" / "resume.latex.pdf"),
            ):
                written.append(latex_renderer.render_pdf(resume, settings, path))
        else:
            # Degrade rather than fail: five artifacts and a warning beat no review at all.
            log.warning("tectonic unavailable — LaTeX artifacts skipped")

    if "docx" in settings.renderers:
        for resume, path in (
            (resume_en, out / "en" / "resume.docx"),
            (resume_zh, out / "zh" / "resume.docx"),
        ):
            written.append(docx_renderer.render_docx(resume, settings, path))

    return written


def _rasterize(written: list[Path], store: RunStore, run: Run, settings: Settings) -> None:
    """Page images for the diff page. Non-fatal: the content diff still works without them."""
    from pipeline.render.pages import PageRenderError, pdf_to_pngs, pdftoppm_available

    if not pdftoppm_available():
        log.warning("pdftoppm unavailable — the diff page will show no page images")
        return

    for pdf in (p for p in written if p.suffix == ".pdf"):
        try:
            pdf_to_pngs(
                pdf,
                store.pages_dir(run.id) / pdf.parent.name,
                settings,
                prefix=pdf.stem,
            )
        except PageRenderError as exc:
            log.warning("could not rasterize %s: %s", pdf.name, exc)


def _write_json(path: Path, resume: Resume) -> None:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            resume.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


async def _notify_failure(run: Run, settings: Settings) -> None:
    from web.slack import post_message

    await post_message(
        settings,
        f"⚠️ 履歷更新失敗 `{run.id}`\n```{run.error}```",
    )


async def _notify_review_queue(outcome: object, settings: Settings) -> None:
    """Tell you that new candidates are waiting in Notion.

    Separate from the approval notification because it needs a different action from you:
    these rows will NOT appear in this run's resume — they are `Pending Review` with
    `Include in Resume` unchecked, and only reach a resume once you approve them in Notion.
    Folding this into the approval message would blur that distinction.
    """
    from web.slack import post_message

    created = getattr(outcome, "created", [])
    commented = getattr(outcome, "commented", [])

    parts = []
    if created:
        listed = "\n".join(f"• {item}" for item in created[:8])
        more = f"\n_…另有 {len(created) - 8} 筆_" if len(created) > 8 else ""
        parts.append(f"*新增待審項目*（{len(created)} 筆）\n{listed}{more}")
    if commented:
        parts.append(
            f"*已核准項目的建議*（{len(commented)} 筆）\n"
            + "\n".join(f"• {label}" for label in commented[:8])
            + "\n_這些只是 Notion 留言，你已核准的內容沒有被改動。_"
        )

    await post_message(
        settings,
        "📥 從 sources/ 抽取到新素材\n\n"
        + "\n\n".join(parts)
        + f"\n\n到 Notion 確認後勾選 `Include in Resume` 並改為 `Approved`，"
        f"下次執行才會進入履歷。\n{settings.notion_master_page_id}",
    )
