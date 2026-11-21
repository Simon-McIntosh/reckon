"""A review files a waived failure count against a run whose suite delta is waived.

A run gated on a GPU cannot land through ``crew complete`` when its CPU suite
arms abort inside the backend compilation at base and head alike: neither arm
reaches a pytest summary, so the review can derive no added-failure count, and
a null ``added_failure_count`` is refused by the delivery contract. The landing
then refuses the review whatever the run's suite-delta waiver says, so
``--waive-suite-delta`` is never reached.

A review may therefore state that no count exists. The record carries
``added_failure_count`` as the literal ``"waived"``, an empty
``added_failure_ids`` and a non-empty ``added_failure_waiver`` naming why, and
the form is accepted only against a run that carries the suite-delta waiver
``crew complete --waive-suite-delta`` records on it:

* filed, ``crew check --review`` and ``crew complete --land`` accept that form
  for a waived run, and the landed row records both reasons;
* the same record against a run with no waiver is refused by the store and by
  the delivery check, naming the missing waiver;
* a null count is refused exactly as it was, so an absent count never reads as
  a measured zero;
* an integer count is filed and landed exactly as it was.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon.cli import main as cli_main
from reckon.crew import review as review_module
from reckon.crew.runs import _write_json, pointer_path, run_dir

PROJECT = "waived-review-fixture"
PLAN = "waived-review-target"

#: The command the project armed, pinned to the CPU these programs die on.
ARMED_COMMAND = "JAX_PLATFORMS=cpu pytest -q tests/"
#: The command the run's arms actually ran.
ARMED_BY_RUN = "pytest -q tests/"

#: Why the run's suite delta stands waived.
WAIVER_REASON = (
    "both arms abort inside the backend compilation, at base and head alike, so "
    "neither reaches a pytest summary and neither counts a failure"
)
#: Why the review carries no count of its own.
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
    """A repository holding a base commit and the head the run delivers."""
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


def _suite_observation(command: str, revision: str) -> dict[str, object]:
    return {
        "revision": revision,
        "command": command,
        "exit_status": 0,
        "log_path": f"/durable/{revision}.log",
        "completed": True,
        "failure_count": 0,
        "failure_ids": [],
    }


def _write_manifest(
    path: Path,
    *,
    baseline: dict[str, object],
    after: dict[str, object],
    commits: list[str],
) -> None:
    path.write_text(
        "\n".join(
            [
                "node: node-a",
                "status: complete",
                "commits: " + " ".join(commits),
                "tests: done",
                "baseline_suite: " + json.dumps(baseline),
                "after_suite: " + json.dumps(after),
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _write_run(
    repository: Path,
    tmp_path: Path,
    run_id: str,
    *,
    arms_command: str,
) -> tuple[str, str]:
    """A live run whose manifest names an arm pair run with ``arms_command``."""
    base_sha, head_sha = _revisions(repository)
    manifest = tmp_path / f"{run_id}.md"
    _write_manifest(
        manifest,
        baseline=_suite_observation(arms_command, base_sha),
        after=_suite_observation(arms_command, head_sha),
        commits=[head_sha],
    )
    # The landing merges into the mount's repository, so the run's worktree is a
    # checkout of its own: the promotion writes the ledger row on the branch and
    # a worktree that was the branch would move out from under its own head.
    worktree = tmp_path / "worktree"
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
    constant, so this record is well formed at a revision that never defined the
    constant: the refusal an older revision earns for it is the measurement.
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
            "pytest -q tests/",
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


def test_a_waived_count_files_checks_and_lands_a_waived_run(
    repository: Path, tmp_path: Path
) -> None:
    """The waived form is filed, passes the check, and lands with both reasons."""
    run_id = "r-20261118T000100000000-node-a"
    base_sha, head_sha = _write_run(
        repository, tmp_path, run_id, arms_command=ARMED_BY_RUN
    )
    # The promotion that states the waiver records it on the run even though it
    # cannot land yet: no review of the head is stored.
    first = _complete(run_id, repository, "--waive-suite-delta", WAIVER_REASON)
    assert first.exit_code == 1, first.output

    record = _record(run_id, base_sha, head_sha, **_waived())
    delivered = _deliver(record, run_dir(run_id))

    # The refusal this check earns at the revision before the waived form
    # existed is "missing or invalid added_failure_count", and it is the first
    # thing this case measures.
    check = _check_review(delivered)
    assert check.exit_code == 0, check.output

    # The promotion above recorded the waiver on the run even though it could
    # not land: the waiver is what the review is composed against.
    pointer = json.loads(pointer_path(run_id).read_text())
    assert pointer["suite_delta_waiver"]["reason"] == WAIVER_REASON

    filed = review_module.store_review(dict(record))
    assert filed.is_file()

    landed = _complete(
        run_id, repository, "--waive-suite-delta", WAIVER_REASON, land=True
    )
    assert landed.exit_code == 0, landed.output
    row = _landed_row(repository, run_id)
    assert row["suite_delta"]["status"] == "waived"
    assert row["suite_delta"]["waiver_reason"] == WAIVER_REASON
    assert (
        row["review"]["added_failure_count"] == review_module.WAIVED_ADDED_FAILURE_COUNT
    )
    assert row["review"]["added_failure_waiver"] == REVIEW_WAIVER


def test_the_waived_count_is_refused_for_a_run_with_no_waiver(
    repository: Path, tmp_path: Path
) -> None:
    """The form is the run's, not the reviewer's: no waiver, no waived count."""
    run_id = "r-20261118T000200000000-node-a"
    base_sha, head_sha = _write_run(
        repository, tmp_path, run_id, arms_command=ARMED_BY_RUN
    )
    assert "suite_delta_waiver" not in json.loads(pointer_path(run_id).read_text())
    record = _record(run_id, base_sha, head_sha, **_waived())
    delivered = _deliver(record, run_dir(run_id))

    check = _check_review(delivered)
    assert check.exit_code == 1, check.output
    assert "no suite-delta waiver" in check.output

    with pytest.raises(ValueError, match="no suite-delta waiver"):
        review_module.store_review(dict(record))


def test_a_null_count_is_refused_against_a_waived_run(
    repository: Path, tmp_path: Path
) -> None:
    """An absent count is still no count, however the run's delta stands."""
    run_id = "r-20261118T000300000000-node-a"
    base_sha, head_sha = _write_run(
        repository, tmp_path, run_id, arms_command=ARMED_BY_RUN
    )
    first = _complete(run_id, repository, "--waive-suite-delta", WAIVER_REASON)
    assert first.exit_code == 1, first.output
    pointer = json.loads(pointer_path(run_id).read_text())
    assert pointer["suite_delta_waiver"]["reason"] == WAIVER_REASON

    record = _record(
        run_id, base_sha, head_sha, added_failure_count=None, added_failure_ids=[]
    )
    delivered = _deliver(record, run_dir(run_id))

    check = _check_review(delivered)
    assert check.exit_code == 1, check.output
    assert "missing or invalid added_failure_count" in check.output


def test_an_integer_count_files_and_lands_as_it_did(
    repository: Path, tmp_path: Path
) -> None:
    """The measured form is untouched: filed, checked and landed with a zero."""
    run_id = "r-20261118T000400000000-node-a"
    base_sha, head_sha = _write_run(
        repository, tmp_path, run_id, arms_command=ARMED_COMMAND
    )
    record = _record(run_id, base_sha, head_sha)
    delivered = _deliver(record, run_dir(run_id))

    check = _check_review(delivered)
    assert check.exit_code == 0, check.output

    assert review_module.store_review(dict(record)).is_file()
    landed = _complete(run_id, repository, land=True)
    assert landed.exit_code == 0, landed.output
    row = _landed_row(repository, run_id)
    assert row["suite_delta"]["status"] == "clean"
    assert row["suite_delta"]["waiver_reason"] is None
    assert row["review"]["total"] == 120, row["review"]
    assert "added_failure_count" not in row["review"], row["review"]
