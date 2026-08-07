"""Stage 3: the safety property and the matching that supports it.

The property under test: **the pipeline never modifies a row you approved.** A candidate
matching approved content produces a Notion comment, never a PATCH. Everything else here
exists to make that property hold in practice — matching that is loose enough to find the
row, and strict enough not to find the wrong one.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from pipeline.extract import CandidateExperience, CandidateSkill, Extraction
from pipeline.ingest import Source
from pipeline.models import Confidence, Status
from pipeline.reconcile import (
    MATCH_THRESHOLD,
    ExistingRow,
    ReconcileResult,
    _create_experience,
    _file_extraction,
    _known_tags,
    _match_by_label,
    _match_experience,
    _suggest,
    normalize,
    similarity,
)


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class RecordingClient:
    """Records every Notion call so a test can assert on what was NOT sent."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.patches: list[tuple[str, dict[str, Any]]] = []
        self._next_id = 0

    async def post(self, url: str, json: dict[str, Any] | None = None, **_: Any) -> FakeResponse:
        self.posts.append((url, json or {}))
        self._next_id += 1
        return FakeResponse({"id": f"page-{self._next_id}"})

    async def patch(self, url: str, json: dict[str, Any] | None = None, **_: Any) -> FakeResponse:
        self.patches.append((url, json or {}))
        return FakeResponse({"id": "patched"})

    def created_pages(self) -> list[dict[str, Any]]:
        return [body for url, body in self.posts if url == "/pages"]

    def comments(self) -> list[dict[str, Any]]:
        return [body for url, body in self.posts if url == "/comments"]


@pytest.fixture
def source(tmp_path) -> Source:
    path = tmp_path / "2026-08_cathay_brd-agent_prd.md"
    path.write_text("content", encoding="utf-8")
    return Source(path=path, sha256="abc", text="content")


@pytest.fixture
def settings():
    from pipeline.config import get_settings

    return get_settings()


def approved_row(label: str, start: date | None = None) -> ExistingRow:
    return ExistingRow(page_id="approved-1", label=label, status=Status.APPROVED.value, start=start)


# --- the safety property -----------------------------------------------------


async def test_approved_row_gets_a_comment_never_a_patch(source: Source, settings: Any) -> None:
    """The core guarantee. A PATCH here would silently rewrite your reviewed wording."""
    client = RecordingClient()
    existing = {
        "experiences": [approved_row("Cathay Financial Holdings — DDT AI", date(2026, 2, 1))],
        "projects": [],
        "skills": [],
        "publications": [],
    }
    extraction = Extraction(
        experiences=[
            CandidateExperience(
                role="ML Engineer Intern",
                organization="Cathay DDT",  # abbreviated, as a source document would write it
                start="2026-02-01",
                bullets=["Built a new thing worth mentioning."],
            )
        ],
        confidence=Confidence.HIGH,
    )
    result = ReconcileResult()

    await _file_extraction(client, extraction, source, existing, settings, result)

    assert client.patches == [], "an approved row was modified"
    assert client.created_pages() == [], "a duplicate row was created for a matched org"
    assert len(client.comments()) == 1
    assert result.commented == ["Cathay Financial Holdings — DDT AI"]


async def test_comment_targets_the_matched_page(source: Source) -> None:
    client = RecordingClient()
    row = approved_row("Cathay Financial Holdings — DDT AI")
    await _suggest(client, row, ["A suggested bullet."], source, ReconcileResult())

    body = client.comments()[0]
    assert body["parent"]["page_id"] == "approved-1"
    text = body["rich_text"][0]["text"]["content"]
    assert "A suggested bullet." in text
    assert source.name in text
    # The comment must say it changed nothing, so a reader knows it is a proposal.
    assert "不會自動修改" in text


async def test_draft_row_also_only_gets_a_comment(source: Source, settings: Any) -> None:
    """Even a Draft row is left alone — you may be mid-edit."""
    client = RecordingClient()
    existing = {
        "experiences": [ExistingRow(page_id="draft-1", label="Google", status=Status.DRAFT.value)],
        "projects": [],
        "skills": [],
        "publications": [],
    }
    extraction = Extraction(
        experiences=[
            CandidateExperience(role="SWE Intern", organization="Google", bullets=["Did work."])
        ],
        confidence=Confidence.HIGH,
    )
    await _file_extraction(client, extraction, source, existing, settings, ReconcileResult())

    assert client.patches == []
    assert client.created_pages() == []
    assert len(client.comments()) == 1


async def test_empty_bullets_produce_no_comment(source: Source) -> None:
    """A match with nothing to suggest should stay silent, not post an empty comment."""
    client = RecordingClient()
    await _suggest(client, approved_row("X"), [], source, ReconcileResult())
    assert client.comments() == []


# --- new rows carry both gates ----------------------------------------------


async def test_new_row_is_pending_and_excluded(source: Source, settings: Any) -> None:
    """Nothing reaches a resume in the same run that discovered it."""
    client = RecordingClient()
    candidate = CandidateExperience(
        role="SWE Intern",
        organization="Some New Company",
        start="2027-01-01",
        bullets=["Shipped something."],
    )
    extraction = Extraction(confidence=Confidence.MEDIUM, uncertainty="dates were vague")

    await _create_experience(client, candidate, source, extraction, settings)

    props = client.created_pages()[0]["properties"]
    assert props["Status"]["select"]["name"] == Status.PENDING_REVIEW.value
    assert props["Include in Resume"]["checkbox"] is False
    # Provenance, so a claim can be traced back to the document that produced it.
    assert props["Source"]["rich_text"][0]["text"]["content"] == source.name
    assert props["Confidence"]["select"]["name"] == "Medium"


