"""A promotion refuses an open git operation before it reads any store.

Whether a landing commit can be written at all depends only on the checkout's
git state: an open merge, rebase, cherry-pick or revert belongs to another
session's operation, and a landing commit written into it would move that
operation's first parent or take its staged work. That answer is available
before promotion reads the ledger or opens a store, so it is judged there — a
refusal reached after the landing checks has already spent the whole promotion's
work, and in a serial sweep every such refusal delays every later promotion by
as much.

Each open state is resolved with ``git rev-parse --git-path`` so a linked
worktree, whose ``.git`` is a file, is read where git actually keeps the marker.

Nothing here touches the operator's store: the crew home, the ledger and the
checkout are all built under the test's own temporary directory.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest
from click.testing import CliRunner

from reckon import _plan_html, ledger, run_store
from reckon import cli as cli_module
from reckon.crew.runs import _write_json, pointer_path, run_dir
from tests.helpers.git import git as _git

PROJECT = "proj"
PLAN = "plan-a"
REVIEW_RUN = "r-20261112T100000000000-review-of-reviewed-subject"
ROW_COUNT = 200

# marker name, the word the refusal must carry, and whether the marker is a
# directory (a rebase state) rather than a file.
OPEN_STATES = (
    ("MERGE_HEAD", "merge", "merge"),
    ("rebase-merge", "rebase", "dir"),
    ("rebase-apply", "rebase", "dir"),
    ("CHERRY_PICK_HEAD", "cherry-pick", "file"),
    ("REVERT_HEAD", "revert", "file"),
)


class _ReadCounter:
    """Count ledger loads and run-store constructions, in order.

    A promotion that refuses on the checkout's git state should reach neither,
    and a count of zero says nothing unless the same wrappers are shown to see
    the reads a promotion really makes — the clean-checkout case below is that
    positive control.
    """

    def __init__(self) -> None:
        self.ledger_loads = 0
        self.run_stores = 0

    def count_ledger_load(self, original):
        def counting(*args, **kwargs):
            self.ledger_loads += 1
            return original(*args, **kwargs)

        return counting

    def count_run_store(self):
        original = run_store.RunStore.__init__

        def counting(store_self, *args, **kwargs):
            self.run_stores += 1
            return original(store_self, *args, **kwargs)

        return counting


def _synthetic_row(position: int) -> dict:
    """One completed run row, shaped like a promoted implement run."""
    return {
        "run_id": f"r-20260101T{position // 3600 % 24:02d}{position // 60 % 60:02d}"
        f"{position % 60:02d}000000-fixture-{position:04d}",
        "project": PROJECT,
        "node": f"fixture-node-{position:04d}",
        "plan": PLAN,
        "role": "implement",
        "gate": "passed",
        "status": "complete",
        "session_id": f"session-{position:04d}",
        "dispatched_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T00:05:00Z",
        "wall_seconds": 300.0,
    }


def _write_plan(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(
        _plan_html.write_state(
            bare,
            {
                "type": "plan",
                "slug": PLAN,
                "title": "Plan A",
                "status": "active",
                "version": 0,
                "comments": {},
            },
        ),
        encoding="utf-8",
    )


def _build_checkout(root: Path) -> Path:
    """A committed checkout a promotion can land its record into."""
    _write_plan(root / "docs" / "plans" / f"{PLAN}.html")
    (root / "docs" / "state" / PROJECT).mkdir(parents=True, exist_ok=True)
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("config", "commit.gpgsign", "false"),
    ):
        _git(root, *arguments)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "docs", "seed.txt")
    _git(root, "commit", "-q", "-m", "test: seed repository")
    return root


def _seed_ledger(root: Path) -> None:
    """Write a ledger of ``ROW_COUNT`` rows, then warm its index with one read."""
    ledger.write(
        PROJECT,
        {
            "members": [],
            "runs": [_synthetic_row(position) for position in range(ROW_COUNT)],
            "holds": [],
        },
        0,
        root=root,
    )
    rows, _version = ledger.load(PROJECT, root=root)
    assert len(rows["runs"]) == ROW_COUNT


def _write_review_run(root: Path) -> None:
    """A review run's pointer and manifest, as the live promotion reads them."""
    manifest = run_dir(REVIEW_RUN) / "manifest.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: review-of-a-subject\n"
        "status: complete\n"
        "changed_paths: []\n"
        'tests: ["pytest -q tests/a.py"]\n',
        encoding="utf-8",
    )
    _write_json(
        pointer_path(REVIEW_RUN),
        {
            "run_id": REVIEW_RUN,
            "project": PROJECT,
            "repo": str(root),
            "worktree": str(root),
            "base_sha": _git(root, "rev-parse", "HEAD"),
            "launch": "in-harness",
            "role": "review",
            "backend": "native",
            "created_at": "2026-11-12T09:59:00Z",
            "manifest_path": str(manifest),
            "node": {
                "id": "review-of-reviewed-subject",
                "plan": PLAN,
                "section": "s3",
                "time_budget": "25m",
                "role": "review",
                "write_paths": [],
            },
        },
    )


