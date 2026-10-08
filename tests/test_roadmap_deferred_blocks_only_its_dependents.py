"""A deferred section blocks only the dependency that names it.

A plan holding a deferred section stays open, so a whole-plan dependency on it
must clear once no section is left implementable — a deferred section is not
impediment, it is a section the author chose not to build here. A dependency on
the deferred section itself stays blocked. A plan declaring no sections withholds
today's answer: only completion satisfies it until its sections are declared.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.roadmap import build_roadmap


def _row(
    slug: str,
    *,
    status: str = "active",
    declarations: dict | None = None,
    depends_on: list | None = None,
) -> dict:
    """One composed inventory row, as discovery hands it to the roadmap."""

    row: dict = {
        "slug": slug,
        "title": slug,
        "type": "plan",
        "project": "proj",
        "status": status,
        "effort": "M",
        "impl": 0.0,
    }
    if declarations is not None:
        row["section_declarations"] = declarations
    if depends_on is not None:
        row["depends_on"] = depends_on
    return row


@pytest.fixture()
def roadmap_rows(tmp_path: Path) -> dict:
    """Return ``build_roadmap``'s pending rows keyed by slug, over one project
    whose section declarations are authored into the inventory."""

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()

    def build(inventory: list[dict]) -> dict:
        report = build_roadmap("proj", inventory, [], docs_dir=docs_dir, review={})
        return {str(row["slug"]): row for row in report["pending_work"]}

    return build


def test_a_deferred_section_leaves_a_whole_plan_dependency_ready(roadmap_rows) -> None:
    """A depends on nothing; B waits on A, whose only open section is deferred.

    A is active with s1 done and s2 deferred, so no section is left
    implementable and the whole-plan dependency clears without A being complete.
    """
    rows = roadmap_rows(
        [
            _row("A", declarations={"s1": "done", "s2": "deferred"}),
            _row("B", depends_on=["A"]),
        ]
    )

    (dependency,) = rows["B"]["depends_on"]
    assert dependency["ref"] == "A"
    assert dependency["satisfied"] is True
    assert rows["B"]["ready"] is True


def test_a_done_section_satisfies_a_section_dependency(roadmap_rows) -> None:
    """A dependency on A#s1 is satisfied by the section's own done declaration."""
    rows = roadmap_rows(
        [
            _row("A", declarations={"s1": "done", "s2": "deferred"}),
            _row("B", depends_on=["A#s1"]),
        ]
    )

    (dependency,) = rows["B"]["depends_on"]
    assert dependency["ref"] == "A#s1"
    assert dependency["stage"] == "s1"
    assert dependency["satisfied"] is True


def test_a_deferred_section_blocks_only_a_dependency_that_names_it(
    roadmap_rows,
) -> None:
    """A dependency on A#s2 stays blocked, and the blocker names A#s2."""
    rows = roadmap_rows(
        [
            _row("A", declarations={"s1": "done", "s2": "deferred"}),
            _row("B", depends_on=["A#s2"]),
        ]
    )

    (dependency,) = rows["B"]["depends_on"]
    assert dependency["ref"] == "A#s2"
    assert dependency["satisfied"] is False
    assert rows["B"]["blocked_sections"] == ["s2"]
    (blocker,) = rows["B"]["section_readiness"][0]["blockers"]
    assert blocker["ref"] == "A#s2"


def test_an_implementable_section_blocks_a_whole_plan_dependency(roadmap_rows) -> None:
    """A section still implementable holds the whole-plan dependency, as before."""
    rows = roadmap_rows(
        [
            _row("C", declarations={"s1": "done", "s2": "implementable"}),
            _row("D", depends_on=["C"]),
        ]
    )

    (dependency,) = rows["D"]["depends_on"]
    assert dependency["satisfied"] is False
    assert rows["D"]["ready"] is False


def test_a_plan_without_declarations_blocks_until_complete(roadmap_rows) -> None:
    """No declarations declares no work: only completion satisfies the dependency."""
    active = roadmap_rows(
        [
            _row("E"),
            _row("F", depends_on=["E"]),
        ]
    )
    (dependency,) = active["F"]["depends_on"]
    assert dependency["satisfied"] is False
    assert active["F"]["ready"] is False

    complete = roadmap_rows(
        [
            _row("E", status="done"),
            _row("F", depends_on=["E"]),
        ]
    )
    (dependency,) = complete["F"]["depends_on"]
    assert dependency["satisfied"] is True
    assert complete["F"]["ready"] is True
