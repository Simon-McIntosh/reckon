"""Gate: a dispatch result agrees with what its launch did.

A dispatch either launches a run — and then its result names that run — or it
refuses — and then it leaves nothing behind: no live pointer, no worktree for
the node, and no worker that ``record_process_alive`` reports alive. A refusal
returned beside any one of those three is the defect this gate removes, because
it invites two harmful moves: a duplicate dispatch of a node already working,
and the removal of a worktree that looks orphaned from under a live process.

Three mechanisms produced such a refusal and are driven here:

* the exception handler around the post-launch tree snapshot — the boundary
  baseline runs after dispatch's own writes, so any failure there is
  post-pointer and the unwind then has to undo the run;
* the post-launch cleanup that once refused to remove the run's own worktree
  because the run's own new claim still held it (the claim is released first
  now, so the remover sees no claim);
* the survival check that once refused a supervisor which had, in fact,
  spawned its worker inside the two-second window.

The first two are exercised through a real dispatch with a stub launcher in a
temporary crew home, so the unwind runs against real worktrees and real live
pointers. The survival check is exercised on the record it reads, since a
spawned supervisor needs a separate process. Every case is a regression guard:
the mechanism is driven at the node's head whether or not the path still
refuses, and the manifest records which commit removed it where it no longer
does.

Every ``ok: false`` result the ``reckon crew dispatch`` command emits — live and
``--dry-run`` alike — must carry ``error`` and ``detail`` as non-empty strings,
so a caller keying on ``error`` never reads a refusal as success. The contract
validation refusal is the one that was silent, carrying its reason only under
``validation.findings``.

Nothing here reaches the real pointer directory: a temporary ``RECKON_HOME``
relocates every state path. The real directory is asserted untouched, because an
isolated read does not prove an isolated write.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew, crew_dispatch_commands
from reckon.crew.node import PlanReviewMissingError
from reckon.crew.runs import list_live, record_process_alive

dispatch_module = importlib.import_module("reckon.crew.dispatch")

PROJECT = "dispatch-result-fixture"
NODE_ID = "dispatch-result-matches-launch"
SESSION = "session-dispatch-result"
RUN_ID = "r-20260101T000000000000-a-launched-run"
REAL_LIVE = Path.home() / ".config" / "reckon" / "crew" / "live"

# A CLI backend so a dispatch cuts a worktree and writes a live pointer rather
# than handing back an in-harness directive. Every launcher is supplied by the
# test, so no command named here is ever executed.
CONFIG: dict[str, Any] = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": False,
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}

DONE_WHEN = "pytest reports the dispatch result matches its launch"
# An unresolved template placeholder, so contract validation fails.
PLACEHOLDER_DONE_WHEN = "pytest reports <the-measure> passing"


@dataclasses.dataclass
class Result:
    """One dispatch's outcome, in the shape a caller keys on."""

    ok: bool
    run_id: str = ""
    error: str = ""
    detail: str = ""


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)


