"""A run whose worktree is gone is not charged the checkout's own files.

A completion refused a run with "cites no commit beyond its base ... while its
worktree holds its own declared work uncommitted". The run's worktree had
already been reclaimed, so the check fell back to the repository the run shared
its object store through — the integration checkout, where an operator keeps
untracked working files. Those files were read as the run's uncommitted work and
the run could not be retired.

The check measures the run's own worktree or nothing. A reclaimed worktree
leaves nothing to measure and the completion is accepted, with the record saying
the worktree was already gone. A worktree still on disk is read as before:
uncommitted declared work there is refused.

Nothing here touches the operator's store: the crew home, the ledger, the
checkout and the reclaimed worktree are all built under the test's own temporary
directory.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import run_store
from reckon.crew import promotion_evidence
from reckon.crew.node import CrewError
from reckon.crew.runs import _write_json, pointer_path, run_dir
from tests.helpers.git import git as _git
from tests.test_a_promotion_does_not_read_the_whole_ledger_index import (
    PLAN,
    PROJECT,
    RUN_ID,
    _build_checkout,
    _seed_ledger,
)

# The operator's untracked experiment files sit under the path the run declared
# it would write. They are never the run's, but the run's declaration reaches
# them once the check reads the integration checkout in the missing worktree's
# place.
DECLARED_ROOT = "configuration/machines/ITER"
OPERATOR_FILES = ("efitOptions-rca.xml", "efitOptions-rca-2.xml")
MANIFEST_BODY = (
    "node: node-a\n"
    "status: complete\n"
    "changed_paths: none (the sole deliverable is the report)\n"
)


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # The crew home is the test's own; no operator store is read or written.
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.delenv("RECKON_RUN_ID", raising=False)
    return config_home


def _write_operator_files(root: Path) -> None:
    declared = root / DECLARED_ROOT
    declared.mkdir(parents=True, exist_ok=True)
    for name in OPERATOR_FILES:
        (declared / name).write_text("<operator experiment/>\n", encoding="utf-8")


def _write_run(root: Path, tmp_path: Path, worktree: str) -> None:
    manifest = tmp_path / "manifests" / f"{RUN_ID}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(MANIFEST_BODY, encoding="utf-8")
    run_dir(RUN_ID).mkdir(parents=True, exist_ok=True)
    _write_json(
        pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "repo": str(root),
            "worktree": worktree,
            "base_sha": _git(root, "rev-parse", "HEAD"),
            "launch": "in-harness",
            "role": "implement",
            "backend": "native",
            "created_at": "2026-11-23T09:23:04Z",
            "manifest_path": str(manifest),
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "s2",
                "write_paths": [DECLARED_ROOT],
            },
        },
    )


def _complete(root: Path, home: Path) -> object:
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "complete",
            "--wait",
            "--run",
            RUN_ID,
            "--gate",
            "not-run",
            "--outcome",
            "the run's work reached the branch before its worktree was reclaimed",
            "--no-commit",
            "the head landed on the integration branch before the worktree was reclaimed",
            "--checkout-path",
            str(root),
        ],
    )


def test_a_gone_worktree_is_not_charged_the_checkouts_files(
    tmp_path: Path, home: Path
) -> None:
    """The reclaimed worktree ends the run's own check; the operator's files
    are charged to nobody."""
    root = _build_checkout(tmp_path / "repo")
    _seed_ledger(root)
    gone = tmp_path / "worktrees" / "r-gone"
    assert not gone.exists()
    _write_run(root, tmp_path, str(gone))
    _write_operator_files(root)
    # The instrument sees the files the check would have charged: read in the
    # integration checkout, the declared root holds two untracked files.
    assert promotion_evidence._uncommitted_paths(root, roots=(Path(DECLARED_ROOT),)), (
        "the fixture holds no untracked file under the declared root"
    )

    record = json.loads(pointer_path(RUN_ID).read_text(encoding="utf-8"))
    assert promotion_evidence._require_commits_beyond_base(RUN_ID, record, ()) is None

    result = _complete(root, home)

    assert result.exit_code == 0, result.output
    stored = run_store.stored_run_rows(PROJECT, run_ids=[RUN_ID])
    assert stored, "the completion recorded no run"
    release = stored[0].get("release") or {}
    assert release.get("worktree_absent") is True, stored[0]
    # Nothing the run produced is charged: the operator's files are not in the
    # record, and no commit was cited because the work had already landed.
    assert DECLARED_ROOT not in json.dumps(stored[0].get("changed_paths") or [])


def test_a_live_worktree_with_uncommitted_declared_work_is_still_refused(
    tmp_path: Path, home: Path
) -> None:
    """A worktree still on disk is measured, and its declared dirt is refused."""
    root = _build_checkout(tmp_path / "repo")
    _seed_ledger(root)
    # The run worked in this checkout, which is still here and holds its own
    # untracked deliverable under the run's declared path.
    _write_run(root, tmp_path, str(root))
    _write_operator_files(root)
    record = json.loads(pointer_path(RUN_ID).read_text(encoding="utf-8"))

    with pytest.raises(CrewError) as refused:
        promotion_evidence._require_commits_beyond_base(RUN_ID, record, ())
    assert DECLARED_ROOT in str(refused.value), refused.value
    # The second case's instrument is the same one the first case ran: it sees
    # an untracked file at the declared root, so the first case's acceptance is
    # an absence rather than a blind reader.
    assert promotion_evidence._uncommitted_paths(root, roots=(Path(DECLARED_ROOT),)), (
        "the instrument sees no untracked file at all"
    )
