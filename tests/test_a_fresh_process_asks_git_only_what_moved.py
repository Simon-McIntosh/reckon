"""A fresh process asks git only about what moved between its reads.

Two answers a derivation takes from git are functions of immutable object ids:
the commit a full revision resolves to, and the number of commits between a
worktree's head and the base it launched from. Neither can change while the
object ids stay the same, so a second process re-deriving the same fleet should
not spend a child process asking for either. They are kept in a store under the
crew home, keyed on the ids themselves.

The cases hold that contract: with the store populated and the in-process
caches empty, a first derivation over unmoved worktrees and full-revision
citations spawns neither command and reports the answers git reports; a ref
name still asks git, because a ref moves; a moved head is counted once and its
store entry updated; a corrupt store falls back to git with the correct answers
and rewrites the entry; and with the store not read at all the first derivation
spawns both, which is the negative control.
"""

from __future__ import annotations

import importlib
import json
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

import pytest

from reckon.crew import obligation_snapshot, runs
from reckon.crew import review as review_module
from tests.helpers.git import git as _git

watch_module = importlib.import_module("reckon.crew.recovery_watch")
subject_module = importlib.import_module("reckon.crew.recovery_review_subject")
obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "git-answer-store-fixture"
SESSION = "coordinator-fixture"
OBSERVED_AT = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
STORE_NAME = "git-object-answers.json"
SOURCE = "reckon/fixture_runtime.py"


def _commit(repository: Path, name: str, body: str) -> str:
    (repository / name).write_text(body, encoding="utf-8")
    _git(repository, "add", name)
    _git(repository, "commit", "-q", "-m", f"test: {body.strip()}")
    return _git(repository, "rev-parse", "HEAD")


def _seed_repo(root: Path, config_home: Path) -> None:
    """A project repository under a temporary configuration home."""
    (root / "docs" / "state" / PROJECT / "runs").mkdir(parents=True)
    (root / "docs" / "plans").mkdir()
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the git-answer fixture"),
    ):
        _git(root, *arguments)
    (root / "reckon").mkdir()
    (root / SOURCE).write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "add", SOURCE)
    _git(root, "commit", "-q", "-m", "test: seed a runtime source")
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
    head_sha: str,
    status: str,
    reviewed: bool,
) -> dict[str, Any]:
    """One live pointer, its manifest, and, when it owes one, its review."""
    manifest = config_home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {run_id}\nstatus: {status}\ncommits: [{head_sha}]\n",
        encoding="utf-8",
    )
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(root),
        "worktree": str(worktree),
        "base_sha": base_sha,
        "process_alive": False,
        "pid": 999_999,
        "launcher_host": socket.gethostname(),
        "launch": "in-harness",
        "role": "implement",
        "manifest_path": str(manifest),
        "node": {
            "id": run_id,
            "plan": "fixture-plan",
            "section": "fixture-section",
            "write_paths": ["seed.txt"],
        },
    }
    runs._write_json(runs.pointer_path(run_id), record)
    if reviewed:
        emitted = "\n".join(
            f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
        )
        stored = review_module.parse_review(emitted)
        stored.update(
            {
                "project": PROJECT,
                "reviewed_run_id": run_id,
                "review_run_id": f"review-of-{run_id}",
                "reviewed_base_sha": base_sha,
                "reviewed_head_sha": head_sha,
            }
        )
        review_module.store_review(stored)
    return record


def _clear_in_process_caches() -> None:
    subject_module._RESOLVE_COMMIT_CACHE.clear()
    watch_module._COMMITS_BEYOND_BASE_CACHE.clear()


