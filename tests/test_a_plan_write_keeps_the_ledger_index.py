"""A plan write leaves the ledger index alone.

The station's per-project refresh spent most of its main-thread time under
``open_run_index.has_row``, in the index rebuild. ``_ensure`` rebuilds whenever
the generation its index recorded differs from ``ledger.ledger_generation``, and
that function read the store's plan-write counter, which ``_store.write_plan``
advances. So a promotion's plan record, a comment or a section edit invalidated
an index no run row had changed, and the next reader paid a full reload of the
ledger.

The ledger now keeps its own counter: only a ledger write advances it, and only
a ledger reader keys on it. A plan write keeps advancing the plan-write counter
that plan readers key on, and the ledger index survives it.

Both cases measure a run id with no run file, which is the case the profile
shares: a finished run that is not yet promoted has no run file, so ``has_row``
passes its file check and reaches ``_ensure``. A recording stand-in for
``ledger.load`` counts the rebuild, so a pass is the absence of a walk and not
merely a correct answer.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import _store, ledger
from reckon.crew import open_run_index

PROJECT = "sample"
PLAN = "fixture"
POPULATION = 60

UNKNOWN_RUN = "r-20260101T000000000000-no-such-run-file"

PLAN_HTML = (
    "<!doctype html><html><head>"
    '<meta name="docs-project" content="sample">'
    '<meta name="reckon-type" content="plan">'
    '<meta name="plan-slug" content="fixture">'
    '<meta name="plan-title" content="Fixture">'
    '<meta name="plan-status" content="active">'
    '<meta name="plan-version" content="1">'
    "</head><body>"
    '<h2 id="s1">Section one</h2><p>Extend the existing mechanism.</p>'
    "</body></html>"
)


@pytest.fixture
def crew_home(tmp_path, monkeypatch):
    """A synthetic crew home, so nothing here reads or writes the real one."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


def _run_record(index: int, **extra: object) -> dict[str, object]:
    record: dict[str, object] = {
        "run_id": f"r-20260101T0000{index:05d}-node-{index}",
        "project": PROJECT,
        "node": f"node-{index}",
        "role": "implement",
        "plan": PLAN,
        "section": "s1",
        "gate": "passed",
    }
    record.update(extra)
    return record


def _project(tmp_path: Path, crew_home: Path) -> Path:
    """A committed checkout with one plan, and a ledger of ``POPULATION`` runs."""
    repo = tmp_path / "repo"
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / f"{PLAN}.html").write_text(PLAN_HTML, encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "test@example.invalid"],
        ["config", "user.name", "Test"],
        ["add", "docs/plans/fixture.html"],
        ["commit", "-q", "-m", "chore: seed fixture", "-m", "Supply a plan."],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (crew_home / "mounts.json").write_text(json.dumps({PROJECT: str(repo / "docs")}))
    runs = ledger.ledger_path(PROJECT, repo).parent / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    for index in range(POPULATION):
        ledger.write_run_file(PROJECT, _run_record(index), repo, fsync=False)
    return repo


def _current_index(repo: Path, calls: list) -> None:
    """Bring the index current, so a case measures revalidation, not bootstrap.

    The bootstrap itself reads the ledger, which is the case this node is not
    about: the counter is cleared here so the measurement starts from an index
    that is already current.
    """
    assert open_run_index.has_row(PROJECT, UNKNOWN_RUN, repo) is False
    calls.clear()


@pytest.fixture
def counted_load(monkeypatch):
    """Record every call to the authoritative ledger read."""
    calls: list[tuple] = []
    real = ledger.load

    def counting(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(ledger, "load", counting)
    return calls


def test_a_plan_write_leaves_the_plan_write_generation_moving(tmp_path, crew_home):
    """The plan-write counter keeps its own meaning, so plan readers still see it."""
    repo = _project(tmp_path, crew_home)
    before = _store.read_write_generation(PROJECT)
    ledger_before = _store.read_ledger_write_generation(PROJECT)

    state, version = _store.read_plan(PROJECT, PLAN, repo, artifact_type="plan")
    _store.write_plan(PROJECT, PLAN, state, version, repo, artifact_type="plan")

    assert _store.read_write_generation(PROJECT) != before
    assert _store.read_ledger_write_generation(PROJECT) == ledger_before


def test_a_plan_write_leaves_the_ledger_index_current(
    tmp_path, crew_home, counted_load
):
    """A plan write must not send the next ledger lookup back to the documents.

    The plan record a promotion writes, a comment and a section edit all reach
    ``_store.write_plan``. None of them writes a run document, so the index the
    ledger reader holds is still current and the lookup that follows answers
    from it.
    """
    repo = _project(tmp_path, crew_home)
    _current_index(repo, counted_load)

    state, version = _store.read_plan(PROJECT, PLAN, repo, artifact_type="plan")
    _store.write_plan(PROJECT, PLAN, state, version, repo, artifact_type="plan")

    assert open_run_index.has_row(PROJECT, UNKNOWN_RUN, repo) is False
    assert counted_load == [], (
        "the plan write sent the ledger reader back to the documents: "
        f"{len(counted_load)} load(s)"
    )


def test_a_ledger_write_keeps_the_next_lookup_on_the_index(
    tmp_path, crew_home, counted_load
):
    """A ledger write publishes the generation its own readers key on.

    The write publishes the row it wrote and records the generation it left, so
    the next lookup — for a different identity, which the index does not hold —
    still answers without reloading the ledger.
    """
    repo = _project(tmp_path, crew_home)
    _current_index(repo, counted_load)

    ledger.write_run_file(PROJECT, _run_record(9001), repo, fsync=False)

    assert open_run_index.has_row(PROJECT, UNKNOWN_RUN, repo) is False
    assert counted_load == [], (
        "a ledger write did not publish the generation its own readers key on: "
        f"{len(counted_load)} load(s)"
    )
