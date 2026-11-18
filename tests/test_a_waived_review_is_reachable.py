"""A waived review count is reachable from the brief and from the committed row.

A review may file ``added_failure_count`` as the literal ``"waived"`` against a
run whose suite delta stands waived, but nothing reached that form on the path a
waived run actually takes. Two holes, one each side of the record:

* the composed review brief asked only for a count derived from the reviewed
  run's own gate logs, and no prompt named the waived form, so a reviewer whose
  arms carry no summary still wrote a null count — which the delivery contract
  refuses;
* once the run's pointer was gone, the committed-row fallback recognised a
  waiver only under ``suite_delta.status == "waived"``. ``crew complete
  --waive-suite-delta`` over a matched pair that added no failure writes
  ``status ``clean`` with the reason in ``waiver_reason``, because there the
  status names what the evidence found rather than how it was accepted, so that
  waiver was invisible and the review of a landed run was refused.

This module measures both: the brief names the waived form and its reason field
exactly for a run that carries a recorded waiver, and a promoted run whose row
carries clean-with-reason (and whose pointer is gone) accepts the waived count,
while a promoted run whose row carries neither form refuses it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon.cli import main as cli_main
from reckon.crew import recovery_review_subject as subject_module
from reckon.crew import review as review_module
from reckon.crew.runs import _write_json, pointer_path, run_dir

PROJECT = "waived-reach-fixture"
PLAN = "waived-reach-target"

#: The command the project armed, and the command both arms ran, so the pair is
#: matched: nothing is missing and the promotion reports what it found.
ARMED_COMMAND = "pytest -q tests/"

#: Why a run's suite delta stands waived.
WAIVER_REASON = (
    "both arms abort inside the backend compilation, at base and head alike, so "
    "neither reaches a pytest summary and neither counts a failure"
)
#: The heading a waived run's brief carries, as a reviewer reads it. Spelled
#: here rather than imported from the module, so this test measures the text
#: the brief carries and not the existence of the name that writes it.
WAIVED_HEADING = "Suite delta waived"

#: Why the review itself carries no count.
REVIEW_WAIVER = (
    "no comparable arm pair exists: both arms abort before pytest prints a "
    "summary, so this review states that there is no count rather than a zero"
)


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)


def _revisions(repository: Path) -> tuple[str, str]:
    """The fixture's base and head revisions."""

    def _rev(name: str) -> str:
        return subprocess.run(
            ["git", "rev-parse", name],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    return _rev("HEAD~1"), _rev("HEAD")


@pytest.fixture()
def repository(isolated_reckon_home: Path, tmp_path: Path) -> Path:
    """A repository holding a base commit and the head a run delivers."""
    assert isolated_reckon_home.is_dir()
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(origin)],
        check=True,
        capture_output=True,
    )
    seed = tmp_path / "seed"
    seed.mkdir()
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(seed, *arguments)
    (seed / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(seed, "add", "seed.txt")
    _git(seed, "commit", "-q", "-m", "seed repository")
    (seed / "reckon").mkdir()
    (seed / "reckon" / "example.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(seed, "add", "reckon/example.py")
    _git(seed, "commit", "-q", "-m", "the run's change")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "origin", "main")
    root = tmp_path / "repo"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(root)],
        check=True,
        capture_output=True,
    )
    for arguments in (
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    (root / "docs" / "state" / PROJECT).mkdir(parents=True, exist_ok=True)
    (isolated_reckon_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _suite_observation(revision: str) -> dict[str, object]:
    return {
        "revision": revision,
        "command": ARMED_COMMAND,
        "exit_status": 0,
        "log_path": f"/durable/{revision}.log",
        "completed": True,
        "failure_count": 0,
        "failure_ids": [],
    }


def _write_run(repository: Path, tmp_path: Path, run_id: str) -> tuple[str, str]:
    """A live run whose manifest names a matched arm pair."""
    base_sha, head_sha = _revisions(repository)
    manifest = tmp_path / f"{run_id}.md"
    manifest.write_text(
        "\n".join(
            [
                "node: node-a",
                "status: complete",
                f"commits: {head_sha}",
                "tests: done",
                "baseline_suite: " + json.dumps(_suite_observation(base_sha)),
                "after_suite: " + json.dumps(_suite_observation(head_sha)),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    # The landing merges into the mount's repository, so the run's worktree is
    # a checkout of its own.
    worktree = tmp_path / f"worktree-{run_id}"
    _git(repository, "worktree", "add", "--detach", str(worktree), head_sha)
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(worktree),
            "launch": "in-harness",
            "role": "implement",
            "member": "worker-a",
            "backend": "native",
            "created_at": "2026-11-18T00:00:00Z",
            "manifest_path": str(manifest),
            "base_sha": base_sha,
            "commits": [head_sha],
            "suite_command": ARMED_COMMAND,
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "section-one",
                "time_budget": "20m",
                "write_paths": [],
            },
        },
    )
    return base_sha, head_sha


def _record(run_id: str, base_sha: str, head_sha: str, **changes: object) -> dict:
    """A review record of the run's head, as the reviewer would write it."""
    emitted = "\n".join(
        f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
    )
    record = review_module.parse_review(emitted)
    record.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": f"r-20261118T000000000000-review-of-{run_id}",
            "reviewed_base_sha": base_sha,
            "reviewed_head_sha": head_sha,
            "added_failure_count": 0,
            "added_failure_ids": [],
            "findings": [],
            "status": "parsed",
        }
    )
    record.update(changes)
    return record


