"""A promotion copies the gate log it cites where the row can still find it.

``crew complete --gate-log-path`` records where a log lies rather than copying
it. Workers write gate logs to ``/tmp`` or to a worktree's gitignored output
directory, and both vanish — ``/tmp`` to reaping, the worktree to the promotion
that releases it. The ledger row then cites a path that resolves to nothing:
every check a reader runs on the row passes, and the evidence is gone, which is
the same defect as a fabricated identifier one layer along.

The remedy is that the cited log is copied into the run directory, which
outlives the worktree and is pruned only by ``crew gc`` on a retention window,
and the row records the copy's path. A log already inside the run directory is
left alone, and a passing gate whose cited log cannot be found is refused
rather than recorded as a path pointing at nothing.

Every fixture below synthesises a temporary repository and a temporary crew
home, and the real plan and crew directories are asserted absent before and
after each test, because an isolated read does not prove an isolated write.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from reckon import _plan_html, crew, ledger
from reckon.crew.node import CrewError
from reckon.crew.runs import _write_json, pointer_path, run_dir

PROJECT = "gate-log-preserved-fixture"
PLAN = "preservation-target"
RUN_IDS = (
    "r-20260922T120000000001-cited-log-outside",
    "r-20260922T120000000002-copy-outlives-original",
    "r-20260922T120000000003-log-already-inside",
    "r-20260922T120000000004-cited-log-absent",
)


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write_plan(root: Path) -> Path:
    path = root / "docs" / "plans" / f"{PLAN}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    state = {
        "type": "plan",
        "slug": PLAN,
        "title": "Preservation target",
        "status": "active",
        "version": 0,
        "comments": {},
    }
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def real_stores_are_not_fixture_targets() -> None:
    """No fixture may reach this checkout's own plan or the real crew home."""
    checkout = Path(__file__).resolve().parents[1]
    real_plan = checkout / "docs" / "plans" / f"{PLAN}.html"
    real_state = checkout / "docs" / "state" / PROJECT
    crew_home = Path.home() / ".config" / "reckon" / "crew"
    real_pointers = [crew_home / "live" / f"{run_id}.json" for run_id in RUN_IDS]
    real_dirs = [crew_home / "runs" / run_id for run_id in RUN_IDS]

    def touched() -> bool:
        return (
            real_plan.exists()
            or real_state.exists()
            or any(path.exists() for path in real_pointers)
            or any(path.exists() for path in real_dirs)
        )

    assert not touched()
    yield
    assert not touched()


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_hook = tmp_path / "config"
    config_hook.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_hook))
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
    (config_hook / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


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
            "created_at": "2026-09-22T11:00:00Z",
            "manifest_path": "/durable/manifest.md",
            "node": {
                "id": "preservation-target",
                "plan": PLAN,
                "section": "§3",
                "time_budget": "25m",
                "write_paths": [],
            },
        },
    )


def _promote(
    repository: Path, run_id: str, outcome: str, **extra: Any
) -> dict[str, Any]:
    return crew.complete(
        run_id,
        gate=extra.pop("gate", "passed"),
        outcome=outcome,
        root=repository,
        **extra,
    )


def _row(repository: Path, run_id: str) -> dict[str, Any]:
    rows = ledger.runs(PROJECT, root=repository)
    matched = [row for row in rows if row["run_id"] == run_id]
    assert len(matched) == 1
    return matched[0]


def _gate_check(log_path: Path | str, **extra: Any) -> dict[str, Any]:
    return {
        "command": "pytest -q tests/test_gate_log_is_preserved.py",
        "exit_status": 0,
        "log_path": str(log_path),
        "log_digest": extra.pop("log_digest", ""),
        **extra,
    }


# ── The copy: an outside log is recorded where the row can find it ──────────


def test_a_cited_log_outside_the_run_directory_is_copied_and_recorded(
    repository: Path, tmp_path: Path
) -> None:
    """The named case: the row cites a path inside its own run directory.

    The cited log sits outside both the run directory and the repository, the
    shape a worker produces with ``--gate-log-path /tmp/gate.log``. Promotion
    must record the copy's path, not the transient original's.
    """
    run_id = RUN_IDS[0]
    cited = tmp_path / "worker-scratch" / "gate.log"
    cited.parent.mkdir(parents=True, exist_ok=True)
    cited.write_text(
        "pytest -q tests/test_gate_log_is_preserved.py\n1 passed\nEXIT=0\n",
        encoding="utf-8",
    )
    _pointer(repository, run_id)

    _promote(
        repository, run_id, "the cited log is preserved", gate_check=_gate_check(cited)
    )

    recorded = _row(repository, run_id)["gate_check"]["log_path"]
    assert Path(recorded).is_relative_to(run_dir(run_id))
    assert Path(recorded) != cited


def test_the_recorded_copy_outlives_the_cited_original(
    repository: Path, tmp_path: Path
) -> None:
    """The point of the copy: it resolves after the original is gone.

    The original is deleted after the promotion, which is what reaping /tmp or
    releasing the worktree does to it. The recorded path must still resolve and
    its bytes must be the original's, or the row cites a path that is as dead
    as the one it replaced.
    """
    run_id = RUN_IDS[1]
    cited = tmp_path / "worker-scratch" / "gate.log"
    cited.parent.mkdir(parents=True, exist_ok=True)
    payload = "pytest -q tests/test_gate_log_is_preserved.py\n4 passed\nEXIT=0\n"
    cited.write_text(payload, encoding="utf-8")
    _pointer(repository, run_id)

    _promote(
        repository,
        run_id,
        "the copy outlives the original",
        gate_check=_gate_check(cited),
    )

    recorded = Path(_row(repository, run_id)["gate_check"]["log_path"])
    cited.unlink()
    assert recorded.is_file()
    assert recorded.read_bytes() == payload.encode()


# ── The non-copy: a log already inside the run directory is left alone ──────


def test_a_log_already_in_the_run_directory_is_recorded_unchanged(
    repository: Path,
) -> None:
    """A durable citation is not re-copied, so it is neither moved nor doubled.

    A worker that already wrote its log into the run directory has recorded the
    durable path itself. Promotion must carry that path through unchanged and
    must not leave a second copy beside it.
    """
    run_id = RUN_IDS[2]
    directory = run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    cited = directory / "gate.log"
    cited.write_text("3 passed\nEXIT=0\n", encoding="utf-8")
    _pointer(repository, run_id)

    _promote(
        repository, run_id, "an inside log is left alone", gate_check=_gate_check(cited)
    )

    recorded = _row(repository, run_id)["gate_check"]["log_path"]
    assert Path(recorded) == cited
    assert sorted(path.name for path in directory.glob("*.log")) == ["gate.log"]


# ── The refusal: nothing to copy must not become a copy of nothing ──────────


def test_a_passing_gate_with_a_missing_log_and_no_digest_is_refused(
    repository: Path, tmp_path: Path
) -> None:
    """An absent cited log is refused rather than recorded as a dead path.

    With no digest recorded in its place, a cited log that cannot be read leaves
    the row citing a path that resolves to nothing — the exact defect the copy
    exists to close. Promotion refuses, writes no ledger row, and creates no
    placeholder in the run directory.
    """
    run_id = RUN_IDS[3]
    absent = tmp_path / "worker-scratch" / "never-written.log"
    _pointer(repository, run_id)

    with pytest.raises(CrewError) as error:
        _promote(
            repository, run_id, "a citation of nothing", gate_check=_gate_check(absent)
        )

    assert "does not exist" in str(error.value)
    assert ledger.runs(PROJECT, root=repository) == []
    assert not (run_dir(run_id) / "gate.log").exists()
