"""A killed promotion writes nothing after it dies.

A ``crew complete`` promotion is killed with SIGTERM at each of the three
stage boundaries the plan names. The promotion runs in a child process of its
own session and the signal is delivered from a stub at the boundary, so the
facts a later reader needs are observed from outside the promotion:

* the boundary was reached, so each case measures the stage it names rather
  than a promotion that failed earlier;
* nothing the promotion started outlived it;
* no commit, index entry, ledger row or plan byte changed after it died;
* the claim it held is released, so the next promotion or landing pass is not
  refused by a promotion that no longer exists.

The last one is the case the incident names: two promotions were killed by
``timeout`` and their commits landed afterwards, which reads as a writer
outliving its parent. The measurement here says where the survives-a-kill
damage actually bites: a killed promotion leaves its host claim fresh for the
stale window, and the retry a coordinator runs immediately afterwards is
refused as "already being promoted" by a dead pid.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from reckon import _plan_html
from reckon.crew.runs import _write_json, pointer_path
from tests.helpers.git import git as _git

PROJECT = "killed-promotion-fixture"
PLAN = "promotion-target"
RUN_ID = "r-20260918T090000000001-killed-promotion"

GATE_CHECK: dict[str, Any] = {
    "command": "pytest -q tests/test_a_killed_promotion_writes_nothing_after_it_dies.py",
    "exit_status": 0,
    "log_path": "gate.log",
    "log_digest": "sha256:0",
}

# Each boundary is the last write before the stage that follows it, so the stub
# delivers SIGTERM with the run in exactly the state that boundary names:
# before the promotion commit the ledger row and the plan comment are on disk
# and uncommitted; between the promote commit and the release the commit is on
# HEAD and the pointer still exists; during the release the tree has been
# released and only the release's own ledger record is outstanding.
BOUNDARIES: dict[str, tuple[str, str]] = {
    "before-promotion-commit": ("reckon.ledger", "append_run"),
    "between-promote-and-release": (
        "reckon.crew.promotion",
        "_commit_landing_writes",
    ),
    "during-release": ("reckon.crew.promotion", "_release_after_promotion"),
}

DRIVER = '''\
"""Drive one real `crew complete` promotion and stop it at a boundary."""
import importlib
import os
import signal
import sys
from pathlib import Path

repo, run_id, module_name, function_name, marker = sys.argv[1:6]
from reckon import crew

GATE_CHECK = {gate_check}

if module_name != "-":
    module = importlib.import_module(module_name)
    original = getattr(module, function_name)
    state = {{"fired": False}}

    def hook(*args, **kwargs):
        result = original(*args, **kwargs)
        if not state["fired"]:
            state["fired"] = True
            Path(marker).write_text(function_name, encoding="utf-8")
            os.kill(os.getpid(), signal.SIGTERM)
        return result

    setattr(module, function_name, hook)
    crew.complete(
        run_id,
        gate="passed",
        outcome="the record the killed promotion was writing",
        require_gate_check=True,
        gate_check=GATE_CHECK,
        root=Path(repo),
    )
else:
    from reckon.crew import pending_landing
    pending_landing.finish_pending_landings()
print("DRIVER_EXIT=completed")
'''

RESUME_DRIVER = '''\
"""Run the pass that finishes whatever the killed promotion left."""
import sys
from pathlib import Path

repo, run_id, mode = sys.argv[1:4]
GATE_CHECK = {gate_check}
if mode == "complete":
    from reckon import crew

    crew.complete(
        run_id,
        gate="passed",
        outcome="the record the killed promotion was writing",
        require_gate_check=True,
        gate_check=GATE_CHECK,
        root=Path(repo),
)
else:
    from reckon.crew import pending_landing

    pending_landing.finish_pending_landings()
print("DRIVER_EXIT=completed")
'''


def _write_plan(root: Path) -> Path:
    path = root / "docs" / "plans" / f"{PLAN}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html>"
        "<html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    state = {
        "type": "plan",
        "slug": PLAN,
        "title": "Promotion target",
        "status": "active",
        "version": 0,
        "comments": {},
    }
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")
    return path


def _pointer(repository: Path, run_id: str) -> None:
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "launch": "in-harness",
            "role": "implement",
            "member": "worker-a",
            "backend": "native",
            "created_at": "2026-09-18T08:00:00Z",
            "manifest_path": str(repository / "manifest.md"),
            "node": {
                "id": "promotion-target",
                "plan": PLAN,
                "section": "§2",
                "time_budget": "25m",
                "write_paths": [],
            },
        },
    )


@pytest.fixture(autouse=True)
def real_stores_are_not_fixture_targets() -> None:
    """No fixture may reach this checkout's plan, crew home or pointer."""
    checkout = Path(__file__).resolve().parents[1]
    checkout_plan = checkout / "docs" / "plans" / f"{PLAN}.html"
    checkout_state = checkout / "docs" / "state" / PROJECT
    real_pointer = (
        Path.home() / ".config" / "reckon" / "crew" / "live" / f"{RUN_ID}.json"
    )
    assert not checkout_plan.exists()
    assert not checkout_state.exists()
    assert not real_pointer.exists()
    yield
    assert not checkout_plan.exists()
    assert not checkout_state.exists()
    assert not real_pointer.exists()


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    _write_plan(root)
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs"),
        ("commit", "-q", "-m", "test: seed repository"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    _pointer(root, RUN_ID)
    return root


def _driver(tmp_path: Path) -> Path:
    path = tmp_path / "killed_promotion_driver.py"
    path.write_text(DRIVER.format(gate_check=repr(GATE_CHECK)), encoding="utf-8")
    return path


def _run_driver(
    tmp_path: Path,
    repository: Path,
    target: tuple[str, str] | None,
    name: str,
) -> tuple[subprocess.Popen, Path, Path]:
    """Run one promotion in its own session; return it, its marker and its log."""
    driver = _driver(tmp_path)
    marker = tmp_path / f"marker-{name}"
    log = tmp_path / f"driver-{name}.log"
    module_name, function_name = target or ("-", "-")
    arguments = [
        sys.executable,
        str(driver),
        str(repository),
        RUN_ID,
        module_name,
        function_name,
        str(marker),
    ]
    environment = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "TMPDIR": str(tmp_path),
    }
    with log.open("wb") as stream:
        process = subprocess.Popen(
            arguments,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=environment,
            cwd=tmp_path,
        )
    return process, marker, log


