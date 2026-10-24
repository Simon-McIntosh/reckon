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
from tests.test_crew_surface_parity import CLASSIFICATION_FIELDS

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
    "list": "crew view live",
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
    successor does not define produces, so it is the code this holds out. The
    case a caller spelled ``--view summary`` is read here too, because that is
    the document bare ``crew ledger`` gave, and an arrow sending ``--view
    summary`` to ``crew view summary`` would hand over the fleet summary
    instead. Each call also holds the printed invocation against the
    ``successor`` the payload reports, so one caller is not told two different
    things.
    """
    runner = CliRunner()
    tail = ["--project", PROJECT, "--checkout-path", str(checkout)]
    # Each call names the case the caller spelled and the operand the successor
    # must receive. ``--view summary`` is the rolled-up document bare ``crew
    # ledger`` gave, so it reaches the same successor; ``crew view summary`` is
    # the fleet summary, a different document.
    calls = {
        "ledger": ("ledger", ["crew", "ledger", *tail]),
        "summary": ("ledger", ["crew", "ledger", "--view", "summary", *tail]),
        "records": ("records", ["crew", "ledger", "--view", "records", *tail]),
        "findings": ("findings", ["crew", "ledger", "--view", "findings", *tail]),
    }
    for spelled, (operand, arguments) in calls.items():
        result = runner.invoke(cli_main, arguments)
        payload = json.loads(result.output)
        assert result.exit_code == TOMBSTONE_EXIT_CODE, result.output
        assert payload["successor"] == f"crew view {operand}", payload["successor"]
        invocation = " ".join(("reckon", "crew", "view", operand, *tail))
        assert payload["invocation"] == invocation, (
            f"{spelled}: {payload['invocation']}"
        )
        followed = runner.invoke(cli_main, shlex.split(invocation)[1:])
        assert followed.exit_code == 0, f"{invocation}: {followed.output}"


def test_each_listing_filter_reaches_an_invocation_the_view_accepts() -> None:
    """The listing's arrow resolves for every option the retired verb took.

    A caller moving off the listing carries ``--project``, ``--phase``,
    ``--session`` and ``--mine`` across, and the live view defines all four, so
    the invocation the tombstone prints is one the successor accepts. Each call
    is answered by the tombstone, the printed invocation is read back, and that
    invocation is then run: exit 2 is the usage error an option the successor
    does not define produces, so it is the code this case holds out. The call
    that carries no filter at all is read too, because a bare ``crew list`` was
    the widest answer the verb gave.
    """
    runner = CliRunner()
    calls = {
        "bare": ["crew", "list"],
        "project": ["crew", "list", "--project", PROJECT],
        "phase": ["crew", "list", "--project", PROJECT, "--phase", "working"],
        "session": ["crew", "list", "--project", PROJECT, "--session", "keeper"],
        "mine": [
            "crew",
            "list",
            "--project",
            PROJECT,
            "--session",
            "keeper",
            "--mine",
        ],
    }
    for label, arguments in calls.items():
        result = runner.invoke(cli_main, arguments)
        assert result.exit_code == TOMBSTONE_EXIT_CODE, result.output
        payload = json.loads(result.output)
        assert payload["successor"] == "crew view live", payload["successor"]
        invocation = " ".join(("reckon", "crew", "view", "live", *arguments[2:]))
        assert payload["invocation"] == invocation, f"{label}: {payload['invocation']}"
        followed = runner.invoke(cli_main, shlex.split(invocation)[1:])
        assert followed.exit_code != 2, f"{invocation}: {followed.output}"


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


# --------------------------------------------------------------------------
# The live listing: the read the mover node moved its callers off.
# --------------------------------------------------------------------------

#: The project the captured listing was read for. Its pointers span two phases
#: and two sessions, and a third pointer sits in another project: every filter
#: case below needs more than one value to be visible, because a case whose
#: filter was dropped would otherwise answer the same rows as one whose filter
#: worked and both would pass.
LIVE_PROJECT = PROJECT

#: The pointers the captured listing was read over, as dispatch writes them.
LIVE_POINTERS = (
    {
        "run_id": "r-live-working",
        "project": PROJECT,
        "session": "keeper",
        "phase": "working",
    },
    {
        "run_id": "r-live-queued",
        "project": PROJECT,
        "session": "peer",
        "phase": "queued",
        # Every queued pointer dispatch writes carries the instant it joined
        # the queue, and the live view ranks the queued ones by it, so a
        # fixture without one is not a pointer the code reads.
        "queued_at": "2026-10-13T00:00:00+00:00",
    },
    {
        "run_id": "r-live-elsewhere",
        "project": "retired-reads-other",
        "session": "keeper",
        "phase": "working",
    },
)

#: The selections the captured answers were read with, as the successor takes
#: them: one entry per flag the retired listing accepted. A session on its own
#: names a caller without narrowing the listing, which is what the verb did
#: with it; ``--mine`` is the flag that narrows by it.
CREW_LIST_CASES: dict[str, tuple[str, ...]] = {
    "project": ("--project", LIVE_PROJECT),
    "phase": ("--project", LIVE_PROJECT, "--phase", "working"),
    "session": ("--project", LIVE_PROJECT, "--session", "keeper"),
    "mine": ("--project", LIVE_PROJECT, "--session", "keeper", "--mine"),
}

#: The pointer fields every captured row carries, so the classified fields are
#: values the fixture sets rather than whatever the running machine reports.
LIVE_POINTER_FIELDS = {
    "backend": "codex",
    "launch": "cli",
    "process_alive": True,
    "created_at": "1970-01-01T00:11:40+00:00",
}

#: The instant every clock in the classification path is held at, with the
#: pointer's creation stamp and its stream's mtime fixed around it, so
#: ``elapsed_seconds`` and ``log_age_seconds`` are fixture values.
FROZEN_NOW = 1000.0
LOG_MTIME = 900

_CLOCK_MODULES = (
    "reckon.crew.recovery_stream",
    "reckon.crew.recovery_liveness",
    "reckon.crew.recovery_classification",
    "reckon.crew.recovery_watch",
)

#: The answers the retired listing composed for each case at the base revision
#: this node's callers moved at, keyed by case and then by run id, holding the
#: fields the listing and its successor share: :data:`CLASSIFICATION_FIELDS`,
#: the set written down in ``tests/test_crew_surface_parity.py``. Captured by
#: driving the retired verb over :func:`live_listing`; written down rather than
#: recomputed so the comparison still runs once the verb is a tombstone.
CREW_LIST_BASE_ROWS: dict[str, dict[str, dict]] = {
    "project": {
        "r-live-working": {
            "budget_overrun": True,
            "budget_seconds": 200,
            "classification": "running",
            "elapsed_seconds": 300,
            "log_age_seconds": 100,
            "manifest_status": None,
            "next_action": "reckon crew observe --run r-live-working",
            "process_alive": True,
        },
        "r-live-queued": {
            "budget_overrun": True,
            "budget_seconds": 200,
            "classification": "queued",
            "elapsed_seconds": 300,
            "log_age_seconds": 100,
            "manifest_status": None,
            "next_action": "wait for a local lane slot",
            "process_alive": True,
        },
    },
    "phase": {
        "r-live-working": {
            "budget_overrun": True,
            "budget_seconds": 200,
            "classification": "running",
            "elapsed_seconds": 300,
            "log_age_seconds": 100,
            "manifest_status": None,
            "next_action": "reckon crew observe --run r-live-working",
            "process_alive": True,
        },
    },
    "session": {
        "r-live-working": {
            "budget_overrun": True,
            "budget_seconds": 200,
            "classification": "running",
            "elapsed_seconds": 300,
            "log_age_seconds": 100,
            "manifest_status": None,
            "next_action": "reckon crew observe --run r-live-working",
            "process_alive": True,
        },
        "r-live-queued": {
            "budget_overrun": True,
            "budget_seconds": 200,
            "classification": "queued",
            "elapsed_seconds": 300,
            "log_age_seconds": 100,
            "manifest_status": None,
            "next_action": "wait for a local lane slot",
            "process_alive": True,
        },
    },
    "mine": {
        "r-live-working": {
            "budget_overrun": True,
            "budget_seconds": 200,
            "classification": "running",
            "elapsed_seconds": 300,
            "log_age_seconds": 100,
            "manifest_status": None,
            "next_action": "reckon crew observe --run r-live-working",
            "process_alive": True,
        },
    },
}

#: The name the listing must not appear under as a command. The bare name stays
#: legitimate prose — the live view's own docstring describes what it narrowed —
#: while a ``reckon crew`` invocation of it is an answer about to expire.
CREW_LIST_COMMAND = "reckon crew list"


def _live_pointer(record: dict, tmp_path: Path) -> dict:
    log = tmp_path / "stream.jsonl"
    log.write_text("")
    os.utime(log, (LOG_MTIME, LOG_MTIME))
    return {
        **record,
        **LIVE_POINTER_FIELDS,
        "log_path": str(log),
        "manifest_path": str(tmp_path / "manifest.md"),
        "worktree": str(tmp_path / "worktree"),
        "node": {"id": record["run_id"], "plan": "plan-a", "time_budget": "200s"},
    }


@pytest.fixture()
def live_listing(tmp_path, monkeypatch) -> Path:
    """Pointers on disk spanning two projects, two phases and two sessions.

    Both sides of the comparison read one fleet through one crew home, and the
    clock is held still with the stream's mtime fixed, so the classified fields
    are values the fixture determines rather than readings of when the case ran.
    """
    from importlib import import_module

    from reckon import crew
    from reckon.crew import runs as runs_module

    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "crew-home"))
    for record in LIVE_POINTERS:
        crew._write_json(
            crew.pointer_path(record["run_id"]), _live_pointer(record, tmp_path)
        )
    for name in _CLOCK_MODULES:
        monkeypatch.setattr(import_module(name), "_utc_seconds", lambda: FROZEN_NOW)
    # The listing publishes a per-project watch stream; answer from the fixture
    # so the projected rows do not depend on a producer having run.
    monkeypatch.setattr(
        runs_module,
        "project_watch_visibility",
        lambda project, session=None: {"project": project, "session": session},
    )
    return tmp_path


def _live_rows(arguments: tuple[str, ...]) -> dict[str, dict]:
    """The successor's rows for one selection, keyed by run id."""
    payload = _cli(["live", *arguments])
    return {row["run_id"]: row for row in payload["runs"]}


