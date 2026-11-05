"""A read can name a window and be answered with what moved inside it.

The fixture is a repository built under ``tmp_path``: two plans whose section
declarations and dependency edge are known, a git history that moves one
declaration, and a producer transition stream that moves one run's
classification. Every expectation is derived from the fixture's own commits and
rows rather than echoed from the reader's output, so the assertions fail if the
window is answered from the present instead of from the two revisions.

The three MCP reads — the roadmap brief, the roadmap's own named views, and
``crew live`` — are exercised over the same fixture, because a change set that
only the composing module can see is not a read that answers the question.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from textwrap import dedent

import pytest

from reckon import change_set as change_set_module

PROJECT = "sample"
DAY = 86_400
BASE = int(datetime(2026, 10, 1, tzinfo=UTC).timestamp())


def _iso(seconds: int) -> str:
    moment = datetime.fromtimestamp(BASE + seconds, tz=UTC)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _git(repo: Path, *arguments: str, when: str | None = None) -> str:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"RECKON_RUN_ID", "RECKON_MANIFEST", "RECKON_ATTEMPT_STARTED_AT"}
    }
    if when is not None:
        environment.update(
            {
                "GIT_AUTHOR_DATE": when,
                "GIT_COMMITTER_DATE": when,
                "GIT_AUTHOR_NAME": "fixture",
                "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
                "GIT_COMMITTER_NAME": "fixture",
                "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            }
        )
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return completed.stdout.strip()


def _gate(section: str, verdict: str, gate_id: str = "g1") -> str:
    """A section gate as the roadmap's own reader parses it."""
    return (
        '<section data-reckon="gates"><div class="r-gate" '
        f'data-id="{gate_id}" data-section="{section}" '
        f'data-gated-sections="{section}" data-status="open" '
        f'data-verdict="{verdict}">'
        '<h4 class="r-gate-measure">measure</h4>'
        '<p class="r-gate-required-evidence">evidence</p></div></section>'
    )


def _plan(
    slug: str,
    declarations: dict[str, str],
    depends_on: dict[str, list[str]] | None = None,
    status: str = "active",
    gates: str = "",
    extra_headings: tuple[str, ...] = (),
) -> str:
    """One plan document carrying the head metadata the change set reads."""

    dep_meta = ""
    if depends_on is not None:
        dep_meta = (
            f'<meta name="plan-section-depends-on" '
            f'content="{json.dumps(depends_on).replace(chr(34), "&quot;")}">'
        )
    return dedent(
        f"""\
        <!doctype html><html><head>
        <meta charset="utf-8">
        <meta name="docs-project" content="{PROJECT}">
        <meta name="reckon-type" content="plan">
        <meta name="plan-slug" content="{slug}">
        <meta name="plan-title" content="Plan {slug}">
        <meta name="plan-status" content="{status}">
        <meta name="plan-section-declarations" content='{json.dumps(declarations)}'>
        {dep_meta}
        <meta name="plan-version" content="1">
        </head><body>
        <h2 id="s1">§1 — first</h2>
        <h2 id="s2">§2 — second</h2>
        {"".join(f'<h2 id="{name}">§ — {name}</h2>' for name in extra_headings)}
        {gates}
        </body></html>
        """
    )


def _commit(repo: Path, message: str, when: str, files: dict[str, str]) -> str:
    for path, text in files.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _git(repo, "add", *files)
    _git(repo, "commit", "-q", "-m", message, when=when)
    return _git(repo, "rev-parse", "HEAD")


def _row(
    event: str,
    when: str,
    run_id: str,
    from_state: str | None,
    to_state: str,
) -> str:
    return json.dumps(
        {
            "project": PROJECT,
            "event": event,
            "observed_at": when,
            "run_id": run_id,
            "node": f"node-{run_id}",
            "from_state": from_state,
            "to_state": to_state,
        }
    )


