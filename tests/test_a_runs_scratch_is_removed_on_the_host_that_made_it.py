"""A run's scratch leaves with the run, on every host that holds a copy.

Node-local scratch is not shared between hosts: the crew home is, so a host can
see that a run ended, but the copy a worker made on its own node is invisible
from the host that promotes it. A release therefore removes only the copy on
the host that runs it, and every other copy needs a pass that runs on its own
host. These cases hold that division to its consequences in a temporary crew
home and a temporary scratch root:

* a release removes this host's copy and its timestamp-stem sibling, records
  that it removed them and names the host;
* a release on a host holding no copy reports nothing removed rather than a
  failure;
* a launch settles this host's copy of a finished run, and so does the hourly
  retention pass;
* a live pointer, or a process holding the directory, keeps the copy;
* a copy whose run has no pointer and no run directory waits for the orphan
  grace, and is removed once it passes;
* a step-placed worker settles the copy on the node it starts on;
* a review run's scratch is removed by its promotion, and its release receipt
  survives a landing commit that is refused.

The declared mutation: a settling pass that keeps the orphan sweep's age wait
leaves the fresh copy of a finished run in place, and the second-host case
fails.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import socket
import subprocess
import time
from pathlib import Path

import pytest

from reckon import _plan_html, ledger
from reckon.crew import gc, promotion
from reckon.crew import promotion_release as release_module
from reckon.crew import review as review_module
from reckon.crew.gc import SCRATCH_GRACE_SECONDS, settle_run_scratch
from reckon.crew.runs import _write_json, pointer_path, run_dir
from tests.helpers.git import git as _git

# `reckon.crew.dispatch` is shadowed by the dispatch function the package
# re-exports, so the module is reached by importlib rather than by attribute.
dispatch = importlib.import_module("reckon.crew.dispatch")
launch = importlib.import_module("reckon.crew.dispatch_launch")

PROJECT = "proj"


@pytest.fixture()
def scratch_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scratch root under the test's own temp directory, never the host's."""
    root = (tmp_path / "scratch").resolve()
    monkeypatch.setenv(dispatch.WORKER_SCRATCH_ROOT_ENV, str(root))
    return root


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A crew home under the test's temp directory, shared by every "host"."""
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.delenv("RECKON_RUN_STORE", raising=False)
    home.mkdir()
    return home


def _run_copy(run_id: str) -> Path:
    """The scratch directory a worker on this host leaves for its run."""
    directory = dispatch.ensure_worker_scratch(run_id)
    (directory / "worker-temp.txt").write_text("scratch\n", encoding="utf-8")
    return directory


def _finished_run_directory(run_id: str) -> Path:
    """A run directory recording that its run ended, as a supervisor leaves."""
    directory = run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "exit.json").write_text(
        json.dumps({"run_id": run_id, "exited_at": "2026-10-24T00:00:00Z"}) + "\n",
        encoding="utf-8",
    )
    return directory


def _live_pointer(run_id: str) -> Path:
    """A run still in flight: its pointer exists and nothing may be removed."""
    path = pointer_path(run_id)
    _write_json(
        path,
        {
            "run_id": run_id,
            "project": PROJECT,
            "role": "implement",
            "repair": None,
            "scratch": str(dispatch.worker_scratch_dir(run_id)),
        },
    )
    return path


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repository"
    (repo / "docs" / "state" / PROJECT).mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "seed.txt", "docs")
    _git(repo, "commit", "-q", "-m", "seed repository")
    return repo


RUN = "r-20261024T000000000000-settled-run"

REVIEW_PLAN = "review-scratch-target"
REVIEWED_RUN = "r-20261024T000000000002-reviewed-run"
REVIEW_RUN = "r-20261024T000000000003-review-of-reviewed-run"
REVIEW_BASE = "b" * 40
REVIEW_HEAD = "a" * 40


def _review_repository(crew_home: Path, tmp_path: Path) -> Path:
    """A committed repository mounted as the project, holding the plan it reads."""
    repo = _repository(tmp_path)
    plan = repo / "docs" / "plans" / f"{REVIEW_PLAN}.html"
    plan.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{REVIEW_PLAN}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    plan.write_text(
        _plan_html.write_state(
            bare,
            {
                "type": "plan",
                "slug": REVIEW_PLAN,
                "title": "Review Scratch Target",
                "status": "active",
                "version": 0,
                "comments": {},
            },
        ),
        encoding="utf-8",
    )
    _git(repo, "add", "docs")
    _git(repo, "commit", "-q", "-m", "add the plan the review reads")
    (crew_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    return repo


def _review_record_path() -> Path:
    """The head-keyed review-store path the reviewer was dispatched to write."""
    return review_module.review_path(
        PROJECT, REVIEWED_RUN, reviewed_head_sha=REVIEW_HEAD
    )


def _store_review_record() -> Path:
    record = {
        "status": "parsed",
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN,
        "review_run_id": REVIEW_RUN,
        "reviewed_base_sha": REVIEW_BASE,
        "reviewed_head_sha": REVIEW_HEAD,
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 20),
        "total": 20 * len(review_module.REVIEW_DIMENSIONS),
        "findings": [],
        "timestamp": "2026-10-24T00:10:00Z",
    }
    path = _review_record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _review_pointer(repository: Path, tmp_path: Path) -> Path:
    """A finished review run's pointer, as its launch left it."""
    manifest = tmp_path / "manifests" / f"{REVIEW_RUN}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: review-of-reviewed-run\n"
        "status: complete\n"
        "checkpoint: the review record is stored\n",
        encoding="utf-8",
    )
    node = {
        "id": f"review-of-{REVIEWED_RUN}",
        "plan": REVIEW_PLAN,
        "section": "s2",
        "time_budget": "25m",
        "role": "review",
        "write_paths": [str(_review_record_path())],
    }
    pointer = {
        "run_id": REVIEW_RUN,
        "project": PROJECT,
        "repo": str(repository),
        "launch": "in-harness",
        "role": "review",
        "backend": "native",
        "created_at": "2026-10-24T00:00:00Z",
        "manifest_path": str(manifest),
        "node": node,
        "scratch": str(dispatch.worker_scratch_dir(REVIEW_RUN)),
    }
    return _write_json(pointer_path(REVIEW_RUN), pointer)


