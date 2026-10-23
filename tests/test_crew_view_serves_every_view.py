"""One function serves every crew read view, and both surfaces call it.

``crew view <name>`` on the command line and ``crew(view=<name>)`` on the MCP
tool both dispatch through :func:`reckon.mcp_views.serve_crew_view`, so for the
same arguments they must answer the same document. This test drives the CLI with
a synthesised fleet under ``tmp_path`` and asserts, view by view, that the two
surfaces agree, and that the tool's declared view surface and the dispatcher's
agree by name.

The second half is what makes the first half mean anything. A view added to one
surface only — the shape a later section takes when it grows the tool without
growing the transport — is caught here and named, rather than being discovered
by a caller whose CLI answers ``invalid_view`` while the tool answers data.

The repositories are synthesised under ``tmp_path`` and nothing outside it is
read: the mounted set is written into the isolated reckon home, so ``--project``
resolves through the synthesised ``mounts.json`` rather than the real one.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import get_args

import pytest
from click.testing import CliRunner

from reckon import budget, mcp, mcp_views, velocity
from reckon._mcp_tools import CrewArgs
from reckon.cli import main

PROJECT = "alpha"
BRANCH = "main"
DAY = 86400
BASE = int(dt.datetime(2026, 9, 1, tzinfo=dt.UTC).timestamp())
WINDOW_START = velocity.iso(BASE + 1 * DAY)
WINDOW_END = velocity.iso(BASE + 20 * DAY)

#: The session the live fixture's pointers are dispatched by. It is what
#: ``--mine`` compares against, so it must match a pointer's own.
LIVE_SESSION = "coordinator-session"

#: The argument vectors both surfaces are driven with, each compared view by
#: view. One vector would let an option added to `crew view` and not to the
#: tool — or the reverse — pass unnoticed, because the views that read it would
#: be compared on a call that never carried it. A vector whose option only one
#: surface understands then disagrees on the view that reads it, and the failure
#: names both the view and the vector.
_BASE_ARGUMENTS = {
    "project": PROJECT,
    "since": WINDOW_START,
    "until": WINDOW_END,
}
_ARGUMENT_VECTORS = (
    ("read", dict(_BASE_ARGUMENTS)),
    ("session", {**_BASE_ARGUMENTS, "session": LIVE_SESSION}),
    ("phase", {**_BASE_ARGUMENTS, "phase": "working"}),
    ("mine", {**_BASE_ARGUMENTS, "session": LIVE_SESSION, "mine": True}),
)

_DISPATCH_IDENTITY = ("RECKON_RUN_ID", "RECKON_MANIFEST", "RECKON_ATTEMPT_STARTED_AT")


def _iso(day: int, seconds: int = 0) -> str:
    return velocity.iso(BASE + day * DAY + seconds)


def _git(repo: Path, *arguments: str, env: dict | None = None) -> str:
    # A git wrapper keyed on the running worker's identity refuses a mutating
    # verb outside its own worktree; drop that identity so the synthesised
    # repository is the only one in play.
    base = {**os.environ, **(env or {})}
    for name in _DISPATCH_IDENTITY:
        base.pop(name, None)
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=base,
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str, day: int, changes: dict[str, str]) -> str:
    when = _iso(day)
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": when,
        "GIT_COMMITTER_DATE": when,
        "GIT_AUTHOR_NAME": "fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }
    for path, content in changes.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        _git(repo, "add", path, env=env)
    _git(repo, "commit", "-q", "--allow-empty", "-m", message, env=env)
    return _git(repo, "rev-parse", "HEAD", env=env)


def _ledger_row(run_id: str, node: str, role: str, backend: str, day: int) -> dict:
    return {
        "run_id": run_id,
        "node": node,
        "plan": "p",
        "role": role,
        "gate": "passed",
        "backend": backend,
        "dispatched_at": _iso(day),
        "completed_at": _iso(day, 100),
        "worker_seconds": 100,
        "lineage": {},
        "attempt": 1,
    }


@pytest.fixture()
def fleet(tmp_path: Path, isolated_reckon_home: Path) -> Path:
    """One mounted repository carrying a seeded ledger and a little history."""
    repo = tmp_path / "code" / PROJECT
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", BRANCH)
    runs = [
        _ledger_row("r-impl", "impl-node", "implement", "claude", 4),
        _ledger_row("r-review", "review-node", "review", "codex", 4),
    ]
    _commit(
        repo,
        "chore: seed the ledger",
        0,
        {
            f"docs/state/{PROJECT}/crew.json": json.dumps(
                {"data": {"members": [], "runs": runs, "holds": []}}, indent=2
            )
        },
    )
    _commit(repo, "feat: add source", 1, {"src/a.py": "l1\nl2\nl3\nl4\nl5\n"})
    _commit(repo, "promote(r-impl)", 5, {})
    _commit(repo, "promote(r-review)", 6, {})
    (isolated_reckon_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}),
        encoding="utf-8",
    )
    return repo


def _live_pointer(run_id: str, *, session: str, phase: str) -> dict:
    """One live pointer in the fields both the listing and `live` read."""
    return {
        "run_id": run_id,
        "project": PROJECT,
        "session": session,
        "phase": phase,
        "node": {"id": run_id, "plan": "delivery"},
    }


@pytest.fixture()
def live_fleet(fleet: Path, monkeypatch) -> Path:
    """The mounted fleet plus live pointers spanning two phases and two sessions.

    Both filters need more than one value to be visible: ``--phase`` needs two
    phases and ``--mine`` two sessions, or a dropped filter would answer the
    same rows as a working one and every comparison built on it would pass
    anyway.
    """
    from reckon import crew
    from reckon.crew import runs as runs_module

    pointers = [
        _live_pointer("run-mine-working", session=LIVE_SESSION, phase="working"),
        _live_pointer("run-peer-working", session="a-peer-session", phase="working"),
        _live_pointer("run-mine-dispatch", session=LIVE_SESSION, phase="dispatch"),
    ]
    # list_live publishes a watch stream when a project is named; stub it so the
    # fixture reads its own pointers and touches nothing on disk.
    monkeypatch.setattr(crew, "list_live", lambda project=None: pointers)
    monkeypatch.setattr(
        runs_module,
        "project_watch_visibility",
        lambda project, session=None: {"project": project, "session": session},
    )
    return fleet


def test_the_live_view_answers_the_list_filters(live_fleet: Path) -> None:
    """The live view keeps, for `--phase` and `--mine`, the rows the listing kept.

    A caller moving off the retired listing keeps the rows it kept before, so
    the expected sets are the fixture's own pointers named by the phase and the
    session they carry. Both surfaces are compared against the same literals,
    and a filter dropped from either answers a different set rather than the
    same one.
    """
    expected_phase = {"run-mine-working", "run-peer-working"}
    phase_arguments = {**_BASE_ARGUMENTS, "phase": "working"}
    for label, served in (
        ("command line", _served_by_the_cli),
        ("crew tool", _served_by_the_tool),
    ):
        rows = served("live", phase_arguments)["runs"]
        assert {row["run_id"] for row in rows} == expected_phase, f"phase on {label}"

    expected_mine = {"run-mine-working", "run-mine-dispatch"}
    mine_arguments = {**_BASE_ARGUMENTS, "session": LIVE_SESSION, "mine": True}
    for label, served in (
        ("command line", _served_by_the_cli),
        ("crew tool", _served_by_the_tool),
    ):
        rows = served("live", mine_arguments)["runs"]
        assert {row["run_id"] for row in rows} == expected_mine, f"mine on {label}"

    # The filters must actually narrow: an unfiltered read is wider, so a filter
    # dropped from the view would answer more rows and the cases above would
    # not both hold, or the filters would be indistinguishable from the base
    # read. Two phases and two sessions are seeded for exactly this.
    unfiltered = _served_by_the_tool("live", _BASE_ARGUMENTS)["runs"]
    assert len(unfiltered) > len(expected_mine)

    # `--mine` without a session compares against nobody, so both surfaces refuse
    # rather than silently answering the whole project.
    for label, served in (
        ("command line", _served_by_the_cli),
        ("crew tool", _served_by_the_tool),
    ):
        refusal = served("live", {**_BASE_ARGUMENTS, "mine": True})
        assert refusal["ok"] is False, label
        assert refusal["error"] == "missing_session", label


def _served_by_the_tool(name: str, arguments: dict) -> dict:
    return mcp._crew(view=name, **arguments)


def _cli_arguments(name: str, arguments: dict) -> list[str]:
    """Render one argument vector as `crew view` argv."""
    rendered = ["crew", "view", name]
    for key, value in arguments.items():
        if key == "mine":
            # A flag, so its presence is the value and there is nothing to pass.
            if value:
                rendered.append("--mine")
            continue
        rendered += [f"--{key}", str(value)]
    return rendered


def _served_by_the_cli(name: str, arguments: dict) -> dict:
    result = CliRunner().invoke(main, _cli_arguments(name, arguments))
    # A view that refuses prints its refusal document and exits non-zero; the
    # document is still the whole of stdout, so parity is compared on the
    # payload rather than on the exit code.
    return json.loads(result.stdout)


def _views_the_schema_declares() -> tuple[str, ...]:
    """The views the crew tool's argument model advertises.

    Read from the model's own ``view`` annotation rather than from a name the
    tool re-exports from the dispatcher: comparing the tool's surface against
    the dispatcher through such a shared name compares the dispatcher with
    itself, so a view the schema fails to advertise is invisible. A control
    replaces this reader to stand in for editing the Literal, which a test
    cannot do to committed source.
    """
    return get_args(CrewArgs.model_fields["view"].annotation)


def test_the_tool_and_the_dispatcher_declare_the_same_views() -> None:
    """Neither surface may grow a view the other does not serve."""
    tool_views = set(_views_the_schema_declares())
    dispatcher_views = set(mcp_views.crew_view_names())

    only_on_the_tool = sorted(tool_views - dispatcher_views)
    only_on_the_dispatcher = sorted(dispatcher_views - tool_views)

    assert not only_on_the_tool, (
        "the crew tool advertises these views and the crew view dispatcher does "
        f"not: {only_on_the_tool}"
    )
    assert not only_on_the_dispatcher, (
        "the crew view dispatcher serves these views and the crew tool does "
        f"not advertise: {only_on_the_dispatcher}"
    )


def _view_names() -> list[str]:
    """Every view either surface names, read at call time.

    Reading the union rather than one side's constant is deliberate: a view
    registered on one surface only is then *driven* here and its answers
    compared, so the failure names it instead of the comparison silently
    skipping it.
    """
    return sorted(set(_views_the_schema_declares()) | set(mcp_views.crew_view_names()))


#: Everything a composition derives from the instant it ran. The lanes view
#: stamps when it composed its answer and reports ages measured from that
#: instant, so two compositions seconds apart legitimately differ in these
#: fields and nowhere else. They are dropped before the two surfaces are
#: compared, so the comparison measures the composition rather than the clock —
#: and every other field, including every stamp read out of a file, must still
#: agree, which is what makes the comparison worth making.
_VOLATILE_KEYS = frozenset(
    {
        "composed_at",
        "age_seconds",
        "lane_age_seconds",
        "fetch_age_seconds",
        "probe_fetch_age_seconds",
    }
)

#: The field a view stamps at its own top level from the instant it composed its
#: answer. The budget view sets ``checked_at`` from the moment
#: :func:`reckon.budget.preflight` runs, so two compositions a moment apart
#: differ there whatever the fixture holds, and the driven clock below makes
#: that difference happen every run rather than only under load.
#:
#: It is dropped by view and at the top level rather than added to
#: :data:`_VOLATILE_KEYS`, which drops by key name at every depth of every view:
#: the review views carry a ``checked_at`` copied from a stored review record at
#: a nested position, and that one records what was written rather than when the
#: answer was composed, so the two surfaces must still agree on it.
_TOP_LEVEL_COMPOSITION_STAMPS = {"budget": frozenset({"checked_at"})}


def _without_the_named_keys(value):
    if isinstance(value, dict):
        return {
            key: _without_the_named_keys(item)
            for key, item in value.items()
            if key not in _VOLATILE_KEYS
        }
    if isinstance(value, list):
        return [_without_the_named_keys(item) for item in value]
    return value


def _without_the_composition_instant(value, *, view: str) -> object:
    """Drop what each surface derived from the instant it happened to run.

    ``view`` selects the stamps dropped at the document's own top level, so a
    stored field that happens to share a name with a composition stamp survives
    at every position below it.
    """
    stamps = _TOP_LEVEL_COMPOSITION_STAMPS.get(view, frozenset())
    if isinstance(value, dict) and stamps:
        value = {key: item for key, item in value.items() if key not in stamps}
    return _without_the_named_keys(value)


#: The instant the budget view is told it is composing at, and how far the
#: second call of each pair is advanced past the first. The two surfaces are
#: invoked seconds apart in reality; driving the clock makes the parity case
#: measure the divergence it guards against instead of waiting for load to
#: produce one.
_PARITY_INSTANT = dt.datetime(2026, 9, 10, 12, 0, tzinfo=dt.UTC)
_PARITY_CLOCK_ADVANCE = dt.timedelta(seconds=30)


def _hold_the_budget_clock_at(monkeypatch, moment: dt.datetime) -> None:
    """Pin ``budget._now`` to ``moment`` for the call that follows."""
    monkeypatch.setattr(budget, "_now", lambda now=None: now or moment)


def _assert_the_pair_reads_the_driven_advance(
    served: dict, emitted: dict, *, label: str
) -> None:
    """The clock drive must have moved the two calls of the pair apart.

    The comparison below only measures something while the two calls compose at
    different instants, and the drop it makes is only exercised while the budget
    view's stamp moves between them. So the advance is read back off the pair's
    own documents before anything is dropped: at a zero advance both calls read
    one instant, the two documents agree on an empty margin, and the case would
    pass having compared nothing the budget view could not already answer.

    The expected advance is written out rather than read from
    :data:`_PARITY_CLOCK_ADVANCE`. An expectation taken from the value under
    test holds at every advance and so holds at zero — the very case this
    assertion exists to refuse.
    """
    composed = dt.datetime.fromisoformat(str(served["checked_at"]))
    compared = dt.datetime.fromisoformat(str(emitted["checked_at"]))
    assert compared - composed == dt.timedelta(seconds=30), (
        f"the budget view with {label} composed its two surfaces {compared - composed} "
        "apart, and the case requires the second call of each pair to read 30 seconds "
        "after the first"
    )


def test_every_declared_view_answers_the_same_document_on_both_surfaces(
    live_fleet: Path, monkeypatch
) -> None:
    disagreements = []
    for name in _view_names():
        for label, arguments in _ARGUMENT_VECTORS:
            _hold_the_budget_clock_at(monkeypatch, _PARITY_INSTANT)
            served = _served_by_the_tool(name, arguments)
            _hold_the_budget_clock_at(
                monkeypatch, _PARITY_INSTANT + _PARITY_CLOCK_ADVANCE
            )
            emitted = _served_by_the_cli(name, arguments)
            if name == "budget":
                _assert_the_pair_reads_the_driven_advance(served, emitted, label=label)
            served = _without_the_composition_instant(served, view=name)
            emitted = _without_the_composition_instant(emitted, view=name)
            if emitted != served:
                disagreements.append(f"{name} with {label}")

    assert not disagreements, (
        "these view/argument pairs answer differently on the command line and "
        f"on the crew tool: {sorted(disagreements)}"
    )


def test_only_the_budget_views_own_stamp_is_dropped() -> None:
    """The budget view's top-level stamp goes; a stored one is still compared.

    The parity case's filter is the subject, driven here with a document of each
    shape. A view other than budget carrying a nested ``checked_at`` — the review
    views' stored stamp — must keep it, and so must the budget view's own nested
    fields: the drop is confined to one view and to its top level.
    """
    stored = {"composed_at": "t0", "review": {"checked_at": "stored"}}
    kept = _without_the_composition_instant(stored, view="reviews")
    assert kept["review"]["checked_at"] == "stored"
    assert "composed_at" not in kept

    composed = {"checked_at": "t0", "backends": [{"checked_at": "stored"}]}
    dropped = _without_the_composition_instant(composed, view="budget")
    assert "checked_at" not in dropped
    assert dropped["backends"][0]["checked_at"] == "stored"


def test_a_view_the_dispatcher_serves_alone_is_named() -> None:
    """One registry control: a view the dispatcher adds and the tool does not.

    Registers a view on the dispatcher's own list. The registry case must then
    fail and name it, which is what makes the case worth having: without it, a
    view reachable from `crew view` and absent from the tool's schema is
    invisible until a caller meets it.
    """
    original = mcp_views.CREW_VIEWS
    mcp_views.CREW_VIEWS = (*original, "dispatcher-only-view")
    try:
        with pytest.raises(AssertionError, match="dispatcher-only-view"):
            test_the_tool_and_the_dispatcher_declare_the_same_views()
    finally:
        mcp_views.CREW_VIEWS = original


def test_a_view_the_schema_declares_alone_is_named(monkeypatch) -> None:
    """The other registry control: a view the tool advertises and no one serves.

    The schema Literal is written by hand, so a view added there and never
    registered on the dispatcher — or a typo in one of its names — is the
    likelier error, and only a tool-side control catches it. Editing the Literal
    is impossible from a test, so this replaces the reader that names it, which
    is the same value the case's own comparison would see were the Literal
    carrying the extra name.
    """
    extended = (*_views_the_schema_declares(), "schema-only-view")
    monkeypatch.setattr(
        sys.modules[__name__], "_views_the_schema_declares", lambda: extended
    )
    with pytest.raises(AssertionError, match="schema-only-view"):
        test_the_tool_and_the_dispatcher_declare_the_same_views()