def _snapshot(repository: Path, crew_home: Path) -> dict[str, Any]:
    """Everything a promotion writes that a kill must not extend.

    HEAD, the index and the worktree status are read through git because a
    commit or a staging shows only there; the files are hashed because the
    ledger row and the plan comment are ordinary writes.
    """
    files: dict[str, str] = {}
    for path in sorted(repository.rglob("*")):
        if not path.is_file() or ".git" in path.parts:
            continue
        files[str(path.relative_to(repository))] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    home: dict[str, str] = {}
    for path in sorted(crew_home.rglob("*")):
        if path.is_file():
            home[str(path.relative_to(crew_home))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return {
        "head": _git(repository, "rev-parse", "HEAD"),
        "index": _git(repository, "ls-files", "--stage"),
        "status": _git(repository, "status", "--porcelain"),
        "files": files,
        "crew_home": home,
    }


def _session_endsw(process: subprocess.Popen, *, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.05)
    return False


def _kill_at_boundary(
    tmp_path: Path, repository: Path, boundary: str
) -> tuple[dict[str, Any], str]:
    """Kill a real promotion at ``boundary`` and return the state around death."""
    crew_home = Path(os.environ["RECKON_HOME"])
    process, marker, log = _run_driver(
        tmp_path, repository, BOUNDARIES[boundary], boundary
    )
    process.wait(timeout=240)
    text = log.read_text(encoding="utf-8", errors="replace")
    assert marker.is_file(), (
        f"the promotion never reached {boundary}, so this case measured "
        f"nothing:\n{text}"
    )
    at_death = _snapshot(repository, crew_home)
    assert _session_endsw(process, seconds=15.0), (
        f"a process the promotion started was still alive 15 s after it exited:\n{text}"
    )
    time.sleep(2.0)
    settled = _snapshot(repository, crew_home)
    assert settled == at_death, (
        "the promotion wrote after it exited: "
        f"{sorted(key for key in at_death if at_death[key] != settled[key])}"
    )
    return at_death, text


def _lease(crew_home: Path) -> Path:
    return crew_home / "crew" / "leases" / f"promotion-{RUN_ID}.json"


def _rows(repository: Path) -> list[dict[str, Any]]:
    directory = repository / "docs" / "state" / PROJECT / "runs"
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("*.json"))
    ]