def _waived(**changes: object) -> dict:
    """The waived form: no count, no ids, and a stated reason.

    The count is the literal the contract names rather than the module's
    constant, so this record stays well formed at a revision that never defined
    the constant.
    """
    return {
        "added_failure_count": "waived",
        "added_failure_ids": [],
        "added_failure_waiver": REVIEW_WAIVER,
        **changes,
    }


def _complete(run_id: str, repository: Path, *extra: str, land: bool = False):
    """Promote the run through the CLI, presenting the commit it delivers."""
    head_sha = _revisions(repository)[1]
    return CliRunner().invoke(
        cli_main,
        [
            "crew",
            "complete",
            "--wait",
            "--run",
            run_id,
            "--gate",
            "passed",
            "--checkout-path",
            str(repository),
            "--gate-command",
            ARMED_COMMAND,
            "--gate-exit-status",
            "0",
            "--gate-log-path",
            "/durable/gate.log",
            "--commit",
            head_sha,
            *(["--land", "--wait"] if land else []),
            *extra,
        ],
    )


def _check_review(record_path: Path):
    return CliRunner().invoke(
        cli_main, ["crew", "check", "--review", "--record", str(record_path)]
    )


def _deliver(record: dict, directory: Path) -> Path:
    path = directory / f"{record['review_run_id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _landed_row(repository: Path, run_id: str) -> dict:
    """The committed ledger row promotion wrote for this run."""
    path = repository / "docs" / "state" / PROJECT / "runs" / f"{run_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _land(
    repository: Path, tmp_path: Path, run_id: str, *extra: str
) -> tuple[str, str, dict]:
    """Promote and land a run whose review is already stored, then read the row.

    The stored review is a measured one, because the run must land before its
    committed row can be judged: the case under test is what a review filed
    *after* the landing is told about the row that landing wrote.
    """
    base_sha, head_sha = _write_run(repository, tmp_path, run_id)
    assert review_module.store_review(_record(run_id, base_sha, head_sha)).is_file()
    landed = _complete(run_id, repository, *extra, land=True)
    assert landed.exit_code == 0, landed.output
    return base_sha, head_sha, _landed_row(repository, run_id)


def _drop_pointer(run_id: str) -> None:
    """Remove the live pointer, so only the committed row can answer."""
    path = pointer_path(run_id)
    if path.exists():
        path.unlink()
    assert not path.exists()