class _GitSpy:
    """Record every argv the bound modules hand to ``subprocess.run``."""

    def __init__(self, *modules: Any) -> None:
        self._modules = modules
        self.commands: list[list[str]] = []

    def __enter__(self) -> Self:
        self._real = {id(module): module.subprocess for module in self._modules}
        spy = self
        real = self._real

        class _Recorder:
            def __init__(self, module: Any) -> None:
                self._module = module

            def run(self, argv: Any, *args: Any, **kwargs: Any) -> Any:
                spy.commands.append([str(part) for part in argv])
                return real[id(self._module)].run(argv, *args, **kwargs)

            def __getattr__(self, name: str) -> Any:
                return getattr(real[id(self._module)], name)

        for module in self._modules:
            module.subprocess = _Recorder(module)
        return self

    def __exit__(self, *exc: object) -> None:
        for module in self._modules:
            module.subprocess = self._real[id(module)]

    @property
    def resolve_calls(self) -> list[list[str]]:
        """The calls that asked git to resolve a revision to a commit."""
        return [
            command
            for command in self.commands
            if command[:2] == ["git", "rev-parse"]
            and command[2:4]
            == [
                "--verify",
                "--quiet",
            ]
        ]

    @property
    def count_calls(self) -> list[list[str]]:
        """The calls that asked git to count commits between two revisions."""
        return [
            command
            for command in self.commands
            if command[:3] == ["git", "rev-list", "--count"]
        ]


@pytest.fixture()
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A project of stopped runs, some owing a review, each with committed work."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    config_home = tmp_path / "config"
    config_home.mkdir()
    root = tmp_path / "repo"
    _seed_repo(root, config_home)
    base = _git(root, "rev-parse", "HEAD")
    records: dict[str, dict[str, Any]] = {}
    worktrees: list[Path] = []
    for index in range(4):
        run_id = f"run-{index:02d}"
        worktree = tmp_path / f"worktree-{index:02d}"
        _git(root, "worktree", "add", "-q", "--detach", str(worktree), base)
        head = _commit(worktree, f"work-{index:02d}.txt", f"work {index}\n")
        reviewed = index >= 2
        records[run_id] = _write_run(
            config_home,
            run_id,
            root=root,
            worktree=worktree,
            base_sha=base,
            head_sha=head,
            status="complete" if reviewed else "in-progress",
            reviewed=reviewed,
        )
        worktrees.append(worktree)
    monkeypatch.setattr(obligations_module, "_utc_now", lambda: OBSERVED_AT)
    return {
        "home": config_home,
        "repo": root,
        "base": base,
        "worktrees": worktrees,
        "records": records,
    }


def _store_path(fleet: dict[str, Any]) -> Path:
    return fleet["home"] / "crew" / STORE_NAME


def _derive() -> None:
    obligation_snapshot.fleet_state(PROJECT, now=OBSERVED_AT)


def test_populated_store_answers_a_first_derivation_without_git(
    fleet: dict[str, Any],
) -> None:
    """A first derivation over an unmoved fleet asks git for neither answer."""
    # The derivation settles its own state on the call that first reads the
    # fleet, so the store is populated by a call that has already run once.
    with _GitSpy(watch_module, subject_module):
        _derive()
    # The positive control lives in the negative-control case below, which shows
    # that with the store unread a derivation spawns both calls; here the point
    # is that a process with a fresh cache and a populated store spawns neither.
    _clear_in_process_caches()
    with _GitSpy(watch_module, subject_module) as cold:
        _derive()
    assert _store_path(fleet).is_file(), (
        "the first derivation wrote no git-answer store, so nothing can answer"
        " a fresh process"
    )
    assert cold.resolve_calls == [], (
        f"a derivation with a fresh cache over a populated store spawned"
        f" {len(cold.resolve_calls)} revision resolves"
    )
    assert cold.count_calls == [], (
        f"a derivation with a fresh cache over a populated store spawned"
        f" {len(cold.count_calls)} commit counts"
    )


def test_the_store_answers_what_git_answers(fleet: dict[str, Any]) -> None:
    """Every stored answer is the one git gives."""
    _derive()
    for run_id, record in fleet["records"].items():
        worktree = Path(record["worktree"])
        head = _git(worktree, "rev-parse", "HEAD")
        expected_count = int(
            _git(worktree, "rev-list", "--count", f"{record['base_sha']}..HEAD")
        )
        assert watch_module._commits_beyond_base(record) == expected_count, (
            f"{run_id} counts {watch_module._commits_beyond_base(record)} commits"
            f" where git counts {expected_count}"
        )
        assert subject_module._canonical_commits(worktree, [head]) == [head], (
            f"{run_id} resolves its full revision {head} to something else"
        )


