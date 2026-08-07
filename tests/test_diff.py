"""Stage 7: matching by stable key, and the classification that follows from it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pipeline.diff import ChangeKind, compute_diff, read_diff, write_diff
from pipeline.models import Resume

FIXTURE = Path(__file__).parent / "fixtures" / "resume.sample.json"


@pytest.fixture
def resume() -> Resume:
    return Resume.model_validate_json(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def snapshot(resume: Resume) -> dict[str, Any]:
    """The previous approved state, as raw JSON — which is how it is stored."""
    return resume.model_dump(mode="json", by_alias=True)


def mutate(snapshot: dict[str, Any], **overrides: Any) -> Resume:
    """A Resume built from the snapshot with top-level sections replaced."""
    return Resume.model_validate({**snapshot, **overrides})


# --- baseline ---------------------------------------------------------------


def test_identical_resume_has_no_diff(resume: Resume, snapshot: dict[str, Any]) -> None:
    """The property the whole schedule rests on: no change means no notification."""
    diff = compute_diff(snapshot, resume)
    assert diff.counts.total == 0
    assert diff.changes == []


def test_sparse_snapshot_has_no_phantom_diff(resume: Resume) -> None:
    """A snapshot that omits defaulted keys must still compare as unchanged.

    This is the regression test for a bug the fixture-based test above could not catch,
    because it built both sides by dumping the model — so both already carried defaults.
    Real snapshots are hand-written or predate a field, so they omit keys like
    `highlight`, which then compared as absent-vs-False. Every run reported ~11 phantom
    modifications, which is worse than no diff at all: it trains you to approve without
    reading.
    """
    sparse = json.loads(FIXTURE.read_text(encoding="utf-8"))
    # The fixture genuinely omits these on most rows — that is the point.
    assert "highlight" not in sparse["education"][0]
    assert "tags" not in sparse["education"][0]

    diff = compute_diff(sparse, resume)
    assert diff.counts.total == 0, [(c.label, [f.field for f in c.fields]) for c in diff.changes]


def test_defaults_do_not_mask_a_real_toggle(resume: Resume, snapshot: dict[str, Any]) -> None:
    """Filling defaults must not swallow a genuine change to a defaulted field."""
    experiences = json.loads(json.dumps(snapshot["experiences"]))
    experiences[1]["highlight"] = True
    diff = compute_diff(snapshot, mutate(snapshot, experiences=experiences))
    assert diff.counts.modified == 1
    assert next(f for f in diff.changes[0].fields if f.field == "highlight").after == "yes"


def test_empty_and_absent_are_equivalent(resume: Resume, snapshot: dict[str, Any]) -> None:
    """An explicit null, an empty string, and an empty list all mean "no value"."""
    sparse = json.loads(json.dumps(snapshot))
    sparse["experiences"][0]["location"] = ""
    sparse["experiences"][0]["tags"] = []
    current = json.loads(json.dumps(snapshot))
    current["experiences"][0]["location"] = None
    del current["experiences"][0]["tags"]

    diff = compute_diff(sparse, Resume.model_validate(current))
    location_or_tags = [
        f.field for c in diff.changes for f in c.fields if f.field in ("location", "tags")
    ]
    assert location_or_tags == []


def test_first_run_reports_everything_added(resume: Resume) -> None:
    """With no baseline the review page must show what is about to be published."""
    diff = compute_diff(None, resume)
    assert diff.is_first_run
    assert diff.counts.added == len(resume.all_items()) + 1  # +1 for the profile
    assert diff.counts.modified == 0
    assert diff.counts.removed == 0


# --- the three kinds --------------------------------------------------------


def test_new_experience_is_added(resume: Resume, snapshot: dict[str, Any]) -> None:
    added = {
        "role": "Software Engineer Intern",
        "organization": "Google",
        "start": "2027-01-01",
        "bullets": ["Shipped a thing."],
    }
    diff = compute_diff(snapshot, mutate(snapshot, experiences=[*snapshot["experiences"], added]))
    assert diff.counts == diff.counts.model_copy(update={"added": 1})
    assert diff.counts.added == 1
    change = next(c for c in diff.changes if c.kind is ChangeKind.ADDED)
    assert change.label == "Google"
    # An addition lists the whole entry: there is no "before" to compare against.
    assert {f.field for f in change.fields} >= {"role", "organization", "start"}


def test_dropped_experience_is_removed(resume: Resume, snapshot: dict[str, Any]) -> None:
    diff = compute_diff(snapshot, mutate(snapshot, experiences=snapshot["experiences"][:-1]))
    assert diff.counts.removed == 1
    assert diff.counts.added == 0
    assert diff.changes[0].label == "Taiwan AI Academy (AIA)"


def test_reworded_role_is_modified_not_replaced(resume: Resume, snapshot: dict[str, Any]) -> None:
    """This is why stable_key() excludes free text.

    Keying on the role would make a title tweak read as delete + add, which is
    unreviewable — you'd be re-reading an entry you didn't change.
    """
    experiences = json.loads(json.dumps(snapshot["experiences"]))
    experiences[0]["role"] = "Machine Learning Engineer Intern"
    diff = compute_diff(snapshot, mutate(snapshot, experiences=experiences))

    assert diff.counts.modified == 1
    assert diff.counts.added == 0
    assert diff.counts.removed == 0
    change = diff.changes[0]
    assert change.kind is ChangeKind.MODIFIED
    field = next(f for f in change.fields if f.field == "role")
    assert field.before == "ML/AI Engineer Intern"
    assert field.after == "Machine Learning Engineer Intern"


def test_end_date_change_is_modified(resume: Resume, snapshot: dict[str, Any]) -> None:
    """Leaving a job is a modification of the same entry, not a new one."""
    experiences = json.loads(json.dumps(snapshot["experiences"]))
    experiences[0]["end"] = "2026-12-31"
    diff = compute_diff(snapshot, mutate(snapshot, experiences=experiences))
    assert diff.counts.modified == 1
    assert next(f for f in diff.changes[0].fields if f.field == "end").after == "2026-12-31"


# --- bullets ----------------------------------------------------------------


def test_bullet_edit_yields_a_line_diff(resume: Resume, snapshot: dict[str, Any]) -> None:
    """A summary count plus the unified diff, so you see which line moved."""
    experiences = json.loads(json.dumps(snapshot["experiences"]))
    experiences[0]["bullets"][0] = "Rebuilt the AI News agent orchestration layer."
    diff = compute_diff(snapshot, mutate(snapshot, experiences=experiences))

    change = diff.changes[0]
    assert change.kind is ChangeKind.MODIFIED
    summary = next(f for f in change.fields if f.field == "bullets")
    assert summary.before == "3 bullet(s)" and summary.after == "3 bullet(s)"

    assert any(line.startswith("+Rebuilt") for line in change.bullet_diff)
    assert any(line.startswith("-Optimized") for line in change.bullet_diff)
    # The ---/+++/@@ framing is stripped; the page supplies its own context.
    assert not any(line.startswith(("---", "+++", "@@")) for line in change.bullet_diff)


def test_added_bullet_shows_only_an_addition(resume: Resume, snapshot: dict[str, Any]) -> None:
    experiences = json.loads(json.dumps(snapshot["experiences"]))
    experiences[0]["bullets"].append("Cut inference latency by 40%.")
    diff = compute_diff(snapshot, mutate(snapshot, experiences=experiences))

    change = diff.changes[0]
    assert next(f for f in change.fields if f.field == "bullets").after == "4 bullet(s)"
    assert [line for line in change.bullet_diff if line.startswith("+")] == [
        "+Cut inference latency by 40%."
    ]
    assert not [line for line in change.bullet_diff if line.startswith("-")]


def test_reordered_bullets_count_as_a_change(resume: Resume, snapshot: dict[str, Any]) -> None:
    """Order carries meaning in a resume, so a reorder is a real change."""
    experiences = json.loads(json.dumps(snapshot["experiences"]))
    experiences[0]["bullets"].reverse()
    diff = compute_diff(snapshot, mutate(snapshot, experiences=experiences))
    assert diff.counts.modified == 1
    assert diff.changes[0].bullet_diff


# --- noise suppression ------------------------------------------------------


def test_bookkeeping_fields_ignored(resume: Resume, snapshot: dict[str, Any]) -> None:
    """`Last Synced` and friends change every run; flagging them would make every run
    report a diff and train you to approve without looking."""
    experiences = json.loads(json.dumps(snapshot["experiences"]))
    experiences[0]["source"] = "a-different-file.md"
    experiences[0]["confidence"] = "Low"
    diff = compute_diff(snapshot, mutate(snapshot, experiences=experiences))
    assert diff.counts.total == 0


def test_priority_change_is_reported(resume: Resume, snapshot: dict[str, Any]) -> None:
    """Reordering the resume IS a change worth reviewing, unlike bookkeeping."""
    experiences = json.loads(json.dumps(snapshot["experiences"]))
    experiences[0]["priority"] = 42
    diff = compute_diff(snapshot, mutate(snapshot, experiences=experiences))
    assert diff.counts.modified == 1


# --- other sections ---------------------------------------------------------


def test_skill_key_includes_category(resume: Resume, snapshot: dict[str, Any]) -> None:
    """Recategorizing a skill is a move, so it reads as one removal and one addition."""
    skills = json.loads(json.dumps(snapshot["skills"]))
    skills[2]["category"] = "Tools"  # Python: Programming -> Tools
    diff = compute_diff(snapshot, mutate(snapshot, skills=skills))
    assert diff.counts.added == 1
    assert diff.counts.removed == 1


def test_profile_change_detected(resume: Resume, snapshot: dict[str, Any]) -> None:
    changed = json.loads(json.dumps(snapshot))
    changed["profile"]["phone"] = "+886 900 000 000"
    diff = compute_diff(snapshot, Resume.model_validate(changed))
    assert diff.counts.modified == 1
    assert diff.changes[0].section == "profile"
    assert next(f for f in diff.changes[0].fields if f.field == "phone").after == "+886 900 000 000"


def test_changes_grouped_in_section_order(resume: Resume, snapshot: dict[str, Any]) -> None:
    """The review page reads top-to-bottom like the resume does."""
    changed = json.loads(json.dumps(snapshot))
    changed["profile"]["phone"] = "+886 900 000 000"
    changed["experiences"][0]["role"] = "Renamed"
    changed["skills"][2]["detail"] = "3.12"
    diff = compute_diff(snapshot, Resume.model_validate(changed))
    assert list(diff.by_section()) == ["profile", "experiences", "skills"]


# --- robustness -------------------------------------------------------------


def test_old_snapshot_with_unknown_fields_still_diffs(
    resume: Resume, snapshot: dict[str, Any]
) -> None:
    """A snapshot written before a model change must remain diffable.

    Validating it into a Resume would raise on the extra field — which is exactly the
    data you most need to compare against.
    """
    legacy = json.loads(json.dumps(snapshot))
    legacy["experiences"][0]["retired_field"] = "whatever"
    diff = compute_diff(legacy, resume)
    # The stale field reads as a removal; the rest of the entry matches.
    assert diff.counts.added == 0
    assert diff.counts.removed == 0


def test_diff_roundtrips_through_json(
    resume: Resume, snapshot: dict[str, Any], tmp_path: Path
) -> None:
    """The diff is written to state/ and re-read by the review page."""
    experiences = json.loads(json.dumps(snapshot["experiences"]))
    experiences[0]["role"] = "Changed"
    diff = compute_diff(snapshot, mutate(snapshot, experiences=experiences))

    path = write_diff(diff, tmp_path / "diff.json")
    assert read_diff(path) == diff


def test_read_diff_returns_none_when_absent(tmp_path: Path) -> None:
    assert read_diff(tmp_path / "nope.json") is None
