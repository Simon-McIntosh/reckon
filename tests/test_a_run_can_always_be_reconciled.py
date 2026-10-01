"""A run is always reconcilable from whatever survives of it.

Promotion reaches a run through its live pointer, but the states that need
reconciling are exactly the ones where the pointer is gone or the landing is
half-written. Four cases are covered here, each in a temporary RECKON_HOME: a
run with only its run directory left promotes from the durable record the
launch wrote; a run whose row already exists has its pointer retired without a
duplicate row; a run whose worktree is gone reports that its boundary check has
no referent and is landed by one acknowledgement rather than one per path; and a
passing run whose manifest names its gate promotes with no gate flags typed.
The closure of the second case is the interrupted landing whose comment carries
an apostrophe: the stored body is compared by its unescaped text, so a
re-promotion after the landing commit failed is accepted as already recorded.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import _plan_html, crew, ledger
from reckon.crew import promotion
from reckon.crew.runs import _write_json, pointer_path, run_dir

PROJECT = "sample"
PLAN = "plan-a"


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write_resource(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{state['slug']}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthetic config home and committed repository, isolated from the fleet."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    _write_resource(
        root / "docs" / "plans" / f"{PLAN}.html",
        {
            "type": "plan",
            "slug": PLAN,
            "title": "Plan A",
            "status": "active",
            "version": 0,
            "impl": 0.0,
            "comments": {},
        },
    )
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt", "docs")
    _git(root, "commit", "-q", "-m", "test: seed repository")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _pointer(
    repository: Path,
    run_id: str,
    *,
    worktree: str | None = None,
    node: dict | None = None,
    manifest_path: str = "",
    extra: dict | None = None,
) -> None:
    pointer: dict[str, object] = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repository),
        "worktree": worktree if worktree is not None else str(repository),
        "base_sha": "",
        "launch": "in-harness",
        "role": "review",
        "backend": "native",
        "created_at": "2026-10-01T06:00:00Z",
        "manifest_path": manifest_path,
        "node": node or {"id": "node-a", "plan": PLAN, "section": "s1", "write_paths": []},
    }
    if extra:
        pointer.update(extra)
    _write_json(pointer_path(run_id), pointer)


def _manifest(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# ── Case 1: only the run directory survives ─────────────────────────────────


def test_a_run_with_only_pointerless_run_directory_promotes(repository: Path) -> None:
    """No pointer, no row: the run directory still carries the delivery."""
    run_id = "r-reconcile-no-pointer"
    directory = run_dir(run_id)
    directory.mkdir(parents=True)
    (directory / "supervisor.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "repo": str(repository),
                "worktree": str(repository),
                "fenced": False,
            }
        ),
        encoding="utf-8",
    )
    (directory / "manifest.md").write_text(
        "node: node-a\nstatus: complete\ntests: not applicable\n",
        encoding="utf-8",
    )
    assert not pointer_path(run_id).exists()

    result = crew.complete(
        run_id,
        gate="failed",
        failure_classification="negative-result",
        outcome="the measured effect was absent under the stated conditions",
        root=repository,
    )

    rows = ledger.runs(PROJECT, root=repository)
    assert len(rows) == 1
    assert rows[0]["gate"] == "failed"
    assert rows[0]["failure_classification"] == "negative-result"
    assert result["run_id"] == run_id


# ── Case 2: a row exists and the pointer is retired without a duplicate ─────


def _promote_not_run(repository: Path, run_id: str, *, outcome: str) -> dict:
    return crew.complete(run_id, gate="not-run", outcome=outcome, root=repository)


def test_a_run_with_a_row_and_a_live_pointer_ends_with_one_row(
    repository: Path,
) -> None:
    """Re-promotion retires the pointer and writes no second row."""
    run_id = "r-reconcile-row-and-pointer"
    outcome = "reviewed and landed under the reconcile path"
    _pointer(repository, run_id)

    first = _promote_not_run(repository, run_id, outcome=outcome)
    assert first["already_promoted"] is False
    assert not pointer_path(run_id).exists()
    assert len(ledger.runs(PROJECT, root=repository)) == 1

    # The state a completed run leaves: the row survives and a pointer is on
    # disk again, which is the shape a half-finished landing leaves behind.
    _pointer(repository, run_id)
    second = _promote_not_run(repository, run_id, outcome=outcome)

    assert second["already_promoted"] is True
    assert not pointer_path(run_id).exists()
    assert len(ledger.runs(PROJECT, root=repository)) == 1


def test_an_interrupted_landing_with_an_apostrophe_is_re_promotable(
    repository: Path,
) -> None:
    """A stored comment is matched by its unescaped text, so an apostrophe lands."""
    run_id = "r-reconcile-apostrophe"
    outcome = "the runner's landing was interrupted before its row was written"
    _pointer(repository, run_id)
    # The first attempt wrote the plan comment, then its landing commit failed
    # and left no row. Seed that comment directly, as the failed attempt did.
    promotion._record_landing_comment(
        project=PROJECT,
        plan=PLAN,
        section="s1",
        run_id=run_id,
        narrative=outcome,
        author="reckon-build",
        when="2026-10-01T06:00:00Z",
        root=repository,
        worker_tree=repository,
    )

    result = crew.complete(run_id, gate="not-run", outcome=outcome, root=repository)

    assert result["plan_comment"]["recorded"] is True
    assert result["plan_comment"]["already_recorded"] is True
    assert not pointer_path(run_id).exists()
    assert len(ledger.runs(PROJECT, root=repository)) == 1


# ── Case 3: a boundary check with no surviving worktree ─────────────────────


def test_a_run_whose_worktree_is_gone_promotes_with_one_acknowledgement(
    repository: Path,
) -> None:
    """No referent is a single acknowledgement, never one claim per path."""
    run_id = "r-reconcile-no-referent"
    gone = repository / "worktrees" / run_id
    # A declared path the walk will find dirty in the main checkout, and a
    # baseline snapshot that recorded it clean. With the run's own worktree
    # gone, those changes cannot be attributed to this run.
    declared = repository / "declared"
    declared.mkdir()
    (declared / "extra.txt").write_text("peer work\n", encoding="utf-8")
    snapshot = {
        "trees": [
            {
                "path": str(repository),
                "status_digest": "before",
                "status_entries": [],
            }
        ]
    }
    _pointer(
        repository,
        run_id,
        worktree=str(gone),
        node={
            "id": "node-a",
            "plan": PLAN,
            "section": "s1",
            "write_paths": ["declared"],
        },
        extra={"repository_tree_snapshot": snapshot},
    )
    assert not gone.is_dir()

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(
            run_id,
            gate="not-run",
            outcome="landed from a run whose worktree was already released",
            root=repository,
        )
    assert "single acknowledgement" in str(refusal.value)
    assert pointer_path(run_id).is_file()
    assert ledger.runs(PROJECT, root=repository) == []

    result = crew.complete(
        run_id,
        gate="not-run",
        outcome="landed from a run whose worktree was already released",
        root=repository,
        boundary_waiver="its worktree was released before promotion",
    )

    assert result["record"]["boundary_waiver"]["no_referent"]
    assert not pointer_path(run_id).exists()
    assert len(ledger.runs(PROJECT, root=repository)) == 1


# ── Case 4: a manifest that names its gate needs no gate flags ───────────────


def test_a_passing_run_whose_manifest_names_its_gate_promotes(
    repository: Path, tmp_path: Path
) -> None:
    """Gate command, log, exit status and commits default to the manifest."""
    run_id = "r-reconcile-manifest-gate"
    (repository / "delivered.txt").write_text("delivered\n", encoding="utf-8")
    _git(repository, "add", "delivered.txt")
    _git(repository, "commit", "-q", "-m", "test: record the delivery")
    commit = _git(repository, "rev-parse", "HEAD")
    log = tmp_path / "logs" / f"{run_id}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("1 passed\nEXIT=0\n", encoding="utf-8")
    manifest = _manifest(
        run_dir(run_id) / "manifest.md",
        "node: node-a\n"
        "status: complete\n"
        f"commits: {commit}\n"
        "changed_paths: delivered.txt\n"
        "tests: pytest tests/test_a_run_can_always_be_reconciled.py\n"
        f"test_logs: {log}\n",
    )
    _pointer(
        repository,
        run_id,
        manifest_path=str(manifest),
        node={
            "id": "node-a",
            "plan": PLAN,
            "section": "s1",
            "write_paths": ["delivered.txt"],
        },
    )

    result = crew.complete(
        run_id,
        gate="passed",
        outcome="landed with the gate its manifest named",
        root=repository,
    )

    row = result["record"]
    assert row["gate"] == "passed"
    assert row["commits"] == [commit]
    assert row["gate_check"]["command"] == (
        "pytest tests/test_a_run_can_always_be_reconciled.py"
    )
    assert row["gate_check"]["exit_status"] == 0
    assert not pointer_path(run_id).exists()
    assert len(ledger.runs(PROJECT, root=repository)) == 1