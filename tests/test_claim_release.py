"""A claimed path can be released so a later dispatch may declare it.

The refusal side is tested as hard as the success path. Releasing a live
worker's claim is how two workers end up writing one file, so a live process, a
foreign launching host, and a pointer written before its worker was spawned
must each refuse rather than succeed.

The release is asserted through the dispatch-equivalent claim check
(``plan_scope_lanes``), which derives what a run holds from the live pointer
exactly as admission does — not by reading the pointer file back.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from reckon import crew
from reckon.crew import claims
from reckon.crew.node import CrewError
from reckon.crew.runs import plan_scope_lanes

PROJECT = "demo"
CLAIMED = "package/one.py"
OTHER = "package/two.py"


def _snapshot(path: Path) -> tuple[str, ...]:
    if not path.is_dir():
        return ()
    return tuple(sorted(item.name for item in path.iterdir()))


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the crew config home at a temp dir and prove the real one is idle."""
    real_live = crew.live_dir()
    before = _snapshot(real_live)
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    yield config_home
    # Read the captured path, not a fresh ``live_dir()``: RECKON_HOME is still
    # patched at teardown, so a second resolve would inspect the temp home and
    # assert nothing about the real one.
    assert _snapshot(real_live) == before


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    (root / "package").mkdir(parents=True)
    return root


def _stopped_pid() -> int:
    """Return a pid whose process has exited and been reaped."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def _write_pointer(
    run_id: str,
    *,
    repo: Path,
    paths: tuple[str, ...],
    pid: int | None = None,
    host: str | None = None,
) -> None:
    directory = crew.live_dir()
    directory.mkdir(parents=True, exist_ok=True)
    pointer: dict[str, Any] = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repo),
        "phase": "working",
        "node": {"id": f"node-{run_id}", "write_paths": list(paths)},
    }
    if pid is not None:
        pointer["pid"] = pid
    if host is not None:
        pointer["launcher_host"] = host
    (directory / f"{run_id}.json").write_text(json.dumps(pointer), encoding="utf-8")


def _live_conflicts(repo: Path, path: str) -> list[dict[str, Any]]:
    """Return the live conflicts a candidate declaring ``path`` would raise."""
    report = plan_scope_lanes(
        [{"id": "candidate", "write_paths": [path]}],
        project=PROJECT,
        repo=repo,
        derivations={},
    )
    return list(report["live_conflicts"])


def test_an_untouched_claim_still_refuses_a_second_writer(home, repo: Path) -> None:
    _write_pointer("run-held", repo=repo, paths=(CLAIMED,), host=socket.gethostname())
    conflicts = _live_conflicts(repo, CLAIMED)
    assert [row["run_id"] for row in conflicts] == ["run-held"]
    assert conflicts[0]["claimed_path"] == CLAIMED


def test_a_released_claim_lets_a_second_writer_declare_the_path(
    home, repo: Path
) -> None:
    _write_pointer(
        "run-held",
        repo=repo,
        paths=(CLAIMED,),
        pid=_stopped_pid(),
        host=socket.gethostname(),
    )
    assert _live_conflicts(repo, CLAIMED)
    report = claims.release_claim("run-held", reason="worker blocked at its fence")
    assert report.removed == (CLAIMED,)
    assert report.current == ()
    assert _live_conflicts(repo, CLAIMED) == []


def test_narrowing_releases_only_the_dropped_path(home, repo: Path) -> None:
    _write_pointer(
        "run-held",
        repo=repo,
        paths=(CLAIMED, OTHER),
        pid=_stopped_pid(),
        host=socket.gethostname(),
    )
    claims.narrow_claim("run-held", keep=(OTHER,), reason="filling a fence gap")
    assert claims.declared_claim_paths("run-held") == (OTHER,)
    assert _live_conflicts(repo, OTHER)
    assert _live_conflicts(repo, CLAIMED) == []


def test_release_refuses_a_worker_that_is_still_running(home, repo: Path) -> None:
    _write_pointer(
        "run-live",
        repo=repo,
        paths=(CLAIMED,),
        pid=os.getpid(),
        host=socket.gethostname(),
    )
    with pytest.raises(claims.ClaimAmendmentRefusedError):
        claims.release_claim(
            "run-live", reason="moving a claim held by a running worker"
        )
    assert claims.declared_claim_paths("run-live") == (CLAIMED,)
    assert _live_conflicts(repo, CLAIMED)


def test_release_refuses_a_claim_launched_on_another_host(home, repo: Path) -> None:
    _write_pointer(
        "run-foreign",
        repo=repo,
        paths=(CLAIMED,),
        pid=os.getpid(),
        host="a-different-login-node",
    )
    with pytest.raises(claims.ClaimAmendmentRefusedError):
        claims.release_claim("run-foreign", reason="a foreign pid is not evidence")


def test_release_refuses_a_pointer_that_records_no_process(home, repo: Path) -> None:
    _write_pointer("run-unborn", repo=repo, paths=(CLAIMED,), host=socket.gethostname())
    with pytest.raises(claims.ClaimAmendmentRefusedError):
        claims.release_claim("run-unborn", reason="the worker may spawn any instant")
    assert claims.declared_claim_paths("run-unborn") == (CLAIMED,)


def test_releasing_one_path_keeps_the_other_declared(home, repo: Path) -> None:
    _write_pointer(
        "run-held",
        repo=repo,
        paths=(CLAIMED, OTHER),
        pid=_stopped_pid(),
        host=socket.gethostname(),
    )
    report = claims.release_claim(
        "run-held", paths=(CLAIMED,), reason="one path was never written"
    )
    assert report.removed == (CLAIMED,)
    assert claims.declared_claim_paths("run-held") == (OTHER,)
    assert _live_conflicts(repo, CLAIMED) == []
    assert _live_conflicts(repo, OTHER)


def test_release_refuses_a_path_the_run_never_declared(home, repo: Path) -> None:
    _write_pointer(
        "run-held",
        repo=repo,
        paths=(CLAIMED,),
        pid=_stopped_pid(),
        host=socket.gethostname(),
    )
    with pytest.raises(CrewError):
        claims.release_claim("run-held", paths=("package/missing.py",), reason="typo")
    assert claims.declared_claim_paths("run-held") == (CLAIMED,)


def test_narrowing_that_drops_nothing_is_refused(home, repo: Path) -> None:
    _write_pointer(
        "run-held",
        repo=repo,
        paths=(CLAIMED, OTHER),
        pid=_stopped_pid(),
        host=socket.gethostname(),
    )
    with pytest.raises(CrewError):
        claims.narrow_claim("run-held", keep=(CLAIMED, OTHER), reason="no change")


def test_removing_the_pointer_releases_the_claim_as_promotion_does(
    home, repo: Path
) -> None:
    _write_pointer(
        "run-done",
        repo=repo,
        paths=(CLAIMED,),
        pid=_stopped_pid(),
        host=socket.gethostname(),
    )
    assert _live_conflicts(repo, CLAIMED)
    (crew.live_dir() / "run-done.json").unlink()
    assert _live_conflicts(repo, CLAIMED) == []