def test_a_release_removes_this_hosts_copy_and_its_stem_sibling(
    scratch_root: Path, crew_home: Path
) -> None:
    copy = _run_copy(RUN)
    sibling = scratch_root / (
        re.match(r"r-\d{8}T\d{12}", RUN).group(0) + "-second-tree"
    )
    sibling.mkdir()
    (sibling / "left-behind.txt").write_text("x\n", encoding="utf-8")

    result = dispatch.remove_worker_scratch(RUN)

    assert result["scratch_removed"] is True
    assert result["scratch_removed_paths"]
    assert result["scratch_host"] == socket.gethostname()
    assert not copy.exists()
    assert not sibling.exists()


def test_a_release_on_a_host_holding_no_copy_reports_nothing_removed(
    scratch_root: Path, crew_home: Path
) -> None:
    result = dispatch.remove_worker_scratch(RUN)

    assert result["scratch_removed"] is False
    assert result["scratch_absent"] is True
    assert result["scratch_removed_paths"] == []
    assert result["scratch_host"] == socket.gethostname()


def test_a_launch_settles_a_finished_runs_copy_on_this_host(
    scratch_root: Path, crew_home: Path
) -> None:
    """The second host: a launch removes a finished run's copy, whatever its age."""
    copy = _run_copy(RUN)
    _finished_run_directory(RUN)
    fresh = time.time() - copy.stat().st_ctime
    assert fresh < SCRATCH_GRACE_SECONDS

    dispatch.ensure_worker_scratch("r-20261024T000000000001-another-run")

    assert not copy.exists()


