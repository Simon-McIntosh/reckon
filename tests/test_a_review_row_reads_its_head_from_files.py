"""A review row reads its run's head from git's files, not a subprocess.

A derivation resolves each live run's reviewed head once, and on a detached
worktree the answer is already written down: the git directory's ``HEAD`` file
holds the revision, and an attached checkout names a ref whose loose file holds
it. Reading those two small files avoids the ``git rev-parse HEAD`` child that,
measured once per review row, dominated the derivation's child wait.

These tests pin the facts that makes true: one derivation over detached review
rows spawns no ``git rev-parse HEAD``, every row still reports the head git
reports, a ref kept solely in ``packed-refs`` still resolves through git, and a
commit landing in a worktree moves the row's head on the next derivation. The
subprocess is counted through the module that owns the review-row read, so a
call made from anywhere else in a derivation cannot stand in for it.

The counts are taken over one synthesised crew state under a temporary
configuration home, so no reader reaches the operator's own crew home.
"""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

import pytest

from reckon.crew import obligation_snapshot, runs
from reckon.crew import review as review_module
from tests.helpers.git import git as _git

subject_module = importlib.import_module("reckon.crew.recovery_review_subject")
obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "review-row-head-fixture"
SESSION = "coordinator-fixture"
ROWS = 6
OBSERVED_AT = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def _commit(repository: Path, name: str, body: str) -> str:
    (repository / name).write_text(body, encoding="utf-8")
    _git(repository, "add", name)
    _git(repository, "commit", "-q", "-m", f"test: {body.strip()}")
    return _git(repository, "rev-parse", "HEAD")


def _seed_repo(root: Path, config_home: Path) -> None:
    """A bare project repository under a temporary configuration home."""
    (root / "docs" / "state" / PROJECT / "runs").mkdir(parents=True)
    (root / "docs" / "plans").mkdir()
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the review-row-head fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )


def _write_run(
    config_home: Path,
    run_id: str,
    *,
    root: Path,
    worktree: Path,
    base_sha: str,
    reviewed_head: str,
) -> None:
    """One live run whose stored review describes a revision it has moved past.

    The stored record names an earlier revision, so the run reads
    review-missing and the head it carries must be resolved by the derivation.
    """
    manifest = config_home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {run_id}\nstatus: complete\ncommits: [{reviewed_head}]\n",
        encoding="utf-8",
    )
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": SESSION,
            "repo": str(root),
            "worktree": str(worktree),
            "base_sha": base_sha,
            "process_alive": False,
            "launch": "in-harness",
            "role": "implement",
            "manifest_path": str(manifest),
            "node": {
                "id": run_id,
                "plan": "fixture-plan",
                "section": "fixture-section",
                "write_paths": ["seed.txt"],
            },
        },
    )
    emitted = "\n".join(
        f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
    )
    record = review_module.parse_review(emitted)
    record.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": f"review-of-{run_id}",
            "reviewed_base_sha": base_sha,
            "reviewed_head_sha": reviewed_head,
        }
    )
    review_module.store_review(record)


class _RevParseSpy:
    """Record every argv the module hands to ``subprocess.run``, then run it.

    The binding is swapped on the module that owns the review-row read, so a
    ``git rev-parse HEAD`` spawned from anywhere else in a derivation is not
    counted here: this measures the call the review row used to make.
    """

    def __init__(self, module: Any) -> None:
        self._module = module
        self.commands: list[list[str]] = []

    def __enter__(self) -> Self:
        self._real = self._module.subprocess
        spy = self

        class _Recorder:
            def run(self, argv: Any, *args: Any, **kwargs: Any) -> Any:
                spy.commands.append([str(part) for part in argv])
                return spy._real.run(argv, *args, **kwargs)

        self._module.subprocess = _Recorder()
        return self

    def __exit__(self, *exc: object) -> None:
        self._module.subprocess = self._real

    @property
    def head_calls(self) -> list[list[str]]:
        """The recorded calls that asked git for a checkout's ``HEAD``."""
        return [
            command
            for command in self.commands
            if command[:3]
            == [
                "git",
                "rev-parse",
                "HEAD",
            ]
        ]


@pytest.fixture()
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A project of live runs, each in a detached worktree, each owing a review."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    config_home = tmp_path / "config"
    config_home.mkdir()
    root = tmp_path / "repo"
    _seed_repo(root, config_home)
    base = _git(root, "rev-parse", "HEAD")
    worktrees: list[Path] = []
    for index in range(ROWS):
        run_id = f"run-{index:02d}"
        worktree = tmp_path / f"worktree-{index:02d}"
        _git(root, "worktree", "add", "-q", "--detach", str(worktree), base)
        _commit(worktree, f"work-{index:02d}.txt", f"work {index}\n")
        _write_run(
            config_home,
            run_id,
            root=root,
            worktree=worktree,
            base_sha=base,
            reviewed_head=base,
        )
        worktrees.append(worktree)
    monkeypatch.setattr(obligations_module, "_utc_now", lambda: OBSERVED_AT)
    return {
        "home": config_home,
        "repo": root,
        "base": base,
        "worktrees": worktrees,
    }


