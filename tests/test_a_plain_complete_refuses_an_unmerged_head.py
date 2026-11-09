"""A plain complete refuses a reviewed head the primary branch does not carry.

``crew complete --gate passed`` promotes a run as landed, and the ledger then
reads work the primary branch never received. A coordinator hit this after
``crew merge --run`` refused: the complete still promoted, deleted the live
pointer, and left the merge unable to find the run it was asked to land. The
plain-complete path therefore asks the remote branch, through the module that
lands there, whether the run's reviewed head is reachable from it, and refuses
naming the head and the branch when it is not.

The arms below each hold a control, because a guard that never fires passes the
same assertions as one that always refuses:

* an unmerged head refuses, and the same run with an unreachable remote or a
  remote carrying no primary ref refuses naming that cause;
* a head on the branch, a ``--land`` complete, a ``crew merge --run`` and a
  failed gate promote or land as before, so the refused paths are not the only
  paths;
* a review run, whose pointer carries the reviewed node's head by design,
  promotes and reads no remote;
* a head fast-forwarded on local ``main`` but never pushed is refused, which is
  the difference between reading the remote and reading the checkout.

The declared mutation removes the guard: with ``RECKON_COMPLETE_SKIP_HEAD
_CHECK=1`` the plain complete promotes an unmerged head and removes the
pointer, which is the red log the manifest names.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew_run_commands
from reckon.crew import merge
from reckon.crew.runs import _write_json, pointer_path
from tests.helpers.git import git as _git
from tests.helpers.landing_repository import (
    PLAN,
    PROJECT,
    _complete_land,
    _write_origin_and_checkout,
)

MARKER = "the reviewed change survives the merge\n"


def _skip_the_guard_if_declared(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply the declared mutation: promote without asking the remote.

    The name is patched in the module whose code resolves it. ``crew_complete``
    lives in ``reckon.crew_run_commands``, so its global lookup reads that
    module's namespace; a patch applied to ``reckon.cli`` would install a name
    nothing calls and the guard would stay in place.
    """
    if not os.environ.get("RECKON_COMPLETE_SKIP_HEAD_CHECK"):
        return
    monkeypatch.setattr(
        crew_run_commands,
        "_refuse_a_plain_complete_off_the_branch",
        lambda *a, **k: None,
    )


class _Fixture:
    """A run whose worktree holds one commit the remote branch may or may not."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.home = tmp_path / "config"
        self.home.mkdir()
        monkeypatch.setenv("RECKON_HOME", str(self.home))
        self.origin, self.repository = _write_origin_and_checkout(tmp_path)
        (self.home / "mounts.json").write_text(
            json.dumps({PROJECT: str(self.repository / "docs")}), encoding="utf-8"
        )
        self.tmp_path = tmp_path
        self.counter = 0
        self.base = _git(self.repository, "rev-parse", "main")
        self.last_file = "reviewed-1.txt"

    def worktree_at(self, head: str, label: str) -> Path:
        worktree = self.tmp_path / "worktrees" / label
        worktree.parent.mkdir(parents=True, exist_ok=True)
        _git(self.repository, "worktree", "add", "--detach", str(worktree), head)
        return worktree

    def committed_head(self) -> str:
        """Commit one file on a side branch, leaving ``main`` checked out."""
        self.counter += 1
        branch = f"reviewed-{self.counter}"
        _git(self.repository, "checkout", "-q", "-b", branch)
        name = f"reviewed-{self.counter}.txt"
        (self.repository / name).write_text(MARKER, encoding="utf-8")
        _git(self.repository, "add", name)
        _git(self.repository, "commit", "-q", "-m", "add the reviewed change")
        head = _git(self.repository, "rev-parse", "HEAD")
        _git(self.repository, "checkout", "-q", "main")
        self.last_file = name
        return head

    def run(self, head: str, *, role: str = "implement", label: str = "node-a") -> dict:
        self.counter += 1
        run_id = f"r-20261109T1200{self.counter:02d}00000000-{label}"
        worktree = self.worktree_at(head, f"{label}-{self.counter}")
        manifest = self.tmp_path / "manifests" / f"{run_id}.md"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text("node: node-a\nstatus: complete\n", encoding="utf-8")
        gate_log = self.tmp_path / f"gate-{self.counter}.log"
        gate_log.write_text("# command: true\nEXIT=0\n", encoding="utf-8")
        _write_json(
            pointer_path(run_id),
            {
                "run_id": run_id,
                "project": PROJECT,
                "repo": str(self.repository),
                "worktree": str(worktree),
                "launch": "in-harness",
                "role": role,
                "member": "worker-a",
                "backend": "native",
                "created_at": "2026-11-09T12:00:00Z",
                "base_sha": self.base,
                "manifest_path": str(manifest),
                "node": {
                    "id": "node-a",
                    "plan": PLAN,
                    "section": "§5",
                    "time_budget": "35m",
                    "write_paths": [self.last_file],
                },
            },
        )
        return {
            "origin": self.origin,
            "repository": self.repository,
            "worktree": worktree,
            "run_id": run_id,
            "head": head,
            "gate_log": gate_log,
            "pointer": pointer_path(run_id),
        }


def _complete_plain(
    run: dict, *extra: str, gate: str = "passed", gate_exit_status: str = "0"
):
    return CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "complete",
            "--run",
            run["run_id"],
            "--gate",
            gate,
            "--commit",
            run["head"],
            "--outcome",
            "the reviewed work is recorded",
            "--checkout-path",
            str(run["repository"]),
            "--no-impl-change",
            "the fixture changes no plan",
            "--gate-command",
            "true",
            "--gate-exit-status",
            gate_exit_status,
            "--gate-log-path",
            str(run["gate_log"]),
            *extra,
        ],
    )


def test_a_plain_complete_on_an_unmerged_head_refuses_and_leaves_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The head is not on the branch, and nothing about the run moves."""
    _skip_the_guard_if_declared(monkeypatch)
    fixture = _Fixture(tmp_path, monkeypatch)
    head = fixture.committed_head()
    run = fixture.run(head)
    pointer_before = run["pointer"].read_bytes()
    main_before = _git(run["repository"], "rev-parse", "main")
    remote_before = _git(run["origin"], "rev-parse", "main")

    result = _complete_plain(run)

    assert result.exit_code != 0, result.output
    assert head in result.output
    assert "origin/main" in result.output
    assert run["pointer"].read_bytes() == pointer_before
    assert _git(run["repository"], "rev-parse", "main") == main_before
    assert _git(run["origin"], "rev-parse", "main") == remote_before
    assert not (run["repository"] / "docs" / "state" / PROJECT / "crew.json").exists()