def _plan_document() -> str:
    return (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<meta name="plan-title" content="fixture">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-impl" content="0">'
        '<meta name="plan-version" content="0">'
        '</head><body><h2 id="s6">A result matches its launch</h2></body></html>'
    )


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A temporary crew home and a repository that looks like a reckon mount."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repo = tmp_path / "repo"
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "fixture.html").write_text(_plan_document(), encoding="utf-8")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "docs"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        _git(repo, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    return config_home, repo


@pytest.fixture(autouse=True)
def real_live_directory_is_not_a_fixture_target() -> Any:
    """No case may write this fixture's pointer into the real crew home."""

    def fixture_pointers() -> list[str]:
        found = []
        for path in REAL_LIVE.glob("*.json"):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if PROJECT in text or NODE_ID in text:
                found.append(path.name)
        return found

    assert fixture_pointers() == []
    yield
    assert fixture_pointers() == []


def _node(config_home: Path, *, done_when: str = DONE_WHEN) -> crew.TaskNode:
    return crew.TaskNode(
        id=NODE_ID,
        goal="record one dispatch whose result matches its launch",
        plan="fixture",
        section="s6",
        role="implement",
        spec_level="guided",
        done_when=done_when,
        write_paths=["src/dispatched.py"],
        time_budget="20m",
        manifest_path=str(config_home / "manifests" / f"{NODE_ID}.md"),
    )


def _live_launcher() -> Any:
    """A launcher that names this process, so a leftover pointer reads alive.

    ``_signal_process_group`` refuses to signal the caller's own pid, so the
    unwind skips the group signal rather than taking the test down, and a
    pointer that survived a refusal would name a genuinely live worker.
    """

    def launch(*_args: Any, **_kwargs: Any) -> int:
        return os.getpid()

    return launch


def _run_dispatch(
    home: tuple[Path, Path],
    *,
    launcher: Any,
    done_when: str = DONE_WHEN,
    member: str = "",
) -> Result:
    """Drive one dispatch, returning its outcome rather than raising."""
    config_home, repo = home
    try:
        record = crew.dispatch(
            node=_node(config_home, done_when=done_when),
            project=PROJECT,
            repo=repo,
            config=CONFIG,
            session=SESSION,
            launcher=launcher,
            member=member,
            check_budget=False,
        )
    except crew.CrewError as exc:
        return Result(ok=False, error=type(exc).__name__, detail=str(exc))
    return Result(ok=True, run_id=str(record["run_id"]))


# ── The reads a result must satisfy ─────────────────────────────────────────


def _live_pointers_naming(node_id: str) -> list[str]:
    naming = []
    for record in list_live(project=PROJECT):
        node = record.get("node") or {}
        if node.get("id") == node_id or node_id in str(record.get("run_id", "")):
            naming.append(str(record.get("run_id")))
    return naming


def _live_workers_naming(node_id: str) -> list[str]:
    """Run ids of live pointers whose recorded worker is still alive."""
    alive = []
    for record in list_live(project=PROJECT):
        node = record.get("node") or {}
        if node.get("id") != node_id:
            continue
        if record_process_alive(record) is True:
            alive.append(str(record.get("run_id")))
    return alive


def _worktrees_naming(repo: Path, node_id: str) -> list[str]:
    listing = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [
        line.removeprefix("worktree ")
        for line in listing.splitlines()
        if line.startswith("worktree ") and node_id in line
    ]


def _assert_result_matches_launch(
    result: Result, home: tuple[Path, Path], node_id: str = NODE_ID
) -> None:
    """Either the launched run is named, or nothing of the run remains."""
    _config_home, repo = home
    if result.ok:
        assert result.run_id, "a launched dispatch must name the run it launched"
        return
    assert result.error, "a refusal must carry an error key"
    assert _live_pointers_naming(node_id) == [], "a refusal left a live pointer"
    assert _worktrees_naming(repo, node_id) == [], "a refusal left a worktree"
    assert _live_workers_naming(node_id) == [], "a refusal left a live worker"


# ── Positive control: the reads do see a launched run ───────────────────────


def test_a_launched_dispatch_names_its_run(home: tuple[Path, Path]) -> None:
    """Without this, a clean assertion on a refusal could pass on a blind read.

    Every read the refusal assertion depends on must name a present run when one
    was launched: the live pointer, the worktree, and a worker the record's own
    pid reports alive.
    """
    result = _run_dispatch(home, launcher=_live_launcher())

    assert result.ok is True
    assert result.run_id
    _assert_result_matches_launch(result, home)
    assert _live_pointers_naming(NODE_ID) == [result.run_id]
    assert _live_workers_naming(NODE_ID) == [result.run_id]
    assert _worktrees_naming(home[1], NODE_ID) != []


# ── Mechanism 1: the handler around the post-launch tree snapshot ───────────


def test_a_post_launch_snapshot_failure_leaves_no_trace(
    home: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The snapshot runs after dispatch's own writes, so it is post-pointer."""
    monkeypatch.setattr(
        dispatch_module,
        "_repository_tree_snapshot",
        lambda *_a, **_k: (_ for _ in ()).throw(crew.CrewError("snapshot failed")),
    )

    result = _run_dispatch(home, launcher=_live_launcher())

    assert result.ok is False
    _assert_result_matches_launch(result, home)


# ── Mechanism 2: the cleanup that refused to remove the run's worktree ──────


def test_the_unwind_releases_the_claim_before_it_removes_the_worktree(
    home: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run's own claim must be gone before its worktree is removed.

    The live-claim read is what refused the removal before the ordering was
    fixed. Here the real remover is wrapped so it asserts the claim is already
    released at the moment it runs: if the order regressed, the wrapper fails
    and the pointer the removal could not clear survives.
    """
    real_remove = dispatch_module._remove_worktree

    def remove_after_release(repo: Path, path: str) -> None:
        assert _live_pointers_naming(NODE_ID) == [], (
            "the worktree was removed while the run's own claim still held it"
        )
        real_remove(repo, path)

    monkeypatch.setattr(dispatch_module, "_remove_worktree", remove_after_release)
    monkeypatch.setattr(
        dispatch_module,
        "_repository_tree_snapshot",
        lambda *_a, **_k: (_ for _ in ()).throw(crew.CrewError("snapshot failed")),
    )

    result = _run_dispatch(home, launcher=_live_launcher())

    assert result.ok is False
    _assert_result_matches_launch(result, home)


# ── Mechanism 3: the survival check accepts a supervisor that launched ──────


def test_the_survival_check_accepts_a_supervisor_that_launched(
    tmp_path: Path,
) -> None:
    """A record naming the spawned worker is the receipt of a launch that ran."""
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    (run_directory / dispatch_module.EXIT_RECORD_NAME).write_text(
        json.dumps({"run_id": RUN_ID, "worker_pid": 4242}), encoding="utf-8"
    )

    assert (
        dispatch_module._confirm_supervisor_survived(os.getpid(), run_directory, RUN_ID)
        == RUN_ID
    )


def test_the_survival_check_refuses_a_supervisor_that_launched_no_worker(
    tmp_path: Path,
) -> None:
    """The refusal is reserved for a supervisor whose exit names no worker."""
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    (run_directory / dispatch_module.EXIT_RECORD_NAME).write_text(
        json.dumps(
            {"run_id": RUN_ID, "worker_pid": None, "detail": "the worker never spawned"}
        ),
        encoding="utf-8",
    )

    with pytest.raises(crew.CrewError) as refusal:
        dispatch_module._confirm_supervisor_survived(os.getpid(), run_directory, RUN_ID)
    assert RUN_ID in str(refusal.value)


# ── An unresolvable --member is refused, never auto-derived ─────────────────


def test_an_unresolvable_member_is_refused_with_nothing_bound(
    home: tuple[Path, Path],
) -> None:
    """A named member is refused when unknown — never silently substituted.

    A substitution would bind the run to a session member the caller did not
    name, so the test asserts both the refusal and that nothing of the run was
    created: no pointer carries a member, because no run exists.
    """
    result = _run_dispatch(home, launcher=_live_launcher(), member="ghost-member")

    assert result.ok is False
    assert "ghost-member" in result.detail
    _assert_result_matches_launch(result, home)
    assert _live_pointers_naming(NODE_ID) == []


# ── Every ok: false the dispatch command emits carries error and detail ─────


def _dispatch_cli(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    extra: list[str] | None = None,
    done_when: str = DONE_WHEN,
) -> Any:
    """One ``reckon crew dispatch`` issuing from the operator's hand."""
    monkeypatch.setattr(crew_dispatch_commands, "_resolved_flight", lambda *a, **k: CONFIG)
    arguments = [
        "crew",
        "dispatch",
        "--project",
        PROJECT,
        "--plan",
        "fixture",
        "--section",
        "s6",
        "--spec-level",
        "guided",
        "--node",
        NODE_ID,
        "--goal",
        "record one dispatch whose result matches its launch",
        "--done-when",
        done_when,
        "--role",
        "implement",
        "--session",
        SESSION,
        "--repo",
        str(repo),
        "--write-path",
        "src/dispatched.py",
    ]
    arguments += list(extra or ())
    return CliRunner().invoke(cli_module.main, arguments)


def _payload(result: Any) -> dict[str, Any]:
    """The emitted JSON, ignoring the human-readable line click also writes."""
    for line in result.output.splitlines():
        stripped = line.strip()
        if stripped.startswith("{"):
            return json.loads(stripped)
    raise AssertionError(f"no JSON result on stdout: {result.output!r}")


def _assert_refusal_is_legible(payload: dict[str, Any]) -> None:
    assert payload["ok"] is False
    for key in ("error", "detail"):
        value = payload.get(key)
        assert isinstance(value, str) and value.strip(), (
            f"refusal {payload.get('error')!r} carries no non-empty {key!r}"
        )


def test_a_contract_validation_refusal_carries_error_and_detail(
    home: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect: the dry-run verdict carried its reason only under findings."""
    _config_home, repo = home

    result = _dispatch_cli(
        repo, monkeypatch, extra=["--dry-run"], done_when=PLACEHOLDER_DONE_WHEN
    )
    payload = _payload(result)

    assert result.exit_code == 2, result.output
    _assert_refusal_is_legible(payload)
    assert payload["error"] == "contract-validation"
    # The failed property is read from the structured findings, so the case
    # holds on the property that failed rather than on the sentence the refusal
    # happens to render today.
    failed = [
        finding
        for finding in payload["validation"]["findings"]
        if finding.get("property") == "fully-specified"
    ]
    assert failed, "the refusal names no failed property"
    assert all(str(finding.get("detail", "")).strip() for finding in failed), (
        "the failed property carries no detail"
    )


# Every refusal class the live dispatch path can raise, built with the minimum
# its constructor needs. A refusal that reaches the caller without error and
# detail routes a caller keying on error straight into a duplicate dispatch.
def _refusal_factories() -> dict[str, Any]:
    return {
        "plan-unavailable": lambda: crew.PlanVisibilityError("the plan is unreadable"),
        "plan-review-missing": lambda: PlanReviewMissingError(
            "the plan section is unreviewed"
        ),
        "budget-hold": lambda: crew.BudgetHold(
            {"backend": "alpha", "reason": "the window is spent"}
        ),
        "competence-refusal": lambda: crew.CompetenceLimit(
            {
                "estimated_hours": 9,
                "competence_horizon_hours": 4,
                "target_size_hours": 4,
            }
        ),
        "unreconciled-runs": lambda: crew.UnreconciledRuns(
            [{"run_id": "r-1", "next_action": "promote it"}], "2h"
        ),
        "scope-conflict": lambda: crew.ScopeConflict(
            run_id="r-peer",
            node_id="peer-node",
            candidate_path="src/dispatched.py",
            claimed_path="src/",
        ),
        "watcher-required": lambda: crew.WatcherRequired(
            PROJECT,
            {"ensure_line": "reckon crew watch", "attach_line": "reckon crew follow"},
            session=SESSION,
        ),
        "member-in-flight": lambda: crew.MemberInFlight("some-member", "r-run"),
        "dispatch-refused": lambda: crew.CrewError("the dispatch could not proceed"),
        "not-dispatchable": lambda: crew.CrewError(
            "node is not dispatchable: the goal names no deliverable"
        ),
    }


@pytest.mark.parametrize("name", sorted(_refusal_factories()))
def test_every_live_refusal_carries_error_and_detail(
    home: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    _config_home, repo = home
    factory = _refusal_factories()[name]

    def raise_refusal(**_kwargs: Any) -> Any:
        raise factory()

    monkeypatch.setattr(crew, "dispatch", raise_refusal)

    result = _dispatch_cli(repo, monkeypatch)
    payload = _payload(result)

    _assert_refusal_is_legible(payload)


@pytest.mark.parametrize(
    "name",
    [
        "plan-unavailable",
        "plan-review-missing",
        "competence-refusal",
        "watcher-required",
        "dispatch-refused",
    ],
)
def test_every_dry_run_refusal_carries_error_and_detail(
    home: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    _config_home, repo = home
    factory = _refusal_factories()[name]

    def raise_refusal(**_kwargs: Any) -> Any:
        raise factory()

    monkeypatch.setattr(crew, "plan_dispatch", raise_refusal)

    result = _dispatch_cli(repo, monkeypatch, extra=["--dry-run"])
    payload = _payload(result)

    _assert_refusal_is_legible(payload)