def _seed_run_directories() -> None:
    """The run directories a crew home of ``ROW_COUNT`` runs carries."""
    for position in range(ROW_COUNT):
        directory = run_dir(f"fixture-{position:04d}")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "manifest.md").write_text(
            f"node: fixture-{position:04d}\nstatus: complete\n", encoding="utf-8"
        )


def _marker_path(root: Path, marker: str) -> Path:
    """Resolve a git state marker through the repository's own git directory."""
    resolved = _git(root, "rev-parse", "--git-path", marker)
    path = Path(resolved)
    return path if path.is_absolute() else root / path


def _open_state(root: Path, marker: str, kind: str) -> None:
    if kind == "merge":
        _git(root, "checkout", "-q", "-b", "peer")
        (root / "peer.txt").write_text("peer\n", encoding="utf-8")
        _git(root, "add", "peer.txt")
        _git(root, "commit", "-q", "-m", "test: peer change")
        _git(root, "checkout", "-q", "main")
        _git(root, "merge", "--no-ff", "--no-commit", "-m", "test: peer merge", "peer")
        return
    path = _marker_path(root, marker)
    if kind == "dir":
        path.mkdir(parents=True, exist_ok=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("0" * 40 + "\n", encoding="utf-8")


def _snapshot(root: Path) -> dict[str, bytes]:
    """The bytes of everything a refusal promises not to write."""
    files: dict[str, bytes] = {}
    for path in (
        ledger.ledger_path(PROJECT, root),
        root / "docs" / "plans" / f"{PLAN}.html",
    ):
        files[str(path)] = path.read_bytes()
    directory = run_dir(REVIEW_RUN)
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            files[str(path)] = path.read_bytes()
    return files


def _invoke(root: Path) -> tuple[object, _ReadCounter]:
    counter = _ReadCounter()
    original_load = ledger.load
    original_open = ledger.load_open
    with (
        mock.patch.object(ledger, "load", counter.count_ledger_load(original_load)),
        mock.patch.object(
            ledger, "load_open", counter.count_ledger_load(original_open)
        ),
        mock.patch.object(run_store.RunStore, "__init__", counter.count_run_store()),
    ):
        result = CliRunner().invoke(
            cli_module.main,
            [
                "crew",
                "complete",
                "--run",
                REVIEW_RUN,
                "--gate",
                "not-run",
                "--outcome",
                "a fixture that exercises the checkout guard",
                "--checkout-path",
                str(root),
            ],
        )
    return result, counter


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.delenv("RECKON_RUN_ID", raising=False)
    return config_home


@pytest.fixture()
def repository(tmp_path: Path, home: Path) -> Path:
    _seed_run_directories()
    root = _build_checkout(tmp_path / "repo")
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    _seed_ledger(root)
    _write_review_run(root)
    return root


@pytest.mark.parametrize(("marker", "state_word", "kind"), OPEN_STATES)
def test_an_open_operation_refuses_before_any_ledger_read(
    repository: Path, marker: str, state_word: str, kind: str
) -> None:
    """The refusal names the operation and reached neither ledger nor store."""
    _open_state(repository, marker, kind)
    before = _snapshot(repository)

    result, counter = _invoke(repository)

    assert result.exit_code != 0, result.output
    assert f"has an open {state_word} in progress" in result.output
    assert counter.ledger_loads == 0, (
        "the checkout's open operation was judged only after the ledger was read"
    )
    assert counter.run_stores == 0, (
        "the checkout's open operation was judged only after a store was opened"
    )
    assert _snapshot(repository) == before, (
        "the refusal wrote to the ledger, the plan or the run directory"
    )
    assert pointer_path(REVIEW_RUN).is_file()


def test_a_clean_checkout_reaches_the_ledger(repository: Path) -> None:
    """The counter is shown to see a promotion's ledger read before asserting none."""
    result, counter = _invoke(repository)

    assert result.exit_code != 0, result.output
    assert counter.ledger_loads > 0, (
        "the wrappers saw no ledger load even without an open operation, so a "
        "zero count on the open-operation case would prove nothing"
    )


def test_the_store_wrapper_sees_a_construction(tmp_path: Path) -> None:
    """The run-store counter is shown to see a known construction."""
    counter = _ReadCounter()
    with (
        mock.patch.object(run_store.RunStore, "__init__", counter.count_run_store()),
        run_store.RunStore(tmp_path / "store.db"),
    ):
        pass

    assert counter.run_stores == 1
