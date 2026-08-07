"""Stage 1: incremental detection and text extraction."""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.config import Settings
from pipeline.ingest import MAX_CHARS, ingest_sources, mark_processed
from pipeline.state import RunStore


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[Settings, RunStore]:
    """Settings rooted at a temp directory, so tests never touch the real repo.

    Passing `repo_root` explicitly rather than monkeypatching: an earlier version patched
    the property and had no effect, because get_settings() is cached — so the tests wrote
    into the production sources/ directory and overwrote its README. `repo_root` is a real
    field now precisely so this is impossible.
    """
    (tmp_path / "sources").mkdir()
    (tmp_path / "state").mkdir()

    settings = Settings(repo_root=tmp_path)
    return settings, RunStore(settings)


def write(settings: Settings, name: str, content: str = "Some project content.") -> Path:
    path = settings.sources_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# --- incremental detection ---------------------------------------------------


def test_new_file_is_found(workspace: tuple[Settings, RunStore]) -> None:
    settings, store = workspace
    write(settings, "prd.md")
    sources = ingest_sources(store, settings)
    assert [s.name for s in sources] == ["prd.md"]
    assert sources[0].text == "Some project content."


def test_processed_file_is_skipped(workspace: tuple[Settings, RunStore]) -> None:
    settings, store = workspace
    write(settings, "prd.md")
    sources = ingest_sources(store, settings)
    mark_processed(sources, store, settings)
    assert ingest_sources(store, settings) == []


def test_changed_file_is_reprocessed(workspace: tuple[Settings, RunStore]) -> None:
    settings, store = workspace
    write(settings, "prd.md", "v1")
    mark_processed(ingest_sources(store, settings), store, settings)

    write(settings, "prd.md", "v2 with new results")
    sources = ingest_sources(store, settings)
    assert len(sources) == 1
    assert sources[0].text == "v2 with new results"


def test_detection_is_by_content_not_mtime(workspace: tuple[Settings, RunStore]) -> None:
    """A git clone rewrites every mtime.

    Keying on mtime would make the first run in a fresh container re-extract the whole
    archive and file duplicate review rows for work already approved.
    """
    settings, store = workspace
    path = write(settings, "prd.md", "stable content")
    mark_processed(ingest_sources(store, settings), store, settings)

    # Same bytes, much later mtime — as a fresh checkout would produce.
    import os

    os.utime(path, (10**9, 10**9))
    assert ingest_sources(store, settings) == []


def test_unprocessed_on_failure(workspace: tuple[Settings, RunStore]) -> None:
    """The index only advances via mark_processed.

    So a crash between extraction and reconcile leaves the file pending rather than
    silently skipped on the next run.
    """
    settings, store = workspace
    write(settings, "prd.md")
    ingest_sources(store, settings)  # discovered, but never marked
    assert len(ingest_sources(store, settings)) == 1


# --- filtering --------------------------------------------------------------


def test_readme_is_not_a_source(workspace: tuple[Settings, RunStore]) -> None:
    """sources/README.md documents the folder; it is not material to extract from."""
    settings, store = workspace
    write(settings, "README.md", "# how to use this folder")
    write(settings, "prd.md")
    assert [s.name for s in ingest_sources(store, settings)] == ["prd.md"]


def test_unsupported_extensions_ignored(workspace: tuple[Settings, RunStore]) -> None:
    settings, store = workspace
    write(settings, "notes.md")
    write(settings, "screenshot.png", "not really an image")
    write(settings, "data.xlsx", "not really a spreadsheet")
    assert [s.name for s in ingest_sources(store, settings)] == ["notes.md"]


def test_hidden_files_ignored(workspace: tuple[Settings, RunStore]) -> None:
    settings, store = workspace
    write(settings, ".DS_Store", "junk")
    write(settings, ".gitkeep", "")
    assert ingest_sources(store, settings) == []


def test_empty_file_skipped(workspace: tuple[Settings, RunStore]) -> None:
    """Nothing to extract, and an empty prompt would waste a call."""
    settings, store = workspace
    write(settings, "blank.md", "   \n\n  ")
    assert ingest_sources(store, settings) == []


def test_nested_directories_are_searched(workspace: tuple[Settings, RunStore]) -> None:
    """You'll organize sources into folders; the walk has to follow."""
    settings, store = workspace
    write(settings, "2026/cathay/prd.md")
    assert [s.name for s in ingest_sources(store, settings)] == ["prd.md"]


def test_oversized_text_is_truncated(workspace: tuple[Settings, RunStore]) -> None:
    """One mis-saved file must not consume a run's whole token budget."""
    settings, store = workspace
    write(settings, "huge.md", "x" * (MAX_CHARS + 5000))
    sources = ingest_sources(store, settings)
    assert len(sources[0].text or "") == MAX_CHARS


# --- format handling --------------------------------------------------------


def test_pdf_is_passed_as_bytes(workspace: tuple[Settings, RunStore]) -> None:
    """PDFs go to the model natively: layout is where a report's structure lives, and
    Claude reads it better than a local text extractor."""
    settings, store = workspace
    (settings.sources_dir / "paper.pdf").write_bytes(b"%PDF-1.4 fake content")
    sources = ingest_sources(store, settings)
    assert sources[0].is_pdf
    assert sources[0].text is None
    assert sources[0].pdf_bytes is not None


def test_oversized_pdf_is_skipped(workspace: tuple[Settings, RunStore]) -> None:
    """Anthropic caps a request at 32MB; a too-large PDF would fail the call."""
    settings, store = workspace
    from pipeline.ingest import MAX_PDF_BYTES

    (settings.sources_dir / "huge.pdf").write_bytes(b"%PDF" + b"x" * MAX_PDF_BYTES)
    assert ingest_sources(store, settings) == []


def test_docx_text_includes_table_cells(workspace: tuple[Settings, RunStore]) -> None:
    """A report's deliverables are as likely to be in a table as in prose."""
    from docx import Document

    settings, store = workspace
    document = Document()
    document.add_paragraph("Project summary paragraph.")
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Deliverable"
    table.rows[0].cells[1].text = "Shipped the BRD agent"
    document.save(str(settings.sources_dir / "report.docx"))

    text = ingest_sources(store, settings)[0].text or ""
    assert "Project summary paragraph." in text
    assert "Shipped the BRD agent" in text


def test_pptx_text_labels_slides(workspace: tuple[Settings, RunStore]) -> None:
    """Slide numbers give the model structure: an agenda slide reads differently from
    a results slide."""
    from pptx import Presentation
    from pptx.util import Inches

    settings, store = workspace
    presentation = Presentation()
    for content in ("Agenda", "Results: 40% faster"):
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1))
        box.text_frame.text = content
    presentation.save(str(settings.sources_dir / "deck.pptx"))

    text = ingest_sources(store, settings)[0].text or ""
    assert "--- Slide 1 ---" in text
    assert "--- Slide 2 ---" in text
    assert "Results: 40% faster" in text


def test_unreadable_file_does_not_fail_the_run(workspace: tuple[Settings, RunStore]) -> None:
    """A corrupt .docx must not stop a resume rebuild from already-approved rows."""
    settings, store = workspace
    (settings.sources_dir / "corrupt.docx").write_bytes(b"this is not a zip archive")
    write(settings, "good.md")
    assert [s.name for s in ingest_sources(store, settings)] == ["good.md"]


def test_missing_sources_directory_is_not_an_error(tmp_path: Path) -> None:
    settings = Settings(repo_root=tmp_path)
    assert ingest_sources(RunStore(settings), settings) == []