def test_a_retention_sweep_settles_a_finished_runs_copy(
    scratch_root: Path,
    crew_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copy = _run_copy(RUN)
    _finished_run_directory(RUN)
    (crew_home / "mounts.json").write_text("{}", encoding="utf-8")
    log = tmp_path / "retention.log"

    report = gc.retention_sweep(apply=True, log=log)

    assert not copy.exists()
    assert str(copy) in report["scratch_removed"]
    assert str(copy) in log.read_text(encoding="utf-8")


def test_a_live_pointer_keeps_its_copy(scratch_root: Path, crew_home: Path) -> None:
    copy = _run_copy(RUN)
    _finished_run_directory(RUN)
    _live_pointer(RUN)

    settle_run_scratch(apply=True)

    assert copy.exists()


def test_a_copy_a_process_holds_is_left(scratch_root: Path, crew_home: Path) -> None:
    copy = _run_copy(RUN)
    _finished_run_directory(RUN)
    held = copy / "open.bin"
    held.write_text("held\n", encoding="utf-8")

    with held.open("r"):
        settle_run_scratch(apply=True)

    assert copy.exists()


def test_a_private_home_never_settles_the_host_wide_scratch(
    scratch_root: Path, crew_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A home that cannot see the live pointers must not delete their copies."""
    copy = _run_copy(RUN)
    _finished_run_directory(RUN)
    # The private home stays; only the declaration that pairs it with a scratch
    # root is withdrawn, as a caller that sets RECKON_HOME alone leaves it.
    monkeypatch.delenv(dispatch.WORKER_SCRATCH_ROOT_ENV)
    monkeypatch.setattr(dispatch, "worker_scratch_root", lambda: scratch_root)

    result = settle_run_scratch(apply=True)

    assert result["withheld"] == "state and scratch roots differ"
    assert result["removed"] == []
    assert copy.exists()
    assert copy.is_dir()


def test_naming_only_the_scratch_root_withholds(
    scratch_root: Path, crew_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scratch root named alone is still paired with an unnamed state home."""
    copy = _run_copy(RUN)
    _finished_run_directory(RUN)
    monkeypatch.delenv(dispatch.WORKER_SCRATCH_ROOT_ENV)

    result = settle_run_scratch(apply=True, root=scratch_root)

    assert result["withheld"] == "state and scratch roots differ"
    assert result["removed"] == []
    assert copy.exists()


def test_naming_both_trees_lets_the_pass_remove(
    scratch_root: Path, crew_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller that relocates both declares both and is settled as usual."""
    copy = _run_copy(RUN)
    _finished_run_directory(RUN)
    monkeypatch.delenv(dispatch.WORKER_SCRATCH_ROOT_ENV)

    result = settle_run_scratch(
        apply=True, root=scratch_root, runs_root=run_dir(RUN).parent
    )

    assert "withheld" not in result
    assert str(copy) in result["removed"]
    assert not copy.exists()


def test_an_orphan_copy_waits_for_the_grace(
    scratch_root: Path, crew_home: Path
) -> None:
    """No pointer and no run directory: the age rule alone decides."""
    copy = _run_copy(RUN)
    now = time.time()

    settle_run_scratch(apply=True, now=now)
    assert copy.exists()

    settle_run_scratch(apply=True, now=now + SCRATCH_GRACE_SECONDS + 1)
    assert not copy.exists()


def test_a_step_on_a_third_host_settles_its_copy(
    scratch_root: Path, crew_home: Path, tmp_path: Path
) -> None:
    """A node that only runs steps never launches, so the step start settles."""
    copy = _run_copy(RUN)
    _finished_run_directory(RUN)
    environment = dict(os.environ)
    environment.update(
        {
            launch.STEP_NODE_DIRS_ENV: "",
            "RECKON_STEP_RECORD": str(tmp_path / "step-record"),
            "SLURM_JOB_ID": "1",
            "SLURM_STEP_ID": "1",
        }
    )

    completed = subprocess.run(
        ["bash", "-c", launch._STEP_TASK_WRAPPER, "step", "/bin/true"],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert not copy.exists()


def test_a_promoted_review_runs_scratch_is_removed_by_its_promotion(
    scratch_root: Path, crew_home: Path
) -> None:
    copy = _run_copy(RUN)
    _finished_run_directory(RUN)
    record = {
        "run_id": RUN,
        "project": PROJECT,
        "role": "review",
        "scratch": str(copy),
        "worktree": None,
        "pid": None,
    }

    release = release_module._release_after_promotion(RUN, record, gate="not-run")

    assert release["scratch_removed"] is True
    assert release["scratch_host"] == socket.gethostname()
    assert not copy.exists()


def test_a_review_promoted_through_the_complete_route_removes_its_scratch(
    scratch_root: Path, crew_home: Path, tmp_path: Path
) -> None:
    """The production entry point, not the release function behind it.

    A review run is promoted by the same route every other run takes: the crew
    complete verb calls ``promotion.complete``, which folds the record, releases
    the workspace and records the release on the ledger row.
    """
    repository = _review_repository(crew_home, tmp_path)
    _store_review_record()
    _review_pointer(repository, tmp_path)
    _finished_run_directory(REVIEW_RUN)
    copy = _run_copy(REVIEW_RUN)

    promotion.complete(
        REVIEW_RUN, gate="not-run", root=repository, promoted_by="review-self"
    )

    assert not copy.exists()
    [row] = ledger.runs(PROJECT, root=repository)
    release = row["release"]
    assert release["scratch_removed"] is True
    assert release["scratch_host"] == socket.gethostname()
    # The receipt also lands beside the run's own record, so it survives a
    # landing commit that is rolled back, and it names the same host.
    receipt = json.loads(
        (run_dir(REVIEW_RUN) / release_module.RELEASE_RECEIPT_NAME).read_text(
            encoding="utf-8"
        )
    )
    assert receipt["scratch_removed"] is True
    assert receipt["scratch_host"] == socket.gethostname()


def test_a_release_receipt_survives_a_refused_landing_commit(
    scratch_root: Path,
    crew_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route that lost the receipt: a landing commit restores its own write.

    A release whose ledger commit cannot stage is rolled back to HEAD, taking
    the receipt with it, so the receipt also lands beside the run's own record.
    """
    _run_copy(RUN)
    _finished_run_directory(RUN)
    record = {"run_id": RUN, "project": PROJECT, "role": "review", "worktree": None}
    release = release_module._release_after_promotion(RUN, record, gate="not-run")
    repo = _repository(tmp_path)
    (repo / "docs" / "state" / PROJECT / "runs").mkdir(parents=True, exist_ok=True)
    ledger.write_run_file(PROJECT, {"run_id": RUN, "project": PROJECT}, repo)

    def refused(*args: object, **kwargs: object) -> dict[str, object]:
        raise RuntimeError("the landing commit could not stage its writes")

    monkeypatch.setattr(release_module, "_commit_landing_writes", refused)
    with pytest.raises(RuntimeError):
        release_module._record_release_on_ledger(
            project=PROJECT,
            root=repo,
            run_id=RUN,
            release=release,
            checkout=repo,
        )

    receipt = json.loads(
        (run_dir(RUN) / release_module.RELEASE_RECEIPT_NAME).read_text(encoding="utf-8")
    )
    assert receipt["scratch_removed"] is True
    assert receipt["scratch_host"] == socket.gethostname()
