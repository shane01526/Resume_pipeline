"""Stage 7: field-level diff between the last approved resume and the new one.

Matching is by `stable_key()`, not by position or by text. That is what makes a reworded
bullet show up as "modified" rather than as a delete plus an add — the distinction that
decides whether the review page is readable or noise.

Three change kinds:

- **added**    a key that wasn't in the previous snapshot
- **removed**  a key that was, and isn't now
- **modified** the same key with different field values, reported field by field

Bullets are compared as a list, and a bullet-level diff is included so you can see which
line changed inside an entry rather than a wall of before/after text.
"""

from __future__ import annotations

import difflib
import json
import logging
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from pipeline.models import Resume
from pipeline.state import DiffCounts

log = logging.getLogger(__name__)


class ChangeKind(StrEnum):
    ADDED = "added"
    REMOVED = "removed"
    MODIFIED = "modified"


# Fields excluded from comparison. These are bookkeeping, not resume content: flagging a
# `Last Synced` bump as a change would mean every run reports a diff.
IGNORED_FIELDS = frozenset({"notion_page_id", "source", "confidence"})

# Section order on the review page, matching the resume itself.
SECTION_ORDER = ("profile", "education", "experiences", "projects", "publications", "skills")


class FieldChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    before: str | None = None
    after: str | None = None


class ItemChange(BaseModel):
    """One added, removed, or modified entry."""

    model_config = ConfigDict(extra="forbid")

    kind: ChangeKind
    section: str
    key: str
    label: str = Field(description="Human-readable identity, e.g. the organization name")
    notion_page_id: str | None = None
    fields: list[FieldChange] = Field(default_factory=list)
    bullet_diff: list[str] = Field(
        default_factory=list, description="Unified diff lines for the bullet list"
    )


class ResumeDiff(BaseModel):
    model_config = ConfigDict(extra="forbid")

    counts: DiffCounts
    changes: list[ItemChange] = Field(default_factory=list)
    is_first_run: bool = Field(
        default=False, description="No previous snapshot: everything reads as added"
    )

    def by_section(self) -> dict[str, list[ItemChange]]:
        """Changes grouped for rendering, in resume section order."""
        grouped: dict[str, list[ItemChange]] = {}
        for section in SECTION_ORDER:
            if members := [c for c in self.changes if c.section == section]:
                grouped[section] = members
        return grouped


def compute_diff(previous: dict[str, Any] | None, current: Resume) -> ResumeDiff:
    """Compare the previous approved snapshot against the new resume.

    `previous` is raw JSON rather than a `Resume` on purpose: an old snapshot written
    before a model change should still be diffable, and strict validation would reject it.
    """
    current_data = current.model_dump(mode="json", by_alias=True)

    if previous is None:
        # First run: report the whole resume as added so the review page shows what is
        # about to be published rather than an empty diff.
        changes = [
            _added_change(section, item)
            for section in SECTION_ORDER
            if section != "profile"
            for item in current_data.get(section, [])
        ]
        if profile_change := _profile_diff(None, current_data.get("profile", {})):
            changes.insert(0, profile_change)
        return ResumeDiff(counts=DiffCounts(added=len(changes)), changes=changes, is_first_run=True)

    changes: list[ItemChange] = []

    if profile_change := _profile_diff(previous.get("profile"), current_data.get("profile", {})):
        changes.append(profile_change)

    for section in SECTION_ORDER:
        if section == "profile":
            continue
        changes.extend(
            _section_diff(section, previous.get(section) or [], current_data.get(section) or [])
        )

    counts = DiffCounts(
        added=sum(1 for c in changes if c.kind is ChangeKind.ADDED),
        modified=sum(1 for c in changes if c.kind is ChangeKind.MODIFIED),
        removed=sum(1 for c in changes if c.kind is ChangeKind.REMOVED),
    )
    return ResumeDiff(counts=counts, changes=changes)


def _section_diff(
    section: str, previous: list[dict[str, Any]], current: list[dict[str, Any]]
) -> list[ItemChange]:
    """Diff one section by stable key."""
    before = {_key_of(section, item): item for item in previous}
    after = {_key_of(section, item): item for item in current}

    changes: list[ItemChange] = []

    for key, item in after.items():
        if key not in before:
            changes.append(_added_change(section, item))
        elif fields := _field_changes(before[key], item, section):
            changes.append(
                ItemChange(
                    kind=ChangeKind.MODIFIED,
                    section=section,
                    key=key,
                    label=_label_of(item),
                    notion_page_id=item.get("notion_page_id"),
                    fields=fields,
                    bullet_diff=_bullet_diff(before[key].get("bullets"), item.get("bullets")),
                )
            )

    for key, item in before.items():
        if key not in after:
            changes.append(
                ItemChange(
                    kind=ChangeKind.REMOVED,
                    section=section,
                    key=key,
                    label=_label_of(item),
                    notion_page_id=item.get("notion_page_id"),
                )
            )

    return changes


def _added_change(section: str, item: dict[str, Any]) -> ItemChange:
    return ItemChange(
        kind=ChangeKind.ADDED,
        section=section,
        key=_key_of(section, item),
        label=_label_of(item),
        notion_page_id=item.get("notion_page_id"),
        # Show the whole entry: on an addition there is no "before" to compare against.
        fields=[
            FieldChange(field=name, after=_stringify(value))
            for name, value in sorted(item.items())
            if name not in IGNORED_FIELDS and value not in (None, "", [], False, 0)
        ],
    )