@pytest.mark.parametrize("boundary", list(BOUNDARIES))
def test_no_write_follows_a_killed_promotion(
    repository: Path, tmp_path: Path, boundary: str
) -> None:
    """A promotion killed at ``boundary`` writes nothing after it exits."""
    _state, text = _kill_at_boundary(tmp_path, repository, boundary)
    crew_home = Path(os.environ["RECKON_HOME"])
    assert not _lease(crew_home).exists(), (
        "a killed promotion must release its claim: while the lease is fresh "
        "the very next promotion is refused as already running under a pid "
        f"that no longer exists. Driver output:\n{text}"
    )
    assert _git(repository, "diff", "--cached", "--name-only") == "", (
        "a killed promotion must not leave a forward write staged: staging "
        "would be relied on by nothing and swept into the next path-less "
        "commit a peer makes"
    )


@pytest.mark.parametrize("boundary", list(BOUNDARIES))
def test_each_boundary_leaves_the_state_its_name_asserts(
    repository: Path, tmp_path: Path, boundary: str
) -> None:
    """The three cases are three cases: each kill leaves its own named state.

    Before the promotion lands its record the row is on disk, the landing is
    uncommitted and the pointer still lives. Between the two commits the
    promotion is on HEAD, the tree is clean and the pointer still lives, so a
    retry finishes the release. During the release the pointer is already
    removed, so the run reads as promoted and only the release's own record is
    outstanding.
    """
    snapshot, text = _kill_at_boundary(tmp_path, repository, boundary)
    assert _rows(repository), f"{boundary} left no ledger row; driver: {text}"
    if boundary == "before-promotion-commit":
        assert snapshot["status"], (
            "the landing is still uncommitted before the promotion lands it"
        )
        assert pointer_path(RUN_ID).exists(), "the pointer is untouched"
    elif boundary == "between-promote-and-release":
        assert snapshot["status"] == "", "the promotion left a clean tree"
        assert pointer_path(RUN_ID).exists(), (
            "the pointer survives between the commits, so a retry finishes the release"
        )
    else:
        assert snapshot["status"] == "", "the promotion left a clean tree"
        assert not pointer_path(RUN_ID).exists(), (
            "the pointer is removed before the release, so the run reads as promoted"
        )


@pytest.mark.parametrize(
    ("boundary", "mode"),
    [
        ("before-promotion-commit", "complete"),
        ("between-promote-and-release", "complete"),
        ("during-release", "landing"),
    ],
)
def test_the_next_pass_completes_what_the_kill_left(
    repository: Path, tmp_path: Path, boundary: str, mode: str
) -> None:
    """Each boundary leaves a state the next promotion or landing pass finishes.

    The case that failed before this change is the first: a promotion killed
    before its landing leaves its host claim fresh, and the retry a
    coordinator runs a minute later is refused as already running under the
    pid that died. The other two boundaries leave a run that is already
    promoted, so the pass that finishes them is the landing pass, and it must
    find nothing that duplicates the row.
    """
    _kill_at_boundary(tmp_path, repository, boundary)
    driver = tmp_path / "resume_driver.py"
    driver.write_text(RESUME_DRIVER.format(gate_check=repr(GATE_CHECK)))
    log = tmp_path / "resume.log"
    environment = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "TMPDIR": str(tmp_path),
    }
    with log.open("wb") as stream:
        completed = subprocess.run(
            [sys.executable, str(driver), str(repository), RUN_ID, mode],
            stdout=stream,
            stderr=subprocess.STDOUT,
            env=environment,
            cwd=tmp_path,
            timeout=240,
            check=False,
        )
    text = log.read_text(encoding="utf-8", errors="replace")
    assert completed.returncode == 0, (
        f"the {mode} pass after a kill at {boundary} did not complete:\n{text}"
    )
    assert "DRIVER_EXIT=completed" in text
    assert len(_rows(repository)) == 1, (
        "the pass after the kill must leave exactly one ledger row: "
        f"{[row['run_id'] for row in _rows(repository)]}\n{text}"
    )
    assert not _lease(Path(os.environ["RECKON_HOME"])).exists()
    assert _git(repository, "diff", "--cached", "--name-only") == ""
    if boundary == "before-promotion-commit":
        assert not pointer_path(RUN_ID).exists(), (
            "the retry lands the promotion and removes the pointer"
        )
