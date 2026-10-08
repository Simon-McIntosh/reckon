"""A deferred section blocks only the target work that names it.

A whole-plan dependency on a plan clears once that plan is complete, or once at
least one of its sections is done and none is left implementable — a deferred
section blocks only the work that names that section. Two cases are uncertain
and no fixed rule settles them: a target whose every section is deferred, so
nothing in it was built, and a target with an authored heading carrying no
declaration. Both hold the dependent by default and are reported in the
roadmap's ``judgment_required`` block, where the orchestrator decides. A
dependency naming the deferred section itself stays blocked, and a plan
declaring no sections keeps the completion-only rule.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.roadmap import build_roadmap, resolve_graph_target


def _row(
    slug: str,
    *,
    status: str = "active",
    declarations: dict | None = None,
    depends_on: list | None = None,
    after: list | None = None,
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
    if after is not None:
        row["after"] = after
    return row


class _Env:
    """A project the roadmap reads inventory rows and authored files from."""

    def __init__(self, docs_dir: Path) -> None:
        self.docs_dir = docs_dir
        self.report: dict = {}

    def __call__(self, inventory: list[dict]) -> dict:
        report = build_roadmap("proj", inventory, [], docs_dir=self.docs_dir, review={})
        self.report = report
        return {str(row["slug"]): row for row in report["pending_work"]}

    def write(
        self,
        slug: str,
        *,
        headings: list[str],
        declarations: dict | None = None,
        status: str = "active",
    ) -> None:
        """Author one plan file with the given section headings."""

        from reckon._plan_html import write_state

        body = "".join(f'<h2 id="{name}">§ {name}</h2>' for name in headings)
        bare = (
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="docs-project" content="proj">'
            f"<title>{slug}</title></head>"
            f'<body><main class="plan-doc">{body}</main></body></html>'
        )
        state: dict = {
            "slug": slug,
            "title": slug,
            "type": "plan",
            "version": 1,
            "status": status,
            "impl": 0.0,
        }
        if declarations is not None:
            state["section_declarations"] = declarations
        html = write_state(bare, state)
        (self.docs_dir / f"{slug}.html").write_text(html, encoding="utf-8")


@pytest.fixture()
def roadmap_rows(tmp_path: Path) -> _Env:
    """The roadmap row builder, over one project with an empty docs tree."""

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    return _Env(docs_dir)


def test_a_deferred_section_leaves_a_whole_plan_dependency_ready(roadmap_rows) -> None:
    """A depends on nothing; B waits on A, whose only open section is deferred.

    A is active with s1 done and s2 deferred, so at least one section is done and
    none is left implementable: the whole-plan dependency clears without A being
    complete, and no judgment is required.
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
    assert rows["B"]["judgment_required"]["required"] is False


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
    # An implementable section is plainly incomplete, so no judgment is asked.
    assert rows["D"]["judgment_required"]["required"] is False


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
    # No declarations keeps the completion-only rule: not a judgment case.
    assert active["F"]["judgment_required"]["required"] is False

    complete = roadmap_rows(
        [
            _row("E", status="done"),
            _row("F", depends_on=["E"]),
        ]
    )
    (dependency,) = complete["F"]["depends_on"]
    assert dependency["satisfied"] is True
    assert complete["F"]["ready"] is True


def test_an_all_deferred_plan_reports_deferred_only(roadmap_rows) -> None:
    """A target whose every section is deferred holds its dependent by default.

    No fixed rule settles a whole-plan dependency on a plan that built nothing,
    so it reads blocked and is reported in ``judgment_required`` with the reason
    ``deferred-only``; the orchestrator decides to build or un-defer the
    section, or to narrow or remove the dependency.
    """
    roadmap_rows.write(
        "G",
        headings=["s1", "s2"],
        declarations={"s1": "deferred", "s2": "deferred"},
    )
    rows = roadmap_rows([_row("G"), _row("H", depends_on=["G"])])

    (dependency,) = rows["H"]["depends_on"]
    assert dependency["satisfied"] is False
    assert rows["H"]["ready"] is False
    block = rows["H"]["judgment_required"]
    assert block["required"] is True
    assert block["count"] == 1
    (member,) = block["members"]
    assert member["dependent"] == "proj:H"
    assert member["target"] == "proj:G"
    assert member["reason"] == "deferred-only"
    assert member["sections"] == ["s1", "s2"]
    # The report carries the same member, so a reader without the row sees it.
    assert roadmap_rows.report["judgment_required"] == {
        "required": True,
        "count": 1,
        "members": block["members"],
    }