def test_a_plain_complete_on_a_head_the_branch_carries_promotes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control for the refusal: the same call on a head that is on main."""
    fixture = _Fixture(tmp_path, monkeypatch)
    head = fixture.committed_head()
    _git(fixture.repository, "merge", "--ff-only", "-q", head)
    _git(fixture.repository, "push", "-q", "origin", "main")
    run = fixture.run(head)

    result = _complete_plain(run)

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["ok"] is True, report
    assert not run["pointer"].exists()


def test_a_land_complete_on_an_unmerged_head_is_unaffected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--land`` merges the head, so the refusal has nothing to refuse."""
    fixture = _Fixture(tmp_path, monkeypatch)
    run = fixture.run(fixture.committed_head())

    result = _complete_land(run)

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["steps"]["land"]["pushed"] is True, report
    assert MARKER.strip() in _git(run["origin"], "show", "main:reviewed-1.txt")


def _merge_argv(run: dict) -> list[str]:
    return [
        "crew",
        "merge",
        "--project",
        PROJECT,
        "--run",
        run["run_id"],
        "--head",
        run["head"],
        "--commit",
        run["head"],
        "--no-impl-change",
        "the fixture changes no plan",
        "--gate-command",
        "true",
        "--gate-exit-status",
        "0",
        "--gate-log-path",
        str(run["gate_log"]),
        "--message",
        "land the reviewed head",
        "--body",
        "the reviewed head reaches every reader",
    ]


def test_a_merge_run_on_an_unmerged_head_still_lands_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``crew merge --run`` must still land a head the branch has not got."""
    fixture = _Fixture(tmp_path, monkeypatch)
    run = fixture.run(fixture.committed_head())

    landed = CliRunner().invoke(cli_module.main, _merge_argv(run))

    assert landed.exit_code == 0, landed.output
    assert MARKER.strip() in _git(run["origin"], "show", "main:reviewed-1.txt")

    # The control: put this same guard in the dry-run list both paths share and
    # the merge is refused, which is the placement the refusal must not take.
    def _refuse(*args, **kwargs):
        raise merge.MergeError("the reviewed head is not on the primary branch")

    monkeypatch.setattr(merge, "require_promotable_head", _refuse)
    second = fixture.run(fixture.committed_head())
    refused = CliRunner().invoke(cli_module.main, _merge_argv(second))
    assert refused.exit_code != 0, refused.output
    assert "not on the primary branch" in refused.output


def test_a_head_fast_forwarded_locally_but_not_pushed_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reading local ``HEAD`` would pass this head; reading the remote refuses."""
    fixture = _Fixture(tmp_path, monkeypatch)
    head = fixture.committed_head()
    _git(fixture.repository, "merge", "--ff-only", "-q", head)
    run = fixture.run(head)
    assert _git(run["origin"], "rev-parse", "main") != head

    result = _complete_plain(run)

    assert result.exit_code != 0, result.output
    assert head in result.output
    assert run["pointer"].exists()

    # The control: reading the checkout's own HEAD promotes it, which is the
    # mistake the remote read exists to avoid.
    monkeypatch.setattr(
        merge,
        "_read_remote_head",
        lambda *args, **kwargs: (
            _git(fixture.repository, "rev-parse", "HEAD"),
            None,
            "",
        ),
    )
    promoted = _complete_plain(run)

    assert promoted.exit_code == 0, promoted.output
    assert json.loads(promoted.output)["ok"] is True


