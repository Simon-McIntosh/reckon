"""A completion whose suite arms never reached a count records them as unmeasured.

A run gated on a GPU cannot run its armed suite on the CPU it was promoted for.
Its arms abort inside the backend compilation at base and head alike, so neither
reaches a pytest summary: both carry ``completed`` false and a null
``failure_count``. Promotion read such a pair as a measured one and called
``int(None)`` on the count, so a completion whose waiver was already recorded and
whose waived-count review was already filed raised ``TypeError`` out of
``ledger.normalized_suite_observation`` instead of landing or refusing.

An arm that never reached a summary is an unmeasured reading rather than a
malformed record, and the waiver is the statement of why. So:

* under ``--waive-suite-delta`` the pair is recorded with both arms unmeasured
  and the waiver's own reason beside them, and the run promotes and lands;
* without the waiver the same completion refuses, naming the uncounted arm and
  pointing at the waiver, and it raises no traceback;
* a pair that did count its failures is recorded exactly as before, so the
  measured shape gains nothing from this change.
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

PROJECT = "uncounted-arms-fixture"
PLAN = "uncounted-arms-target"

#: The command the project armed for this node.
ARMED_COMMAND = "JAX_PLATFORMS=cpu pytest -q tests/"

#: Why neither arm reached a count.
WAIVER_REASON = (
    "both arms abort with SIGABRT inside the backend compilation, at base and "
    "head alike, so neither reaches a pytest summary and neither counts a "
    "failure"
)
#: Why the review files no count of its own.
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

    return _rev("HEAD~1"), _revisions_head(repository)


def _revisions_head(repository: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


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


def _counted_arm(command: str, revision: str) -> dict[str, object]:
    """An arm that reached its summary and found no failure."""
    return {
        "revision": revision,
        "command": command,
        "exit_status": 0,
        "log_path": f"/durable/{revision}.log",
        "completed": True,
        "failure_count": 0,
        "failure_ids": [],
    }


def _uncounted_arm(command: str, revision: str) -> dict[str, object]:
    """An arm whose program aborted before pytest printed a summary."""
    return {
        "revision": revision,
        "command": command,
        "exit_status": -6,
        "log_path": f"/durable/{revision}.log",
        "completed": False,
        "failure_count": None,
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
    counted: bool,
    measurable: bool = True,
) -> tuple[str, str]:
    """A live run whose manifest names an arm pair for this project's command,
    with the waiver the completion recorded on it already present.

    ``measurable`` is false for a run the landing cannot take a reading of: its
    record names no worktree that exists, so the promotion falls back to the
    arms the run itself recorded rather than re-running them.
    """
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD~1"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    head_sha = _revisions_head(repository)
    arm = _counted_arm if counted else _uncounted_arm
    manifest = tmp_path / f"{run_id}.md"
    _write_manifest(
        manifest,
        baseline=arm(ARMED_COMMAND, base_sha),
        after=arm(ARMED_COMMAND, head_sha),
        commits=[head_sha],
    )
    worktree = tmp_path / f"{run_id}-worktree"
    if measurable:
        _git(repository, "worktree", "add", "--detach", str(worktree), head_sha)
    pointer: dict[str, object] = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repository),
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
    }
    if measurable:
        pointer["worktree"] = str(worktree)
    _write_json(pointer_path(run_id), pointer)
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
            "review_run_id": f"r-20261118T020000000000-review-of-{run_id}",
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
    """The waived form: no count, no ids, and a stated reason."""
    return {
        "added_failure_count": "waived",
        "added_failure_ids": [],
        "added_failure_waiver": REVIEW_WAIVER,
        **changes,
    }


def _deliver(record: dict, directory: Path) -> Path:
    path = directory / f"{record['review_run_id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _complete(
    run_id: str,
    repository: Path,
    *extra: str,
    land: bool = False,
    strict: bool = True,
):
    """Promote the run through the CLI, presenting the commit it delivers."""
    head_sha = _revisions_head(Path(str(repository)))
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
        catch_exceptions=not strict,
    )


def _check_review(path: Path):
    return CliRunner().invoke(
        cli_main, ["crew", "check", "--review", "--record", str(path)]
    )


def _landed_row(repository: Path, run_id: str) -> dict:
    """The committed ledger row promotion wrote for this run."""
    path = repository / "docs" / "state" / PROJECT / "runs" / f"{run_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_waived_uncounted_pair_lands_recorded_as_unmeasured(
    repository: Path, tmp_path: Path
) -> None:
    """The waiver states that no count exists, and the row keeps that statement.

    The completion that records the waiver cannot land yet — no review of the
    head is stored — so the waiver is on the run before the waived-count review
    is filed. The landing that follows is the one that raised ``TypeError`` on
    an uncounted pair.
    """
    run_id = "r-20261118T020100000000-node-a"
    base_sha, head_sha = _write_run(repository, tmp_path, run_id, counted=False)

    first = _complete(run_id, repository, "--waive-suite-delta", WAIVER_REASON)
    assert first.exit_code == 1, first.output
    pointer = json.loads(pointer_path(run_id).read_text())
    assert pointer["suite_delta_waiver"]["reason"] == WAIVER_REASON

    record = _record(run_id, base_sha, head_sha, **_waived())
    check = _check_review(_deliver(record, run_dir(run_id)))
    assert check.exit_code == 0, check.output
    review_module.store_review(dict(record))

    landed = _complete(
        run_id,
        repository,
        "--waive-suite-delta",
        WAIVER_REASON,
        land=True,
        strict=False,
    )
    assert landed.exit_code == 0, landed.output

    delta = _landed_row(repository, run_id)["suite_delta"]
    assert delta["status"] == "waived"
    assert delta["waiver_reason"] == WAIVER_REASON
    assert delta["added_failure_ids"] == []
    for name in ("baseline_suite", "after_suite"):
        arm = delta[name]
        assert arm["unmeasured"] is True
        assert arm["completed"] is False
        assert arm["failure_count"] is None
        assert arm["unmeasured_reason"] == WAIVER_REASON
    waived = delta["waived_arms"]
    assert waived["reason"] == WAIVER_REASON
    assert waived["baseline_suite"]["unmeasured"] is True
    assert waived["after_suite"]["unmeasured"] is True


def test_the_same_pair_without_a_waiver_is_refused_naming_the_arm(
    repository: Path, tmp_path: Path
) -> None:
    """An uncounted arm is refused as one the completion would have to waive.

    The refusal names the arm that never counted, offers the waiver that would
    record it as unmeasured, and is a refusal rather than a traceback: a
    promotion the run cannot land halts with a reason its coordinator can act
    on, so nothing about the raise is reachable from here.
    """
    run_id = "r-20261118T020200000000-node-a"
    base_sha, head_sha = _write_run(
        repository, tmp_path, run_id, counted=False, measurable=False
    )
    review_module.store_review(_record(run_id, base_sha, head_sha))

    result = _complete(run_id, repository, land=True)

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["error"] == "suite-delta-refused"
    assert "Traceback" not in result.output
    assert "TypeError" not in result.output
    # The command's own reply summarises the run; the outcome it wrote beside it
    # carries the refusal's detail and the fields that were missing.
    refusal = json.loads(Path(payload["record_path"]).read_text(encoding="utf-8"))
    detail = refusal["detail"]
    assert "baseline_suite" in detail
    assert "after_suite" in detail
    assert "never reached a pytest summary" in detail
    assert "--waive-suite-delta" in detail
    assert refusal["missing_fields"] == [
        "baseline_suite.completed",
        "baseline_suite.failure_count",
        "after_suite.completed",
        "after_suite.failure_count",
    ]
    row = repository / "docs" / "state" / PROJECT / "runs" / f"{run_id}.json"
    assert not row.exists()


def test_a_counted_pair_is_recorded_exactly_as_before(
    repository: Path, tmp_path: Path
) -> None:
    """A measured pair gains nothing: the recorded shape is the base revision's."""
    run_id = "r-20261118T020300000000-node-a"
    base_sha, head_sha = _write_run(repository, tmp_path, run_id, counted=True)
    review_module.store_review(_record(run_id, base_sha, head_sha))

    landed = _complete(run_id, repository, land=True, strict=False)

    assert landed.exit_code == 0, landed.output
    delta = _landed_row(repository, run_id)["suite_delta"]
    assert delta["status"] == "clean"
    assert delta["waived_arms"] is None
    assert delta["baseline_suite"] == {
        "revision": base_sha,
        "command": ARMED_COMMAND,
        "exit_status": 0,
        "log_path": f"/durable/{base_sha}.log",
        "log_digest": None,
        "completed": True,
        "failure_count": 0,
        "failure_ids": [],
    }
    assert delta["after_suite"]["revision"] == head_sha
    assert delta["after_suite"]["completed"] is True
