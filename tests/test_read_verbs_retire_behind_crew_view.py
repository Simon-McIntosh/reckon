"""The retired crew read verbs answer through the crew view that replaced them.

``crew ledger`` and ``crew velocity`` were each a read the crew MCP tool already
served as a view. Retiring them is only safe if the answer survives the move:
the document a caller received from the retired verb must still be readable from
``crew view``, over the fields the two documents share.

The retired verb's own document is composed here by the module function the
verb called (``ledger.summary``, ``ledger.read_records``,
``findings.findings_by_path``, ``velocity.view``); the successor's comes from
driving the ``crew view`` command line. A different builder composes each side,
so the two documents are never compared whole — only over
:data:`SHARED_FIELDS`, which is written down rather than derived, so a successor
that later drops one of those fields fails this file.

The verbs are gone, so this file also holds the arrow's two ends together: each
retired name exits with the tombstone code and its document names the view that
replaced it, and no string literal under ``reckon/`` still names either as a
``reckon crew`` command.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import ledger as ledger_module
from reckon import velocity
from reckon.cli import main as cli_main
from reckon.crew import findings as findings_module
from reckon.crew.tombstones import TOMBSTONE_EXIT_CODE, TOMBSTONES

PROJECT = "retired-reads"
COMPONENT = "reckon/crew_run_commands.py"
BRANCH = "main"
ROOT = Path(__file__).resolve().parents[1]

DAY = 86400
BASE_EPOCH = int(dt.datetime(2026, 9, 1, tzinfo=dt.UTC).timestamp())

_DISPATCH_IDENTITY = ("RECKON_RUN_ID", "RECKON_MANIFEST", "RECKON_ATTEMPT_STARTED_AT")

#: The successor each retired name answers with, written as the literal a reader
#: types. A row pointed at any other view fails :func:`test_each_retired_verb_names_its_view`.
SUCCESSORS = {
    "ledger": "crew view ledger",
    "velocity": "crew view velocity",
}

#: For each retired read, the fields its own document and its successor's share
#: at the base revision the callers moved at. Written down so a successor that
#: drops one of them is a failure rather than a silently narrower comparison.
SHARED_FIELDS: dict[str, tuple[str, ...]] = {
    # The retired ``--view summary`` rollup and the ``ledger`` view are not one
    # document: both name a ``runs`` field, and one counts them where the other
    # lists them. What they do share is the ledger they read — its path, its
    # project and its version — so that is what is compared.
    "ledger": ("path", "project", "version"),
    "records": ("project", "runs"),
    "findings": ("artefacts", "path", "paths", "project", "records", "rows"),
    "velocity": (
        "by_day",
        "by_lane",
        "by_project",
        "by_project_day_lane_count",
        "ok",
        "project",
        "view",
        "window",
    ),
}


def _iso(day: int, seconds: int = 0) -> str:
    return velocity.iso(BASE_EPOCH + day * DAY + seconds)


def _git(repo: Path, *arguments: str, when: str | None = None) -> str:
    environment = {**os.environ}
    for name in _DISPATCH_IDENTITY:
        environment.pop(name, None)
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
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    ).stdout.strip()


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


def _review_record(run_id: str, review_run_id: str) -> dict:
    return {
        "project": PROJECT,
        "reviewed_run_id": run_id,
        "review_run_id": "r-20260901T000000000000-review",
        "reviewed_head_sha": "0" * 40,
        "scores": {"evidence": 17},
        "findings": [
            {
                "file": COMPONENT,
                "line": "12",
                "text": "unguarded index",
                "severity": "blocking",
            }
        ],
    }


@pytest.fixture(scope="module")
def checkout(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A synthesised checkout holding a committed ledger and a committed review.

    Built once: every case reads the same fixture, and the two sides of each
    comparison must see one repository for their documents to be commensurable.
    """
    root = tmp_path_factory.mktemp("retired-reads")
    repo = root / "repo"
    repo.mkdir()
    mounted = root / "config"
    mounted.mkdir()
    _git(repo, "init", "-q", "-b", BRANCH)

    state = repo / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "crew.json").write_text(
        json.dumps(
            {
                "data": {
                    "runs": [
                        _ledger_row("r-impl", "impl-node", "implement", "claude", 4),
                        _ledger_row("r-review", "review-node", "review", "codex", 4),
                    ],
                    "members": [],
                    "holds": [],
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    _git(repo, "add", "docs/state")
    _git(repo, "commit", "-q", "-m", "chore: seed the ledger", when=_iso(0))

    (repo / "reckon").mkdir()
    (repo / "reckon" / "crew_run_commands.py").write_text("l1\n", encoding="utf-8")
    _git(repo, "add", "reckon/crew_run_commands.py")
    _git(repo, "commit", "-q", "-m", "feat: add a component", when=_iso(1))

    plans = repo / "docs" / "plans"
    plans.mkdir()
    (plans / "p.html").write_text("<p>a</p>\n", encoding="utf-8")
    _git(repo, "add", "docs/plans/p.html")
    _git(repo, "commit", "-q", "-m", "docs(plan): add a plan", when=_iso(3))

    _git(repo, "commit", "-q", "--allow-empty", "-m", "promote(r-impl)", when=_iso(5))
    _git(repo, "commit", "-q", "--allow-empty", "-m", "promote(r-review)", when=_iso(6))

    from reckon.crew import review as review_module

    review_module.store_committed_review(
        _review_record("r-reviewed", "r-review"), root=repo
    )
    _git(repo, "add", "docs/state")
    _git(repo, "commit", "-q", "-m", "chore: store a review", when=_iso(7))

    (mounted / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    return repo


def _cli(arguments: list[str]) -> dict:
    """One ``crew view`` call, whose JSON is the whole of stdout."""
    result = CliRunner().invoke(cli_main, ["crew", "view", *arguments])
    assert result.exit_code == 0, result.output + (getattr(result, "stderr", "") or "")
    return json.loads(result.stdout)


def _base_document(view: str, repo: Path) -> dict:
    """The document the retired verb composed, through the function it called."""
    if view == "ledger":
        return {"project": PROJECT, **ledger_module.summary(PROJECT, root=repo)}
    if view == "records":
        records, _version = ledger_module.read_records(PROJECT, repo)
        return {
            "project": PROJECT,
            "runs": records,
            "holds": ledger_module.holds(PROJECT, repo),
            "members": ledger_module.members(PROJECT, repo),
        }
    if view == "findings":
        return {
            "project": PROJECT,
            **findings_module.findings_by_path(PROJECT, path="reckon/", root=repo),
        }
    if view == "velocity":
        return velocity.view(PROJECT, since=_iso(1), until=_iso(20), checkout_path=repo)
    raise AssertionError(f"no retired verb composed the {view!r} document")


def _head_document(view: str, repo: Path) -> dict:
    """The same document through the successor the retired verb now names."""
    common = ["--project", PROJECT, "--checkout-path", str(repo)]
    if view == "findings":
        return _cli([view, *common, "--path", "reckon/"])
    if view == "velocity":
        return _cli([view, *common, "--since", _iso(1), "--until", _iso(20)])
    return _cli([view, *common])


@pytest.mark.parametrize("view", sorted(SHARED_FIELDS))
def test_crew_view_gives_the_retired_verbs_answer(view: str, checkout: Path) -> None:
    """Each retired read's document survives its move to ``crew view``.

    Compared over the fields the two documents share, never whole: a different
    builder composes each side, so the field set is the contract and it is
    written down in :data:`SHARED_FIELDS`.
    """
    base = _base_document(view, checkout)
    head = _head_document(view, checkout)

    shared = SHARED_FIELDS[view]
    # The instrument check: a field set that matched nothing would let the
    # comparison pass on two empty documents. Each named field must be present
    # on both sides before its value is compared.
    for field in shared:
        assert field in base, f"base document for {view!r} has no {field!r}"
        assert field in head, f"successor document for {view!r} has no {field!r}"

    assert {field: head[field] for field in shared} == {
        field: base[field] for field in shared
    }


def test_each_retired_verb_names_its_view() -> None:
    """Each retired name exits with the tombstone code and names its successor.

    The expectations are written as literals in :data:`SUCCESSORS` rather than
    read from the registry, so a row repointed at another view fails here rather
    than agreeing with itself.
    """
    runner = CliRunner()
    for retired, successor in SUCCESSORS.items():
        result = runner.invoke(cli_main, ["crew", retired, "--project", PROJECT])
        assert result.exit_code == TOMBSTONE_EXIT_CODE, result.output
        payload = json.loads(result.output)
        assert payload["retired"] == retired
        assert payload["successor"] == successor
        assert payload["invocation"].startswith(f"reckon {successor}")


def test_the_invocation_a_tombstone_prints_runs_as_written(checkout: Path) -> None:
    """The invocation the tombstone prints is one the successor accepts.

    A caller follows the arrow literally, so an invocation that carries the
    retired verb's own selection flag through — ``crew view ledger --view
    records`` — sends them to a usage error rather than an answer. Each call
    below is answered by the tombstone, the invocation it prints is read back,
    and that invocation is then run: exit 2 is the usage error a flag the
    successor does not define produces, so it is the code this holds out.
    """
    runner = CliRunner()
    tail = ["--project", PROJECT, "--checkout-path", str(checkout)]
    calls = {
        "ledger": ["crew", "ledger", *tail],
        "records": ["crew", "ledger", "--view", "records", *tail],
        "findings": ["crew", "ledger", "--view", "findings", *tail],
    }
    for view, arguments in calls.items():
        result = runner.invoke(cli_main, arguments)
        assert result.exit_code == TOMBSTONE_EXIT_CODE, result.output
        invocation = json.loads(result.output)["invocation"]
        assert invocation == " ".join(("reckon", "crew", "view", view, *tail))
        followed = runner.invoke(cli_main, shlex.split(invocation)[1:])
        assert followed.exit_code == 0, f"{invocation}: {followed.output}"


def test_no_reckon_source_names_a_retired_read_as_a_command() -> None:
    """No string under ``reckon/`` still sends a caller to a retired verb.

    A citation left behind is worse than the retirement: ``crew list`` and the
    other retired names resolve to a tombstone, so a remedy that names one hands
    a caller an error rather than an arrow.
    """
    offenders: list[str] = []
    for path in sorted(ROOT.glob("reckon/**/*.py")):
        text = path.read_text(encoding="utf-8")
        offenders.extend(
            f"{path.relative_to(ROOT)}: crew {retired}"
            for retired in SUCCESSORS
            if f"crew {retired}" in text
        )
    assert not offenders, "\n".join(offenders)


def test_the_registry_carries_both_retired_reads() -> None:
    """The scan above is only meaningful if the names really are retired."""
    registered = {row.retired for row in TOMBSTONES}
    assert set(SUCCESSORS) <= registered, sorted(registered)
