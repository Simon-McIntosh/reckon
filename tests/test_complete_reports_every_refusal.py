"""``crew complete`` answers every refusal it would raise in one call.

Three behaviours are checked here, each making the guarded thing happen and
reading the answer rather than resting on the suite being green:

* ``--dry-run`` on a run failing three guards reports all three together, with
  the one invocation that then promotes the run.
* a reason-taking flag refuses a value that begins with ``--``, so an omitted
  reason is reported as a missing reason rather than swallowed.
* a manifest carrying the gate receipt in the contracted form promotes with no
  gate flags at all.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Sequence
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import _plan_html, _store, cli as cli_module, crew
from reckon.crew import promotion as promotion_module
from reckon.crew.runs import _write_json, pointer_path
from tests.conftest import EXECUTABLE_GATE_COMMAND
from tests.helpers.git import git as _git

PROJECT = "proj"
PLAN = "plan-a"


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


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    _write_plan(root / "docs" / "plans" / f"{PLAN}.html")
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


def _set_plan(repository: Path, **fields) -> None:
    state, version = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    state.update(fields)
    _store.write_plan(PROJECT, PLAN, state, version, repository, artifact_type="plan")


def _manifest(
    tmp_path: Path,
    run_id: str,
    *,
    commits: Sequence[str] = (),
    log_lines: Sequence[str] = (),
) -> Path:
    """Write the run's manifest, citing a gate log with the given last line."""
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    log = manifest.parent / f"{run_id}.log"
    log.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    listed = ", ".join(commits)
    manifest.write_text(
        "node: node-a\n"
        "status: complete\n"
        f"commits: [{listed}]\n"
        "changed_paths: []\n"
        f"tests: {EXECUTABLE_GATE_COMMAND}\n"
        f"test_logs: [{log}]\n",
        encoding="utf-8",
    )
    return manifest


def _pointer(
    repository: Path,
    run_id: str,
    *,
    manifest: Path,
    plan_impl_at_dispatch: float | None = None,
) -> None:
    record: dict = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repository),
        "worktree": str(repository),
        "launch": "in-harness",
        "role": "implement",
        "backend": "native",
        "created_at": "2026-09-21T06:00:00Z",
        "manifest_path": str(manifest),
        "node": {
            "id": "node-a",
            "plan": PLAN,
            "section": "s2",
            "time_budget": "25m",
            "write_paths": [],
        },
    }
    if plan_impl_at_dispatch is not None:
        record["plan_impl_at_dispatch"] = plan_impl_at_dispatch
    _write_json(pointer_path(run_id), record)


def _three_guard_run(repository: Path, tmp_path: Path, run_id: str) -> None:
    """A run that fails the impl, review and gate-receipt guards at once.

    Its manifest cites a runnable command and a log that records no exit
    status, so the fill supplies the command and the log path and the check is
    refused only for the status the log does not carry.
    """
    _set_plan(repository, impl=0.5)
    _pointer(
        repository,
        run_id,
        manifest=_manifest(tmp_path, run_id, log_lines=("ran the focused gate",)),
        plan_impl_at_dispatch=0.5,
    )


def test_the_dry_run_reports_three_refusals_and_an_invocation_that_promotes(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20261019T090000000000-node-a"
    _three_guard_run(repository, tmp_path, run_id)

    report = promotion_module.complete_dry_run(
        run_id,
        root=repository,
        gate="passed",
        gate_check={"command": "", "exit_status": None, "log_path": "", "log_digest": ""},
        require_gate_check=True,
    )
    refusals = "\n".join(report["refusals"])

    assert report["dry_run"] is True
    assert report["would_promote"] is False
    assert report["refusal_count"] == 3, refusals
    assert "impl did not move" in refusals
    assert "review" in refusals
    assert "the check that produced it" in refusals
    # Nothing was written: the pointer survives for the run the dry run describes.
    assert pointer_path(run_id).exists()

    # The emitted invocation is run as it stands, and promotes.
    argv = shlex.split(report["invocation"])
    assert argv[:4] == ["reckon", "crew", "complete", "--run"]
    result = CliRunner().invoke(
        cli_module.main, ["crew", *argv[2:], "--checkout-path", str(repository)]
    )
    assert result.exit_code == 0, result.output
    assert not pointer_path(run_id).exists()
    assert "promoted" in result.output or "run_id" in result.output


def test_a_reason_flag_refuses_a_value_that_begins_with_two_dashes() -> None:
    result = CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "complete",
            "--run",
            "r-20261019T090100000000-node-a",
            "--gate",
            "passed",
            "--no-commit",
            "--gate",
            "passed",
        ],
    )

    assert result.exit_code != 0, result.output
    assert "requires a reason" in result.output
    assert "missing" in result.output


def test_a_contracted_receipt_promotes_with_no_gate_flags(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20261019T090200000000-node-a"
    _set_plan(repository, impl=0.8)
    _pointer(
        repository,
        run_id,
        manifest=_manifest(
            tmp_path, run_id, log_lines=("ran the gate", "EXIT=0")
        ),
        plan_impl_at_dispatch=0.5,
    )

    result = crew.complete(
        run_id,
        root=repository,
        gate="passed",
        review_waiver="a sibling node carries the review of this change",
    )

    assert result["ledger_path"], result
    assert not pointer_path(run_id).exists()