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
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import mcp, mcp_views, velocity
from reckon.cli import main

PROJECT = "alpha"
BRANCH = "main"
DAY = 86400
BASE = int(dt.datetime(2026, 9, 1, tzinfo=dt.UTC).timestamp())
WINDOW_START = velocity.iso(BASE + 1 * DAY)
WINDOW_END = velocity.iso(BASE + 20 * DAY)

#: The arguments both surfaces are driven with. Every view accepts them, so the
#: comparison is the same call on both sides rather than a per-view special case.
_READ_ARGUMENTS = {
    "project": PROJECT,
    "since": WINDOW_START,
    "until": WINDOW_END,
}

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


def _served_by_the_tool(name: str) -> dict:
    return mcp._crew(view=name, **_READ_ARGUMENTS)


def _served_by_the_cli(name: str) -> dict:
    arguments = ["crew", "view", name]
    for key, value in _READ_ARGUMENTS.items():
        arguments += [f"--{key}", value]
    result = CliRunner().invoke(main, arguments)
    # A view that refuses prints its refusal document and exits non-zero; the
    # document is still the whole of stdout, so parity is compared on the
    # payload rather than on the exit code.
    return json.loads(result.stdout)


def test_the_tool_and_the_dispatcher_declare_the_same_views() -> None:
    """Neither surface may grow a view the other does not serve."""
    tool_views = set(mcp.CREW_TOOL_VIEWS)
    dispatcher_views = set(mcp_views.crew_view_names())

    only_on_the_tool = sorted(tool_views - dispatcher_views)
    only_on_the_dispatcher = sorted(dispatcher_views - tool_views)

    assert not only_on_the_tool, (
        "the crew tool serves these views and the crew view dispatcher does "
        f"not: {only_on_the_tool}"
    )
    assert not only_on_the_dispatcher, (
        "the crew view dispatcher serves these views and the crew tool does "
        f"not: {only_on_the_dispatcher}"
    )


def _view_names() -> list[str]:
    """Every view either surface names, read at call time.

    Reading the union rather than one side's constant is deliberate: a view
    registered on one surface only is then *driven* here and its answers
    compared, so the failure names it instead of the comparison silently
    skipping it.
    """
    return sorted(set(mcp.CREW_TOOL_VIEWS) | set(mcp_views.crew_view_names()))


#: The instant a composition ran. A view that reads a live document stamps the
#: moment it composed the answer, so two compositions seconds apart legitimately
#: differ in exactly this field and nowhere else. It is dropped before the two
#: surfaces are compared, so the comparison measures the composition rather than
#: the clock; every other field, including any stamp read out of a file, must
#: still agree.
_VOLATILE_KEYS = frozenset({"composition_time"})


def _without_the_composition_instant(value):
    if isinstance(value, dict):
        return {
            key: _without_the_composition_instant(item)
            for key, item in value.items()
            if key not in _VOLATILE_KEYS
        }
    if isinstance(value, list):
        return [_without_the_composition_instant(item) for item in value]
    return value


def test_every_declared_view_answers_the_same_document_on_both_surfaces(
    fleet: Path,
) -> None:
    disagreements = []
    for name in _view_names():
        served = _without_the_composition_instant(_served_by_the_tool(name))
        emitted = _without_the_composition_instant(_served_by_the_cli(name))
        if emitted != served:
            disagreements.append(name)

    assert not disagreements, (
        "these views answer differently on the command line and on the crew "
        f"tool: {sorted(disagreements)}"
    )


def test_a_view_the_tool_serves_alone_is_named(fleet: Path) -> None:
    """The declared negative control, as an executable assertion.

    Registers a view on the MCP crew tool only. The parity case must then fail
    and name it, which is what makes the parity case worth having: without the
    declaration check, a tool-only view is invisible until a caller meets it.
    """
    original = mcp.CREW_TOOL_VIEWS
    mcp.CREW_TOOL_VIEWS = (*original, "tool-only-view")
    try:
        with pytest.raises(AssertionError, match="tool-only-view"):
            test_the_tool_and_the_dispatcher_declare_the_same_views()
    finally:
        mcp.CREW_TOOL_VIEWS = original
