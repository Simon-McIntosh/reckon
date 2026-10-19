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
import os
import shlex
from collections.abc import Sequence
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import _plan_html, _store, crew
from reckon import cli as cli_module
from reckon.crew import promotion as promotion_module
from reckon.crew.runs import _write_json, pointer_path, run_dir, runs_dir
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
    project: str | None = PROJECT,
    write_paths: Sequence[str] = (),
) -> None:
    record: dict = {
        "run_id": run_id,
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
            "write_paths": [str(path) for path in write_paths],
        },
    }
    if project is not None:
        record["project"] = project
    if plan_impl_at_dispatch is not None:
        record["plan_impl_at_dispatch"] = plan_impl_at_dispatch
    _write_json(pointer_path(run_id), record)


# The four stores a dry run promises to leave alone, read back as bytes rather
# than as a difference in shape: a pointer rewritten to the same content, a
# ledger rewritten at the same size and a record refiled byte-identically are
# each a write, and only the bytes tell them apart from not writing.
#
# Three subtrees of the crew home are excluded, each because a *read* writes it
# and none because it might hold a record: `cache` is the ledger read cache any
# reader populates, and `locks` and `leases` are the run's pointer lock and the
# promotion's cross-host claim, which a dry run takes and releases so it cannot
# race a promotion of the same run. A pointer, a ledger, a review store record
# and a worktree file are all still compared.
_COORDINATION_DIRS = ("cache", "locks", "leases")


def _files_under(
    root: Path, *, skip_dirs: Sequence[str] = (), skip_names: Sequence[str] = ()
) -> dict[str, bytes]:
    """Every file under ``root`` by relative path, minus the skipped names."""
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in skip_dirs for part in relative.parts[:-1]):
            continue
        if path.name in skip_names:
            continue
        files[relative.as_posix()] = path.read_bytes()
    return files


def _stores(repository: Path, config_home: Path) -> dict[str, dict[str, bytes]]:
    """The pointer, the ledger, the review store and the worktree, as bytes.

    Two kinds of file are left out, and neither is a record this node writes.
    The classification memo is a read cache the classifier persists into the
    run directory whenever anything classifies the pointer, so every reader of
    the fleet writes it and no promotion owns it. The run's pointer lock and the
    promotion's cross-host lease are taken by a dry run and released when it
    returns, which is how a dry run avoids racing a promotion of the same run.
    """
    return {
        "home": _files_under(
            config_home,
            skip_dirs=_COORDINATION_DIRS,
            skip_names=("classification.json",),
        ),
        "worktree": _files_under(repository, skip_dirs=(".git",)),
    }


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
    config_home = Path(os.environ["RECKON_HOME"])
    before = _stores(repository, config_home)

    report = promotion_module.complete_dry_run(
        run_id,
        root=repository,
        gate="passed",
        gate_check={
            "command": "",
            "exit_status": None,
            "log_path": "",
            "log_digest": "",
        },
        require_gate_check=True,
    )
    refusals = "\n".join(report["refusals"])

    assert report["dry_run"] is True
    assert report["would_promote"] is False
    assert report["refusal_count"] == 3, refusals
    assert "impl did not move" in refusals
    assert "review" in refusals
    assert "the check that produced it" in refusals
    # Nothing was written: every record the promotion would touch reads back
    # byte-identical, the pointer included.
    assert _stores(repository, config_home) == before

    # The emitted invocation is run as it stands, and promotes.
    argv = shlex.split(report["invocation"])
    assert argv[:4] == ["reckon", "crew", "complete", "--run"]
    result = CliRunner().invoke(
        cli_module.main, ["crew", *argv[2:], "--checkout-path", str(repository)]
    )
    assert result.exit_code == 0, result.output
    assert not pointer_path(run_id).exists()
    assert "promoted" in result.output or "run_id" in result.output


def test_a_dry_run_writes_nothing_on_any_of_its_three_paths(
    repository: Path, tmp_path: Path
) -> None:
    """The withdrawn, review-filing and picker-timeout paths write nothing.

    A promotion reaches each of these before it judges any guard, so a dry run
    that branches only after them mutates state it promised to leave alone:
    it retires a withdrawn run's pointer and releases its workspace, files a
    delivered review into the store, and stamps a picker timeout onto the
    pointer of a run whose pick has not answered. Each run is asked to do
    exactly what it would do, and every store is compared byte for byte across
    the three calls.
    """
    ordinary = "r-20261019T091000000000-node-a"
    withdrawn = "r-20261019T091100000000-node-a"
    delivered = "r-20261019T091200000000-node-a"
    reviewed_run = "r-20261019T091300000000-node-a"

    _set_plan(repository, impl=0.5)
    _pointer(
        repository,
        ordinary,
        manifest=_manifest(tmp_path, ordinary, log_lines=("EXIT=0",)),
        plan_impl_at_dispatch=0.5,
    )
    # A dispatch refused before a project was resolved: no project to hold a row.
    _pointer(
        repository,
        withdrawn,
        manifest=_manifest(tmp_path, withdrawn, log_lines=("EXIT=0",)),
        project=None,
    )
    # A review whose record is delivered into the reviewed run's directory and
    # never filed, which is the run the filing ahead of the guards exists for.
    record = runs_dir() / reviewed_run / "review-records" / "record.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps({"reviewed_run": reviewed_run}), encoding="utf-8")
    _pointer(
        repository,
        delivered,
        manifest=_manifest(tmp_path, delivered, log_lines=("EXIT=0",)),
        write_paths=(str(record),),
    )
    # A dispatch whose advisory pick has not answered: a promotion waits for it
    # and writes the timeout onto the pointer when the wait runs out.
    run_dir(ordinary).mkdir(parents=True, exist_ok=True)
    (run_dir(ordinary) / "shadow-picker.json").write_text("{}", encoding="utf-8")

    config_home = Path(os.environ["RECKON_HOME"])
    before = _stores(repository, config_home)
    reports = {
        run_id: promotion_module.complete_dry_run(
            run_id,
            root=repository,
            gate="passed",
            gate_check={
                "command": "",
                "exit_status": None,
                "log_path": "",
                "log_digest": "",
            },
            require_gate_check=True,
        )
        for run_id in (ordinary, withdrawn, delivered)
    }
    after = _stores(repository, config_home)

    changed = sorted(
        name
        for name in set(before["home"]) | set(after["home"])
        if before["home"].get(name) != after["home"].get(name)
    )
    assert after["home"] == before["home"], changed
    assert after["worktree"] == before["worktree"]
    # A withdrawn run says it would be withdrawn, and offers no invocation: its
    # promotion writes no ledger row to point at.
    assert reports[withdrawn]["would_withdraw"] is True
    assert reports[withdrawn]["invocation"] == ""
    assert "withdraw" in "\n".join(reports[withdrawn]["refusals"])
    # A run carrying a delivered review says which record a filing would read.
    assert reports[delivered]["would_file_reviews"] == [str(record)]
    assert "would_file_reviews" in "\n".join(reports[delivered].get("notes") or [])
    # A run whose pick is outstanding says a promotion would wait for it.
    assert reports[ordinary]["would_wait_for_pick"] is True
    assert pointer_path(ordinary).exists()


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
        manifest=_manifest(tmp_path, run_id, log_lines=("ran the gate", "EXIT=0")),
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