def test_an_undeclared_heading_reports_undeclared_heading(roadmap_rows) -> None:
    """A heading with no declaration holds the dependency and is reported.

    The heading could be work the dependency still needs, so no fixed rule
    settles it: the dependency reads blocked and is reported in
    ``judgment_required`` with the reason ``undeclared-heading``.
    """
    roadmap_rows.write("I", headings=["s1", "s2"], declarations={"s1": "done"})
    rows = roadmap_rows([_row("I"), _row("J", depends_on=["I"])])

    (dependency,) = rows["J"]["depends_on"]
    assert dependency["satisfied"] is False
    assert rows["J"]["ready"] is False
    (member,) = rows["J"]["judgment_required"]["members"]
    assert member["dependent"] == "proj:J"
    assert member["target"] == "proj:I"
    assert member["reason"] == "undeclared-heading"
    assert member["sections"] == ["s2"]


def test_an_after_edge_to_an_all_deferred_plan_is_reported(roadmap_rows) -> None:
    """An uncertain whole-plan hold reached through an after edge is reported.

    The same judgment an edge declares through ``depends_on`` reaches the
    roadmap through a soft ``after`` edge, and the orchestrator must be told
    whenever an uncertain dependency holds something — whatever route carried
    it. M is sequenced after L, whose every section is deferred, so L's hold is
    reported in M's ``judgment_required`` with the reason ``deferred-only``.
    """
    roadmap_rows.write(
        "L",
        headings=["s1", "s2"],
        declarations={"s1": "deferred", "s2": "deferred"},
    )
    rows = roadmap_rows([_row("L"), _row("M", after=["L"])])

    block = rows["M"]["judgment_required"]
    assert block["required"] is True
    (member,) = block["members"]
    assert member["dependent"] == "proj:M"
    assert member["target"] == "proj:L"
    assert member["reason"] == "deferred-only"
    assert member["sections"] == ["s1", "s2"]


def _graph_row(
    slug: str,
    *,
    depends_on: list | None = None,
    graph_handle: str | None = None,
    declarations: dict | None = None,
) -> dict:
    """One graph inventory row, as project discovery hands it to the resolver."""

    row: dict = {
        "type": "plan",
        "slug": slug,
        "title": slug,
        "status": "active",
        "impl": 0.0,
        "depends_on": depends_on or [],
        "graph_handle": graph_handle,
        "sprint": None,
        "decisions": [],
    }
    if declarations is not None:
        row["section_declarations"] = declarations
    return row


def test_a_graph_judgment_names_the_dependency_its_own_project(
    monkeypatch, tmp_path
) -> None:
    """A cross-project hold qualifies each side with its own project.

    In a graph the held dependency may live in another mounted project, which is
    the case the graph resolver exists for. The member's target must name the
    dependency's project — naming it the dependent's project points at a plan
    that does not exist there.
    """
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "reckon-home"))
    projects = {
        "alpha": [
            _graph_row("dependent", depends_on=["beta:base"], graph_handle="release"),
        ],
        "beta": [
            _graph_row("base", declarations={"s1": "deferred", "s2": "deferred"}),
        ],
    }

    result = resolve_graph_target("release", projects)

    block = result["judgment_required"]
    assert block["required"] is True
    assert block["count"] == 1
    (member,) = block["members"]
    assert member["dependent"] == "alpha:dependent"
    assert member["target"] == "beta:base"
    assert member["reason"] == "deferred-only"
    assert member["sections"] == ["s1", "s2"]