def test_a_waived_run_s_brief_names_the_waived_form(
    repository: Path, tmp_path: Path
) -> None:
    """The reviewer of a waived run is told how to file an absent count.

    The brief is composed from the pointer the ``crew complete
    --waive-suite-delta`` attempt wrote, which is where a run's waiver is
    recorded until its promotion writes a row. A run without that waiver is
    composed by the same function and must say nothing about the form.
    """
    waived_run = "r-20261118T000300000000-node-a"
    _base_sha, head_sha = _write_run(repository, tmp_path, waived_run)
    unwaived_run = "r-20261118T000400000000-node-a"
    _write_run(repository, tmp_path, unwaived_run)

    # The promotion records the waiver on the run even though it cannot land
    # yet: no review of the head is stored.
    recorded = _complete(waived_run, repository, "--waive-suite-delta", WAIVER_REASON)
    assert recorded.exit_code == 1, recorded.output
    waived = json.loads(pointer_path(waived_run).read_text())
    assert waived["suite_delta_waiver"]["reason"] == WAIVER_REASON

    brief = subject_module._review_brief_text(waived, head_sha)
    assert WAIVED_HEADING in brief
    assert "added_failure_waiver" in brief
    assert '"waived"' in brief
    assert WAIVER_REASON in brief

    unwaived = json.loads(pointer_path(unwaived_run).read_text())
    assert "suite_delta_waiver" not in unwaived
    plain = subject_module._review_brief_text(unwaived, head_sha)
    # The control is a brief, not an empty string: it carries the contract the
    # waived form would be an exception to.
    assert "Review record contract" in plain
    assert WAIVED_HEADING not in plain
    assert "added_failure_waiver" not in plain
    assert '"waived"' not in plain


def test_a_waived_count_is_accepted_from_a_clean_promoted_row(
    repository: Path, tmp_path: Path
) -> None:
    """A landed clean-with-reason row carries the waiver once the pointer is gone.

    ``crew complete --waive-suite-delta`` over a matched pair that adds no
    failure writes ``status clean`` with the reason beside it; nothing about
    that pair needed waiving, so the status names what the evidence found. A
    review filed after the pointer is dropped may therefore still file the
    waived count.
    """
    run_id = "r-20261118T000500000000-node-a"
    base_sha, head_sha, row = _land(
        repository,
        tmp_path,
        run_id,
        "--waive-suite-delta",
        WAIVER_REASON,
    )
    assert row["suite_delta"]["status"] == "clean"
    assert row["suite_delta"]["waiver_reason"] == WAIVER_REASON
    _drop_pointer(run_id)
    # The row is the only record left, and it is the reader that answers from
    # it — not a path the check happens to accept.
    assert review_module.recorded_suite_delta_waiver(run_id, PROJECT) == WAIVER_REASON

    record = _record(run_id, base_sha, head_sha, **_waived())
    delivered = _deliver(record, run_dir(run_id))
    check = _check_review(delivered)
    assert check.exit_code == 0, check.output
    assert review_module.store_review(dict(record)).is_file()


def test_a_waived_count_is_refused_from_a_row_with_no_waiver(
    repository: Path, tmp_path: Path
) -> None:
    """A row carrying neither spelling of the waiver authorises no waived count.

    The same record, the same landed run, one difference: the landing was given
    no reason, so its row records a clean pair with no waiver. The form is the
    run's, not the reviewer's, and the pointer is gone so only the row can say.
    """
    run_id = "r-20261118T000600000000-node-a"
    base_sha, head_sha, row = _land(repository, tmp_path, run_id)
    assert row["suite_delta"]["status"] == "clean"
    assert row["suite_delta"]["waiver_reason"] is None
    _drop_pointer(run_id)
    assert review_module.recorded_suite_delta_waiver(run_id, PROJECT) == ""

    record = _record(run_id, base_sha, head_sha, **_waived())
    delivered = _deliver(record, run_dir(run_id))
    check = _check_review(delivered)
    assert check.exit_code == 1, check.output
    assert "no suite-delta waiver" in check.output
    with pytest.raises(ValueError, match="no suite-delta waiver"):
        review_module.store_review(dict(record))