def _review_rows() -> list[dict[str, Any]]:
    """The derivation's rows that carry a resolved reviewed head."""
    items = obligations_module.obligations(PROJECT, SESSION)["obligations"]
    return [item for item in items if item.get("head")]


def test_one_derivation_spawns_no_rev_parse_for_detached_rows(
    fleet: dict[str, Any],
) -> None:
    """One fleet_state spawns no git rev-parse HEAD for the detached rows."""
    with _RevParseSpy(subject_module) as spy:
        obligation_snapshot.fleet_state(PROJECT, now=OBSERVED_AT)
    # Positive control: the same spy sees a rev-parse HEAD when one genuinely
    # runs, so the zero below is the file read rather than a blind instrument.
    # A tree with no git directory cannot answer from files and falls through.
    plain = fleet["repo"] / "not-a-checkout"
    plain.mkdir()
    with _RevParseSpy(subject_module) as control:
        subject_module._reviewed_run_head({"worktree": str(plain)})
    assert len(control.head_calls) == 1, (
        "the spy did not see the one rev-parse HEAD the fallback runs, so it"
        " cannot count the calls below"
    )
    assert spy.head_calls == [], (
        f"one derivation spawned {len(spy.head_calls)} git rev-parse HEAD calls"
        f" over {ROWS} detached review rows"
    )


def test_every_row_reports_the_head_git_reports(fleet: dict[str, Any]) -> None:
    """Every review row reports the head ``git rev-parse HEAD`` reports."""
    rows = _review_rows()
    assert len(rows) == ROWS, (
        f"the fixture produced {len(rows)} resolved review rows over"
        f" {ROWS} runs, so the comparison below would prove nothing"
    )
    reported = {str(row["run_id"]): str(row["head"]) for row in rows}
    for index, worktree in enumerate(fleet["worktrees"]):
        run_id = f"run-{index:02d}"
        expected = _git(worktree, "rev-parse", "HEAD")
        assert reported.get(run_id) == expected, (
            f"{run_id} reports head {reported.get(run_id)!r} but git reports"
            f" {expected!r}"
        )


def test_head_resolves_when_the_ref_lives_only_in_packed_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A head held only in packed-refs still resolves, through git."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    config_home = tmp_path / "config"
    config_home.mkdir()
    root = tmp_path / "repo"
    _seed_repo(root, config_home)
    base = _git(root, "rev-parse", "HEAD")
    worktree = tmp_path / "branch-worktree"
    _git(root, "worktree", "add", "-q", "-b", "feature", str(worktree), base)
    # Loose refs are folded into packed-refs, so the worktree's HEAD names a ref
    # no loose file holds and the files alone cannot answer.
    _git(root, "pack-refs", "--all")
    assert not (root / ".git" / "refs" / "heads" / "feature").exists(), (
        "the loose ref survived pack-refs, so this is not the packed-refs case"
    )
    assert (
        _git(worktree, "symbolic-ref", "-q", "HEAD") == "ref: refs/heads/feature"
        or _git(worktree, "symbolic-ref", "-q", "HEAD") == "refs/heads/feature"
    ), "the worktree HEAD does not name the packed branch"
    assert subject_module._reviewed_run_head({"worktree": str(worktree)}) == base, (
        "a head held only in packed-refs did not resolve through git"
    )


def test_a_commit_moves_the_row_head_on_the_next_derivation(
    fleet: dict[str, Any],
) -> None:
    """A commit landing in one worktree changes that row's head next time."""
    before = {str(row["run_id"]): str(row["head"]) for row in _review_rows()}
    worktree = fleet["worktrees"][0]
    moved = _commit(worktree, "later.txt", "later\n")
    after = {str(row["run_id"]): str(row["head"]) for row in _review_rows()}
    assert after["run-00"] == moved, (
        f"run-00 reports {after['run-00']!r} after a commit, not {moved!r}"
    )
    assert after["run-00"] != before["run-00"], (
        "the row head did not move after a commit in its worktree"
    )
    for run_id, earlier in before.items():
        if run_id == "run-00":
            continue
        assert after[run_id] == earlier, (
            f"{run_id} moved without a commit in its own worktree"
        )