async def test_bullets_go_into_the_page_body(source: Source, settings: Any) -> None:
    """Matching the format the master page documents and the reader expects."""
    client = RecordingClient()
    candidate = CandidateExperience(
        role="R", organization="O", bullets=["First bullet.", "Second bullet."]
    )
    await _create_experience(
        client, candidate, source, Extraction(confidence=Confidence.HIGH), settings
    )

    children = client.created_pages()[0]["children"]
    assert [c["type"] for c in children] == ["bulleted_list_item", "bulleted_list_item"]
    assert children[0]["bulleted_list_item"]["rich_text"][0]["text"]["content"] == "First bullet."


async def test_unknown_skill_category_falls_back(source: Source, settings: Any) -> None:
    """Notion rejects an undefined select option; dropping the skill would be worse."""
    client = RecordingClient()
    extraction = Extraction(
        skills=[CandidateSkill(name="Kubernetes", category="DevOps")],  # not in the schema
        confidence=Confidence.HIGH,
    )
    existing = {"experiences": [], "projects": [], "skills": [], "publications": []}
    await _file_extraction(client, extraction, source, existing, settings, ReconcileResult())

    props = client.created_pages()[0]["properties"]
    assert props["Category"]["select"]["name"] == "Tools"


def test_unknown_tags_are_dropped() -> None:
    """An undefined multi_select option makes Notion reject the whole row."""
    assert _known_tags(["LLM", "Quantum Computing", "aws"]) == ["LLM", "AWS"]


# --- matching ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right"),
    [
        # The case that motivates the containment bonus: a PRD's shorthand vs Notion's
        # full name. SequenceMatcher alone scores this around 0.5.
        ("Cathay DDT", "Cathay Financial Holdings — DDT AI"),
        ("Taiwan AI Academy", "Taiwan AI Academy (AIA)"),
        ("NTU", "NTU"),
        ("cathay financial holdings", "Cathay Financial Holdings"),
        ("Cathay（DDT）", "Cathay (DDT)"),  # full-width vs ASCII punctuation
    ],
)
def test_abbreviations_match(left: str, right: str) -> None:
    assert similarity(left, right) >= MATCH_THRESHOLD, similarity(left, right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Google", "Cathay Financial Holdings"),
        ("Taiwan AI Academy", "National Taiwan University"),
        ("Microsoft", "Cathay"),
    ],
)
def test_different_organizations_do_not_match(left: str, right: str) -> None:
    """A false match attaches a suggestion to the wrong job — worse than a duplicate row."""
    assert similarity(left, right) < MATCH_THRESHOLD, similarity(left, right)


def test_normalize_folds_case_width_and_punctuation() -> None:
    assert normalize("Cathay （DDT）!") == normalize("cathay ddt")


def test_repeat_stint_is_a_separate_job() -> None:
    """Two internships at one employer a year apart must not share a row."""
    rows = [approved_row("Taiwan AI Academy (AIA)", date(2025, 6, 1))]
    second_stint = CandidateExperience(
        role="Returning Intern",
        organization="Taiwan AI Academy",
        start="2026-06-01",  # a year later, well outside the window
        bullets=["Came back."],
    )
    assert _match_experience(second_stint, rows) is None


def test_same_stint_matches_despite_date_drift() -> None:
    """A document may date a role by its offer month rather than its start."""
    rows = [approved_row("Taiwan AI Academy (AIA)", date(2025, 6, 1))]
    same = CandidateExperience(
        role="Intern", organization="Taiwan AI Academy", start="2025-07-15", bullets=["Worked."]
    )
    assert _match_experience(same, rows) is rows[0]


def test_undated_candidate_matches_on_name_alone() -> None:
    """Most source documents state no dates; requiring one would create duplicates."""
    rows = [approved_row("Taiwan AI Academy (AIA)", date(2025, 6, 1))]
    undated = CandidateExperience(
        role="Intern", organization="Taiwan AI Academy", bullets=["Worked."]
    )
    assert _match_experience(undated, rows) is rows[0]


def test_best_match_wins_among_several() -> None:
    rows = [
        ExistingRow(page_id="1", label="National Taiwan University", status="Approved"),
        ExistingRow(page_id="2", label="Taiwan AI Academy (AIA)", status="Approved"),
    ]
    match = _match_by_label("Taiwan AI Academy", rows)
    assert match is not None and match.page_id == "2"


def test_no_match_returns_none() -> None:
    rows = [ExistingRow(page_id="1", label="Google", status="Approved")]
    assert _match_by_label("Cathay Financial Holdings", rows) is None


def test_empty_label_never_matches() -> None:
    """A row whose title failed to parse must not swallow every candidate."""
    assert similarity("", "Google") == 0.0
    assert _match_by_label("Google", [ExistingRow(page_id="1", label="", status=None)]) is None