def _classification(row: dict) -> dict:
    return {field: row.get(field) for field in CLASSIFICATION_FIELDS}


def _answered_empty(value: object) -> bool:
    """Whether a captured answer holds no value for its field.

    The successor's projection leaves such a field off the row, so this is what
    tells a field the view dropped from a value this run has none of.
    """

    return value in (None, "", [], {})


def test_the_live_rows_are_compared_over_the_written_down_field_set() -> None:
    """The field set the listing's answers are compared over is the shared one.

    Only fields both documents carry can be compared, and a set that dropped to
    one or two would let a successor that answered almost nothing pass.
    """
    assert {
        "classification",
        "process_alive",
        "elapsed_seconds",
        "log_age_seconds",
        "budget_seconds",
        "budget_overrun",
        "manifest_status",
        "next_action",
    } == CLASSIFICATION_FIELDS


def test_the_captured_listing_spans_every_filter(live_listing: Path) -> None:
    """The captured fixture can fail: each filter narrows, and nothing is empty.

    A fixture whose cases all answered the same rows would let a successor that
    dropped every filter agree with it, and an empty capture would agree with a
    successor that answered nothing at all.
    """
    captured = {
        case: _live_rows(arguments) for case, arguments in CREW_LIST_CASES.items()
    }
    for case, rows in captured.items():
        assert rows, case
    assert set(captured["project"]) == {"r-live-working", "r-live-queued"}
    assert set(captured["phase"]) == {"r-live-working"}
    assert set(captured["mine"]) == {"r-live-working"}
    assert set(captured["session"]) == set(captured["project"])