def test_a_failed_gate_on_an_unmerged_head_promotes_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gate that did not pass records work that is not to land."""
    fixture = _Fixture(tmp_path, monkeypatch)
    run = fixture.run(fixture.committed_head())

    result = _complete_plain(
        run,
        "--failure-classification",
        "work-rejected",
        gate="failed",
        gate_exit_status="1",
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["ok"] is True
    assert not run["pointer"].exists()


def test_a_review_run_promotes_without_reading_the_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reviewer's pointer carries the reviewed head, which is off the branch."""
    fixture = _Fixture(tmp_path, monkeypatch)
    run = fixture.run(fixture.committed_head(), role="review")

    seen: list[tuple[str, ...]] = []
    real = merge._run

    def _recording(repo, *args, **kwargs):
        seen.append(tuple(str(arg) for arg in args))
        return real(repo, *args, **kwargs)

    monkeypatch.setattr(merge, "_run", _recording)
    result = _complete_plain(
        run, "--no-commit", "a review run cites the reviewed node's head, not its own"
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["ok"] is True
    assert not [argv for argv in seen if argv and argv[0] in {"fetch", "ls-remote"}]

    # The control: keying the guard on the gate alone refuses the review run.
    from reckon.crew import recovery_review_delivery

    monkeypatch.setattr(
        recovery_review_delivery, "_is_review_run", lambda record: False
    )
    second = fixture.run(fixture.committed_head(), role="review", label="node-b")
    refused = _complete_plain(second)

    assert refused.exit_code != 0, refused.output
    assert second["head"] in refused.output


def test_an_unreachable_remote_refuses_naming_the_failed_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A remote that cannot be read is a refusal, not a silent promotion."""
    fixture = _Fixture(tmp_path, monkeypatch)
    run = fixture.run(fixture.committed_head())
    _git(
        run["repository"],
        "remote",
        "set-url",
        "origin",
        str(tmp_path / "no-such-remote.git"),
    )

    result = _complete_plain(run)

    assert result.exit_code != 0, result.output
    assert "could not be read" in result.output
    assert "origin" in result.output
    assert run["pointer"].exists()


def test_a_remote_with_no_primary_ref_refuses_naming_the_missing_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty answer from a reachable remote is a missing branch, not assent."""
    fixture = _Fixture(tmp_path, monkeypatch)
    run = fixture.run(fixture.committed_head())
    _git(run["origin"], "update-ref", "-d", "refs/heads/main")

    result = _complete_plain(run)

    assert result.exit_code != 0, result.output
    assert "carries no main" in result.output
    assert run["pointer"].exists()

    # The control: reading that empty answer as promotable promotes the run.
    monkeypatch.setattr(
        merge,
        "head_on_remote_branch",
        lambda *args, **kwargs: {
            "remote": "origin",
            "branch": "main",
            "head": run["head"],
            "remote_head": "",
            "on_branch": True,
            "cause": None,
            "detail": "",
        },
    )
    promoted = _complete_plain(run)

    assert promoted.exit_code == 0, promoted.output
    assert json.loads(promoted.output)["ok"] is True


def test_the_answer_distinguishes_a_missing_branch_from_an_unreachable_remote(
    tmp_path: Path,
) -> None:
    """The two causes are different answers, and neither reads as ancestry."""
    origin, repository = _write_origin_and_checkout(tmp_path)
    head = _git(repository, "rev-parse", "main")

    present = merge.head_on_remote_branch(repository, head)
    assert present["cause"] is None
    assert present["on_branch"] is True

    _git(origin, "update-ref", "-d", "refs/heads/main")
    missing = merge.head_on_remote_branch(repository, head)
    assert missing["cause"] == merge.REMOTE_BRANCH_MISSING
    assert missing["on_branch"] is None

    _git(repository, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    unreachable = merge.head_on_remote_branch(repository, head)
    assert unreachable["cause"] == merge.REMOTE_UNREACHABLE
    assert unreachable["on_branch"] is None
    assert unreachable["detail"]