@pytest.fixture
def fixture(tmp_path: Path) -> dict:
    """A repository, a transition stream and the instants either side of a move.

    The first commit declares ``a#s1`` implementable and a plan ``b`` waiting on
    it; the second declares ``a#s1`` done. ``moved_at`` sits after the second
    commit, so a window that ends there and starts before it holds exactly that
    one declaration change.
    """
    root = tmp_path / "checkout"
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    first = _iso(0)
    _commit(
        root,
        "two plans, one waiting on the other",
        first,
        {
            "docs/plans/a.html": _plan(
                "a", {"s1": "implementable", "s2": "implementable"}
            ),
            "docs/plans/b.html": _plan(
                "b", {"s1": "implementable"}, depends_on={"s1": ["a#s1"]}
            ),
            # A section carrying no declaration: its gates alone satisfy it, so
            # the dependent below is held by a gate rather than by a landing.
            "docs/plans/e.html": _plan("e", {}, gates=_gate("s1", "failed")),
            "docs/plans/d.html": _plan(
                "d", {"s1": "implementable"}, depends_on={"s1": ["e#s1"]}
            ),
            # A dependency on a whole plan rather than on one of its sections.
            "docs/plans/f.html": _plan("f", {"s1": "implementable"}),
            "docs/plans/g.html": _plan(
                "g", {"s1": "implementable"}, depends_on={"s1": ["f"]}
            ),
            # A plan whose sections are nearly complete, but whose markup
            # carries a heading no declaration names: the roadmap's whole-plan
            # rule holds it, so its dependent is never released.
            "docs/plans/h.html": _plan(
                "h",
                {"s1": "implementable", "s2": "deferred"},
                extra_headings=("s3",),
            ),
            "docs/plans/i.html": _plan(
                "i", {"s1": "implementable"}, depends_on={"s1": ["h"]}
            ),
        },
    )
    second = _iso(DAY)
    _commit(
        root,
        "the first section of a lands",
        second,
        {
            "docs/plans/a.html": _plan("a", {"s1": "done", "s2": "implementable"}),
        },
    )

    stream = tmp_path / "watch" / f"{PROJECT}-000000000000.events"
    stream.parent.mkdir(parents=True)
    stream.write_text(
        "\n".join(
            [
                # Before the window: run two's state change must not be reported.
                _row("baseline", _iso(60), "run-2", None, "working"),
                _row("transition", _iso(600), "run-2", "working", "blocked"),
                # The observation that opens the window.
                _row("baseline", _iso(DAY // 2), "run-1", None, "working"),
                # Inside the window: exactly this one run's classification moved.
                _row("transition", _iso(DAY + 600), "run-1", "working", "blocked"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "root": root,
        "stream": stream,
        "before": datetime.fromtimestamp(BASE + 900, tz=UTC),
        "at_move": datetime.fromtimestamp(BASE + DAY + 3_600, tz=UTC),
        "later": datetime.fromtimestamp(BASE + DAY + 7_200, tz=UTC),
        "first_commit": first,
        "second_commit": second,
    }


def test_the_window_names_what_moved_in_it(fixture):
    """A read with a driven clock is answered from the two revisions, not today."""
    root, stream = fixture["root"], fixture["stream"]

    opened = change_set_module.change_set(
        PROJECT, None, repo=root, stream_path=stream, now=fixture["before"]
    )
    assert opened["full"] is True
    assert opened["sections"] == [] and opened["runs"] == []

    moved = change_set_module.change_set(
        PROJECT,
        opened["observed_at"],
        repo=root,
        stream_path=stream,
        now=fixture["at_move"],
    )

    assert moved["full"] is False
    # The instant is the one the injected clock names, not whatever the reader
    # would compute for itself — a comparison against the function's own output
    # would pass even if the clock were ignored.
    assert moved["observed_at"] == _iso(DAY + 3_600)
    assert moved["sources"] == {"plans": True, "runs": True}
    assert [(row["ref"], row["from"], row["to"]) for row in moved["sections"]] == [
        ("a#s1", "implementable", "done")
    ]
    assert moved["sections"][0]["commit"] == _git(root, "rev-parse", "HEAD")
    assert [row["ref"] for row in moved["newly_ready"]] == ["b#s1"]
    assert moved["newly_ready"][0]["finished_waiting_on"] == ["a#s1"]
    assert [row["run_id"] for row in moved["runs"]] == ["run-1"]


def test_the_window_holds_exactly_the_one_run_whose_classification_moved(fixture):
    """Run two moved before the window opened, so only run one is reported."""
    root, stream = fixture["root"], fixture["stream"]
    opened = change_set_module.change_set(
        PROJECT, None, repo=root, stream_path=stream, now=fixture["before"]
    )
    moved = change_set_module.change_set(
        PROJECT,
        opened["observed_at"],
        repo=root,
        stream_path=stream,
        now=fixture["at_move"],
    )
    assert [row["run_id"] for row in moved["runs"]] == ["run-1"]
    assert moved["runs"][0]["from_state"] == "working"
    assert moved["runs"][0]["to_state"] == "blocked"


def test_a_change_after_the_commit_belongs_to_the_later_one(fixture):
    """Two in-window commits credit their own sections, not the newest one."""
    root, stream = fixture["root"], fixture["stream"]
    opened = change_set_module.change_set(
        PROJECT, None, repo=root, stream_path=stream, now=fixture["before"]
    )
    earlier = _git(root, "rev-parse", "HEAD")
    third = _commit(
        root,
        "the second section of a lands too",
        _iso(DAY + 1_800),
        {"docs/plans/a.html": _plan("a", {"s1": "done", "s2": "done"})},
    )
    moved = change_set_module.change_set(
        PROJECT,
        opened["observed_at"],
        repo=root,
        stream_path=stream,
        now=fixture["at_move"],
    )
    assert [(row["ref"], row["commit"]) for row in moved["sections"]] == [
        ("a#s1", earlier),
        ("a#s2", third),
    ]
    assert moved["sections"][0]["observed_at"] == _iso(DAY)
    assert moved["sections"][1]["observed_at"] == _iso(DAY + 1_800)


def test_a_change_after_the_observation_instant_waits_for_the_next_answer(fixture):
    """The window is closed at its end, so a later change is reported once, later."""
    root, stream = fixture["root"], fixture["stream"]
    opened = change_set_module.change_set(
        PROJECT, None, repo=root, stream_path=stream, now=fixture["before"]
    )
    third = _commit(
        root,
        "a second section lands after the observation",
        _iso(DAY + 5_400),
        {"docs/plans/a.html": _plan("a", {"s1": "done", "s2": "done"})},
    )
    stream.write_text(
        stream.read_text(encoding="utf-8")
        + _row("transition", _iso(DAY + 5_400), "run-3", "working", "complete")
        + "\n",
        encoding="utf-8",
    )
    moved = change_set_module.change_set(
        PROJECT,
        opened["observed_at"],
        repo=root,
        stream_path=stream,
        now=fixture["at_move"],
    )
    assert [row["ref"] for row in moved["sections"]] == ["a#s1"]
    assert [row["run_id"] for row in moved["runs"]] == ["run-1"]

    next_read = change_set_module.change_set(
        PROJECT,
        moved["observed_at"],
        repo=root,
        stream_path=stream,
        now=fixture["later"],
    )
    assert [(row["ref"], row["commit"]) for row in next_read["sections"]] == [
        ("a#s2", third)
    ]
    assert [row["run_id"] for row in next_read["runs"]] == ["run-3"]


def test_a_section_released_by_a_passing_gate_is_reported_ready(fixture):
    """A gate alone satisfies a section, so its dependent is released by it."""
    root, stream = fixture["root"], fixture["stream"]
    opened = change_set_module.change_set(
        PROJECT, None, repo=root, stream_path=stream, now=fixture["before"]
    )
    _commit(
        root,
        "the gate on e closes",
        _iso(DAY + 2_400),
        {"docs/plans/e.html": _plan("e", {}, gates=_gate("s1", "passed"))},
    )
    moved = change_set_module.change_set(
        PROJECT,
        opened["observed_at"],
        repo=root,
        stream_path=stream,
        now=fixture["at_move"],
    )
    released = {row["ref"]: row for row in moved["newly_ready"]}
    # ``a#s1`` landed in this same window, so ``b#s1`` is released alongside.
    assert set(released) == {"b#s1", "d#s1"}
    assert released["d#s1"]["finished_waiting_on"] == ["e#s1"]


def test_a_dependent_on_a_whole_plan_is_released_when_it_completes(fixture):
    """A ref naming no section waits on the plan, so completion releases it."""
    root, stream = fixture["root"], fixture["stream"]
    opened = change_set_module.change_set(
        PROJECT, None, repo=root, stream_path=stream, now=fixture["before"]
    )
    _commit(
        root,
        "f completes",
        _iso(DAY + 2_700),
        {"docs/plans/f.html": _plan("f", {"s1": "done"}, status="done")},
    )
    moved = change_set_module.change_set(
        PROJECT,
        opened["observed_at"],
        repo=root,
        stream_path=stream,
        now=fixture["at_move"],
    )
    released = {row["ref"]: row for row in moved["newly_ready"]}
    assert "g#s1" in released
    assert released["g#s1"]["finished_waiting_on"] == ["f"]


def test_a_whole_plan_held_by_an_undeclared_heading_releases_nothing(fixture):
    """The whole-plan rule reads the authored headings from the docs root.

    ``h`` reaches the roadmap with a done section and only deferred open
    sections, so the decision falls to its authored headings — and its markup
    carries one no declaration names. The roadmap holds the dependency, so the
    plan waiting on it stays unreleased even though a section of it landed.
    """
    root, stream = fixture["root"], fixture["stream"]
    opened = change_set_module.change_set(
        PROJECT, None, repo=root, stream_path=stream, now=fixture["before"]
    )
    _commit(
        root,
        "the open section of h lands",
        _iso(DAY + 3_000),
        {
            "docs/plans/h.html": _plan(
                "h",
                {"s1": "done", "s2": "deferred"},
                extra_headings=("s3",),
            )
        },
    )
    moved = change_set_module.change_set(
        PROJECT,
        opened["observed_at"],
        repo=root,
        stream_path=stream,
        now=fixture["at_move"],
    )
    # The section landing was seen, so the plan and its markup were read.
    assert "h#s1" in {row["ref"] for row in moved["sections"]}
    assert "i#s1" not in {row["ref"] for row in moved["newly_ready"]}


def test_a_project_with_no_stream_does_not_claim_to_have_read_one(fixture, tmp_path):
    """No producer means no run history, which is not the same as an empty one."""
    answer = change_set_module.change_set(
        PROJECT,
        _iso(1),
        repo=fixture["root"],
        stream_path=tmp_path / "watch" / "absent-000000000000.events",
        now=fixture["later"],
    )
    assert answer["sources"] == {"plans": True, "runs": False}
    assert answer["runs"] == []


def test_an_unchanged_project_answers_empty_with_a_new_instant(fixture):
    """Nothing landed between two reads, so the set is empty and the clock moves."""
    root, stream = fixture["root"], fixture["stream"]
    opened = change_set_module.change_set(
        PROJECT, None, repo=root, stream_path=stream, now=fixture["at_move"]
    )
    quiet = change_set_module.change_set(
        PROJECT,
        opened["observed_at"],
        repo=root,
        stream_path=stream,
        now=fixture["later"],
    )
    assert quiet["sections"] == [] and quiet["newly_ready"] == []
    assert quiet["runs"] == []
    assert quiet["full"] is False
    assert quiet["observed_at"] > opened["observed_at"]


def test_a_window_older_than_the_stream_is_answered_in_full(fixture):
    """A since the stream cannot reach back to is widened, never read as silence."""
    root, stream = fixture["root"], fixture["stream"]
    answer = change_set_module.change_set(
        PROJECT,
        _iso(1),
        repo=root,
        stream_path=stream,
        now=fixture["later"],
    )
    assert answer["full"] is True
    assert answer["window_start"] == _iso(60)
    assert {row["run_id"] for row in answer["runs"]} == {"run-1", "run-2"}
    assert [row["ref"] for row in answer["sections"]] == ["a#s1"]


def test_an_unreadable_since_is_named_rather_than_answered_empty(fixture):
    """An instant that cannot be parsed says so instead of meaning no change."""
    answer = change_set_module.change_set(
        PROJECT,
        "not-a-time",
        repo=fixture["root"],
        stream_path=fixture["stream"],
        now=fixture["later"],
    )
    assert answer["error"] == "unreadable_since"
    assert answer["sections"] == [] and answer["runs"] == []


@pytest.fixture
def config_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


@pytest.mark.parametrize("view", [None, "summary", "detail"])
def test_the_roadmap_carries_the_change_set(fixture, config_home, view):
    """Every roadmap view answers a window, the brief included."""
    from reckon import mcp

    opened = change_set_module.change_set(
        PROJECT,
        None,
        repo=fixture["root"],
        stream_path=fixture["stream"],
        now=fixture["before"],
    )
    answer = mcp._roadmap_tool(
        PROJECT,
        checkout_path=str(fixture["root"]),
        view=view,
        since=opened["observed_at"],
    )
    assert answer.get("ok", True) is not False
    assert answer["observed_at"]
    assert [row["ref"] for row in answer["change_set"]["sections"]] == ["a#s1"]
    assert [row["ref"] for row in answer["change_set"]["newly_ready"]] == ["b#s1"]


def test_crew_live_carries_the_change_set(fixture, config_home, monkeypatch):
    """The live view answers the same window from the same composition."""
    from reckon import mcp
    from reckon.crew import run_paths

    monkeypatch.setattr(
        run_paths, "watch_stream_path", lambda project: fixture["stream"]
    )
    opened = change_set_module.change_set(
        PROJECT,
        None,
        repo=fixture["root"],
        stream_path=fixture["stream"],
        now=fixture["before"],
    )
    answer = mcp._crew(
        project=PROJECT,
        view="live",
        checkout_path=str(fixture["root"]),
        since=opened["observed_at"],
    )
    assert answer["ok"] is True
    assert answer["observed_at"]
    assert [row["run_id"] for row in answer["change_set"]["runs"]] == ["run-1"]


def test_a_read_with_no_window_still_returns_the_instant(fixture, config_home):
    """The first read has no window to answer, and hands back what to pass next."""
    from reckon import mcp

    answer = mcp._roadmap_tool(PROJECT, checkout_path=str(fixture["root"]))
    assert answer["observed_at"]
    assert answer["change_set"]["full"] is True
    assert answer["change_set"]["sections"] == []