def _field_changes(
    before: dict[str, Any], after: dict[str, Any], section: str = ""
) -> list[FieldChange]:
    """Per-field differences, bullets excluded (they get their own diff)."""
    # Fill the previous snapshot's missing keys with the model's defaults first. A
    # snapshot written before a field existed omits it entirely while a fresh dump always
    # includes it, so `highlight` compares as absent-vs-False and EVERY run reports
    # phantom modifications — which trains you to approve without reading and defeats the
    # whole review step. Filling defaults means only real edits surface.
    before = _with_defaults(before, section)

    changes = []
    for name in sorted(set(before) | set(after)):
        if name in IGNORED_FIELDS or name == "bullets":
            continue
        old, new = _normalize(before.get(name)), _normalize(after.get(name))
        if old != new:
            changes.append(FieldChange(field=name, before=_stringify(old), after=_stringify(new)))

    # Bullets are summarized as one entry; the line-level detail is in bullet_diff.
    old_bullets, new_bullets = before.get("bullets") or [], after.get("bullets") or []
    if old_bullets != new_bullets:
        changes.append(
            FieldChange(
                field="bullets",
                before=f"{len(old_bullets)} bullet(s)",
                after=f"{len(new_bullets)} bullet(s)",
            )
        )
    return changes


def _bullet_diff(before: list[str] | None, after: list[str] | None) -> list[str]:
    """Unified diff of the bullet lists, so you see which line changed.

    difflib rather than a set comparison: order carries meaning in a resume, and a
    reordered bullet is a real change worth showing.
    """
    before, after = before or [], after or []
    if before == after:
        return []
    return [
        line
        for line in difflib.unified_diff(before, after, lineterm="", n=0)
        # Drop the ---/+++/@@ framing; the review page provides its own context.
        if not line.startswith(("---", "+++", "@@"))
    ]


def _profile_diff(before: dict[str, Any] | None, after: dict[str, Any]) -> ItemChange | None:
    """The header block, treated as a single entry."""
    before = before or {}
    fields = [
        FieldChange(
            field=name, before=_stringify(before.get(name)), after=_stringify(after.get(name))
        )
        for name in sorted(set(before) | set(after))
        if before.get(name) != after.get(name)
    ]
    if not fields:
        return None
    return ItemChange(
        kind=ChangeKind.ADDED if not before else ChangeKind.MODIFIED,
        section="profile",
        key="profile",
        label=after.get("name", "Profile"),
        fields=fields,
    )


def _key_of(section: str, item: dict[str, Any]) -> str:
    """Reconstruct an item's stable key from raw JSON.

    Mirrors the `stable_key()` implementations in models.py. Duplicated because the
    previous snapshot is raw JSON that may predate a model change — instantiating a model
    to get its key would fail on exactly the old data we most need to diff.
    """
    from pipeline.models import slugify

    if section == "experiences":
        start = (item.get("start") or "")[:7]  # YYYY-MM
        return f"exp:{slugify(item.get('organization', ''))}:{start}"
    if section == "projects":
        return f"proj:{slugify(item.get('name', ''))}"
    if section == "education":
        return f"edu:{slugify(item.get('institution', ''))}"
    if section == "skills":
        return f"skill:{item.get('category', '')}:{slugify(item.get('name', ''))}"
    if section == "publications":
        return f"pub:{slugify(item.get('title', ''))}"
    return f"{section}:{item.get('name') or item.get('title') or ''}"


def _label_of(item: dict[str, Any]) -> str:
    """The most identifying string available, for the review page's row header."""
    for field in ("organization", "institution", "name", "title", "role"):
        if value := item.get(field):
            return str(value)
    return "(unnamed)"


def _with_defaults(item: dict[str, Any], section: str) -> dict[str, Any]:
    """Return `item` with any keys it omits filled from the section's model defaults.

    Derived from the Pydantic models rather than hardcoded, so adding a field with a
    default doesn't silently reintroduce phantom diffs on the next run.
    """
    defaults = _section_defaults(section)
    if not defaults:
        return item
    return {**defaults, **item}


def _section_defaults(section: str) -> dict[str, Any]:
    """Default values for a section's model, cached per section."""
    if section in _DEFAULTS_CACHE:
        return _DEFAULTS_CACHE[section]

    from pipeline.models import Education, Experience, Project, Publication, Skill

    model = {
        "education": Education,
        "experiences": Experience,
        "projects": Project,
        "publications": Publication,
        "skills": Skill,
    }.get(section)

    defaults: dict[str, Any] = {}
    if model is not None:
        for name, field in model.model_fields.items():
            if not field.is_required():
                key = field.alias or name
                factory = field.default_factory
                defaults[key] = factory() if factory is not None else field.default  # type: ignore[call-arg]

    _DEFAULTS_CACHE[section] = defaults
    return defaults


_DEFAULTS_CACHE: dict[str, dict[str, Any]] = {}


def _normalize(value: Any) -> Any:
    """Collapse the several ways "no value" is spelled into one.

    A missing key, an explicit null, an empty string, and an empty list all mean the same
    thing for a resume field, but compare as four different values. `False` and `0` are
    NOT collapsed — an unchecked `highlight` is a real state, distinct from a snapshot
    that predates the field.
    """
    if value is None or value == "" or value == []:
        return None
    return value


def _stringify(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) if value else None
    return str(value)


def write_diff(diff: ResumeDiff, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(diff.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return path


def read_diff(path: Path) -> ResumeDiff | None:
    if not path.is_file():
        return None
    return ResumeDiff.model_validate_json(path.read_text(encoding="utf-8"))