def test_a_ref_name_still_asks_git(fleet: dict[str, Any]) -> None:
    """A ref name can move, so it is never answered from the store."""
    _derive()
    worktree = fleet["worktrees"][0]
    with _GitSpy(subject_module) as spy:
        resolved = subject_module._canonical_commits(worktree, ["main"])[0]
    assert resolved == _git(worktree, "rev-parse", "main"), (
        "the ref name did not resolve to the revision git reports"
    )
    assert len(spy.resolve_calls) == 1, (
        f"resolving a ref name spawned {len(spy.resolve_calls)} calls; a ref"
        " name must always ask git"
    )


def test_a_moved_head_is_counted_once_and_its_entry_updated(
    fleet: dict[str, Any],
) -> None:
    """A commit landing in a worktree is counted once, and stored for next time."""
    _derive()
    record = fleet["records"]["run-00"]
    worktree = Path(record["worktree"])
    moved = _commit(worktree, "later.txt", "later\n")
    _clear_in_process_caches()
    with _GitSpy(watch_module) as spy:
        count = watch_module._commits_beyond_base(record)
    expected = int(_git(worktree, "rev-list", "--count", f"{record['base_sha']}..HEAD"))
    assert count == expected == 2, (
        f"the moved worktree counts {count} commits where git counts {expected}"
    )
    assert len(spy.count_calls) == 1, (
        f"a moved head spawned {len(spy.count_calls)} counts, not one"
    )
    store = json.loads(_store_path(fleet).read_text(encoding="utf-8"))
    entries = store["counts"][str(worktree)]
    key = watch_module._count_answer_key(
        watch_module._worktree_head_identity(worktree), record["base_sha"]
    )
    assert entries.get(key) == expected, (
        "the store did not record the moved head's count, so the entry served"
        " next time is stale"
    )
    assert moved


def test_a_corrupt_store_falls_back_to_git_and_repairs_itself(
    fleet: dict[str, Any],
) -> None:
    """A corrupt store yields git's answers and is rewritten with them."""
    _derive()
    _store_path(fleet).write_text("{not json", encoding="utf-8")
    _clear_in_process_caches()
    record = fleet["records"]["run-00"]
    worktree = Path(record["worktree"])
    head = _git(worktree, "rev-parse", "HEAD")
    with _GitSpy(watch_module, subject_module) as spy:
        count = watch_module._commits_beyond_base(record)
        resolved = subject_module._canonical_commits(worktree, [head])[0]
    assert count == int(
        _git(worktree, "rev-list", "--count", f"{record['base_sha']}..HEAD")
    ), "a corrupt store did not fall back to the count git gives"
    assert resolved == head, "a corrupt store did not fall back to the commit git gives"
    assert spy.count_calls and spy.resolve_calls, (
        "a corrupt store answered without asking git, so the fallback was not exercised"
    )
    repaired = json.loads(_store_path(fleet).read_text(encoding="utf-8"))
    assert repaired["version"] == watch_module._GIT_ANSWER_STORE_VERSION, (
        "the corrupt store was not rewritten in the store's own shape"
    )
    assert repaired["commits"].get(str(worktree), {}).get(head) == head, (
        "the repaired store does not hold the revision it was asked about"
    )


def test_without_the_store_the_derivation_spawns_both(
    fleet: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard fires: with the store unread, a first derivation spawns both.

    This is the negative control in-process. The recorded control run applies
    the same mutation to the module and records the failing ids there.
    """
    _derive()
    _clear_in_process_caches()
    monkeypatch.setattr(
        watch_module,
        "_read_git_answer_store",
        watch_module._empty_git_answer_store,
    )
    with _GitSpy(watch_module, subject_module) as spy:
        _derive()
    assert spy.resolve_calls, (
        "with the store unread the derivation spawned no revision resolve"
    )
    assert spy.count_calls, (
        "with the store unread the derivation spawned no commit count"
    )
