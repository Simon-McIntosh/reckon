"""A node whose only change is its evidence lands on its declared check.

Such a node names no test file, so no baseline and after suite pair can measure
it and the landing reads the check the node declared instead. The check is a
command: it passes here only because the fragment the node delivered is on
disk. The second case runs the same node beside the same check answering
failure, and landing refuses that run — so the case lands because it meets the
declared check, not because a run with no test file is admitted whatever it
says.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon.cli import main as cli_main
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "evidence-fixture"
PLAN = "evidence-plan"
FRAGMENT = "docs/evidence/fragments/evidence-plan/leaf.html"
RUN_ID = "r-20261115T040000000000-evidence-only"


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *arguments], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture()
def evidence_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A temporary crew home and a repository holding one evidence-only run."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    repo = tmp_path / "repo"
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / f"{PLAN}.html").write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{PLAN}">'
        "</head><body></body></html>",
        encoding="utf-8",
    )
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "worker@example.invalid")
    _git(repo, "config", "user.name", "Worker")
    _git(repo, "add", "docs")
    _git(repo, "commit", "-q", "-m", "seed")
    return tmp_path, repo


def _deliver(repo: Path) -> str:
    """Commit the run's only change — its evidence fragment — and its record."""
    fragment = repo / FRAGMENT
    fragment.parent.mkdir(parents=True, exist_ok=True)
    fragment.write_text("<section>evidence</section>\n", encoding="utf-8")
    _git(repo, "add", "docs")
    _git(repo, "commit", "-q", "-m", "evidence")
    directory = pointer_path(RUN_ID).parent
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / "manifest.md"
    manifest_path.write_text(
        "node: evidence-node\nstatus: complete\ntests: none\n", encoding="utf-8"
    )
    _write_json(
        pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "repo": str(repo),
            "worktree": str(repo),
            "launch": "in-harness",
            "role": "implement",
            "member": "worker-a",
            "backend": "native",
            "created_at": "2026-11-15T04:00:00Z",
            "manifest_path": str(manifest_path),
            "node": {
                "id": "evidence-node",
                "plan": PLAN,
                "section": "evidence",
                "time_budget": "20m",
                "write_paths": ["docs/evidence/fragments/evidence-plan"],
            },
        },
    )
    return _git(repo, "rev-parse", "HEAD")


def _declared_check() -> str:
    """The node's declared check: it passes only on the delivered fragment."""
    return (
        f'{sys.executable} -c "import pathlib,sys; '
        f"sys.exit(not pathlib.Path('{FRAGMENT}').exists())\""
    )


def _complete(repo: Path, log: Path, exit_status: int):
    """Drive the node's promotion with the evidence check as its gate."""
    check = _declared_check()
    return CliRunner().invoke(
        cli_main,
        [
            "crew",
            "complete",
            "--wait",
            "--run",
            RUN_ID,
            "--gate",
            "passed",
            "--checkout-path",
            str(repo),
            "--no-commit",
            "evidence only",
            "--gate-command",
            check,
            "--gate-exit-status",
            str(exit_status),
            "--gate-log-path",
            str(log),
            "--waive-unreviewed-promotion",
            "an evidence-only run has no review store",
        ],
    )


def test_an_evidence_only_node_lands_on_its_declared_check(
    evidence_run: tuple[Path, Path],
) -> None:
    tmp_path, repo = evidence_run
    head = _deliver(repo)
    log = tmp_path / "gate.log"
    log.write_text("fragment present\nEXIT=0\n", encoding="utf-8")

    result = _complete(repo, log, 0)

    assert result.exit_code == 0, result.output
    # A completion's reply is bounded: it names the path its whole receipt was
    # written to, and the landed record is read there.
    receipt = json.loads(result.output)
    landed = json.loads(Path(receipt["record_path"]).read_text(encoding="utf-8"))
    record = landed["record"]
    assert record["gate"] == "passed"
    assert record["gate_check"]["command"] == _declared_check()
    assert record["gate_check"]["exit_status"] == 0
    assert record["negative_control"]["verdict"] == "exempt"
    assert record["negative_control"]["reason"] == "node-writes-no-test-path"
    assert record["promoted_revision"] == head


def test_the_same_node_beside_a_failing_check_is_refused(
    evidence_run: tuple[Path, Path],
) -> None:
    tmp_path, repo = evidence_run
    _deliver(repo)
    log = tmp_path / "gate.log"
    log.write_text("fragment missing\nEXIT=1\n", encoding="utf-8")

    result = _complete(repo, log, 1)

    assert result.exit_code != 0, result.output
    assert "gate 'passed'" in result.output