@pytest.mark.parametrize("case", sorted(CREW_LIST_CASES))
def test_crew_view_live_gives_the_listings_answer(
    case: str, live_listing: Path
) -> None:
    """Each captured listing's answer is what ``crew view live`` serves for it.

    Compared row by row per run id over :data:`CLASSIFICATION_FIELDS`, never as
    whole documents: the listing and the view compose their rows with different
    builders, so the shared field set is the contract. A field the listing
    answered with an empty value is left off the successor's row, because every
    list view routes through one projection and that projection omits a field
    with no value; the comparison reads the absence as the empty value it
    stands for. The rows were captured from the retired verb at the base
    revision the callers moved at, so the case still holds once the verb is a
    tombstone.
    """
    expected_rows = CREW_LIST_BASE_ROWS[case]
    rows = _live_rows(CREW_LIST_CASES[case])

    # The instrument check: a case compared over zero rows would pass on two
    # empty documents, so the fixture's own ids must be present on both sides.
    assert expected_rows, f"no captured answer for {case!r}"
    for run_id, expected in expected_rows.items():
        assert run_id in rows, f"successor answers no {run_id} for {case!r}"
        row = rows[run_id]
        missing = [
            field
            for field, value in expected.items()
            if not _answered_empty(value) and field not in row
        ]
        assert not missing, f"{run_id} carries no {missing}"
        serialised = [
            field
            for field, value in expected.items()
            if _answered_empty(value) and field in row
        ]
        assert not serialised, f"{run_id} serialises an empty {serialised}"
        assert _classification(row) == expected, run_id
    # The omission rule is exercised by the capture rather than assumed: a
    # capture whose every field held a value would leave the arm above unable
    # to fail.
    assert any(
        _answered_empty(value)
        for expected in expected_rows.values()
        for value in expected.values()
    ), "the captured answer exercises no omission"
    assert set(rows) == set(expected_rows), (sorted(rows), sorted(expected_rows))


def test_no_reckon_source_answers_with_the_listing_as_a_command() -> None:
    """No invocation under ``reckon/`` still sends a caller to the listing.

    A caller follows an answer literally, so a refusal handing back
    ``reckon crew list`` during the window before the tombstone hands over a
    name about to expire. The scan reads every file under ``reckon/``: the
    deadline refusal that carried this answer is a module literal, and the crew
    AGENTS map carried the same name in prose.
    """
    offenders: list[str] = []
    scanned: list[Path] = []
    for path in sorted(ROOT.glob("reckon/**/*")):
        if not path.is_file() or path.suffix in {".pyc", ".so"}:
            continue
        scanned.append(path)
        if CREW_LIST_COMMAND in path.read_text(encoding="utf-8", errors="replace"):
            offenders.append(f"{path.relative_to(ROOT)}: {CREW_LIST_COMMAND}")
    # The instrument check: the scan must have read the sources it reports on,
    # and the pattern must be able to match the shape an answer takes. A scan
    # that matched nothing because it read nothing is not a clean scan.
    assert len(scanned) > 1, scanned
    assert CREW_LIST_COMMAND in "reckon crew list --project <project>"
    assert not offenders, "\n".join(offenders)
