"""A worker widens its own run's scope to a path no live run claims.

The widening command was coordinator-only: it refused any run whose worker
process was alive, and a worker asking for its own boundary to move had no way
to make it move. These cases show the exception and its limits — the grant is
recorded with its reason and its source, a path a peer holds is still refused,
a worker cannot reach another run, and the review brief counts the self-grant
as inside the scope the run was given.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon.cli import main as cli_main
from reckon.crew import recovery_review_dispatch
from reckon.crew.runs import _write_json, pointer_path

OWN_RUN = "r-20261027T130000000000-self-widen"
PEER_RUN = "r-20261027T130000000000-self-widen-peer"
WAITING_PEER_RUN = "r-20261027T130000000000-self-widen-queued"
OTHER_RUN = "r-20261027T130000000000-self-widen-other"
DECLARED = "reckon/crew/claims.py"
GRANTED = "reckon/crew/dispatch.py"
PEER_HELD = "reckon/crew/queue_admission.py"
REASON = "the repair needs the widen command's own module"


def _pointer(root: Path, run_id: str, *, phase: str, paths: list[str]) -> dict:
    """Write one live pointer and return it as recorded."""
    pointer = {
        "run_id": run_id,
        "project": "sample",
        "repo": str(root / "repo"),
        "worktree": str(root / "repo"),
        "base_sha": "0" * 40,
        "launch": "in-harness",
        "role": "implement",
        "phase": phase,
        "pid": os.getpid(),
        "session_id": "s-self-widen",
        "node": {
            "id": "self-widen",
            "plan": "fixture",
            "section": "s23",
            "write_paths": list(paths),
        },
        "manifest_path": str(root / "manifest.md"),
    }
    _write_json(pointer_path(run_id), pointer)
    return pointer


def _read(run_id: str) -> dict:
    return json.loads(pointer_path(run_id).read_text())


def _widen(*arguments: str, env: dict[str, str] | None = None):
    runner = CliRunner()
    return runner.invoke(cli_main, ["crew", "widen", *arguments], env=env or {})


@pytest.fixture()
def workspace(tmp_path: Path, isolated_reckon_home: Path) -> Path:
    """An isolated crew home and a repository directory beside the pointers."""
    (tmp_path / "repo" / "docs").mkdir(parents=True)
    return tmp_path


def test_a_self_widen_records_the_grant_its_reason_and_its_source(
    workspace: Path,
) -> None:
    """The run's own worker widens a live run, and the pointer says who did it."""
    _pointer(workspace, OWN_RUN, phase="working", paths=[DECLARED])

    result = _widen(
        "--run",
        OWN_RUN,
        "--write-path",
        GRANTED,
        "--reason",
        REASON,
        env={"RECKON_RUN_ID": OWN_RUN},
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["self_widen"] is True
    assert payload["added"] == [GRANTED]
    recorded = _read(OWN_RUN)
    assert recorded["node"]["write_paths"] == [DECLARED, GRANTED]
    grants = {entry["path"]: entry for entry in recorded["node"]["write_grants"]}
    assert grants[GRANTED]["reason"] == REASON
    assert grants[GRANTED]["source"] == "worker"
    assert payload["grants"] == [
        {"path": DECLARED, "source": "dispatch", "reason": ""},
        {"path": GRANTED, "source": "worker", "reason": REASON},
    ]


def test_a_self_widen_needs_a_stated_reason(workspace: Path) -> None:
    """A grant recorded without why would be a boundary moved for no account."""
    _pointer(workspace, OWN_RUN, phase="working", paths=[DECLARED])

    result = _widen(
        "--run", OWN_RUN, "--write-path", GRANTED, env={"RECKON_RUN_ID": OWN_RUN}
    )

    assert result.exit_code != 0
    assert "--reason" in result.output
    assert _read(OWN_RUN)["node"]["write_paths"] == [DECLARED]


@pytest.mark.parametrize(
    ("peer_phase", "peer_run"),
    [("working", PEER_RUN), ("queued", WAITING_PEER_RUN)],
)
def test_a_self_widen_to_a_peer_held_path_is_refused_naming_the_peer(
    workspace: Path, peer_phase: str, peer_run: str
) -> None:
    """A launched or waiting peer's path is refused, and the pointer is untouched."""
    _pointer(workspace, OWN_RUN, phase="working", paths=[DECLARED])
    _pointer(workspace, peer_run, phase=peer_phase, paths=[PEER_HELD])
    before = pointer_path(OWN_RUN).read_bytes()

    result = _widen(
        "--run",
        OWN_RUN,
        "--write-path",
        PEER_HELD,
        "--reason",
        "the repair reads this peer's module",
        env={"RECKON_RUN_ID": OWN_RUN},
    )

    assert result.exit_code != 0, result.output
    assert peer_run in result.output
    assert pointer_path(OWN_RUN).read_bytes() == before


def test_a_worker_cannot_widen_another_run(workspace: Path) -> None:
    """The live-worker exception belongs to the run the worker is, not any run."""
    _pointer(workspace, OTHER_RUN, phase="blocked", paths=[DECLARED])
    before = pointer_path(OTHER_RUN).read_bytes()

    result = _widen(
        "--run",
        OTHER_RUN,
        "--write-path",
        GRANTED,
        "--reason",
        "a grant this worker has no authority to make belongs to another run",
        env={"RECKON_RUN_ID": OWN_RUN},
    )

    assert result.exit_code != 0, result.output
    assert OWN_RUN in result.output
    assert pointer_path(OTHER_RUN).read_bytes() == before


def test_the_review_brief_lists_the_self_grant(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A review reads the grant and its source, so it counts the path in scope."""
    record = {
        "run_id": OWN_RUN,
        "project": "",
        "repo": "",
        "node": {
            "id": "self-widen",
            "write_paths": [DECLARED, GRANTED],
            "write_grants": [
                {
                    "path": GRANTED,
                    "reason": REASON,
                    "source": "worker",
                    "at": "2026-10-27T13:30:00Z",
                }
            ],
        },
        "manifest_path": str(workspace / "manifest.md"),
    }
    brief = workspace / "review-brief.md"
    brief.write_text(
        "Rubric text.\n\nReviewed run: " + OWN_RUN + "\n", encoding="utf-8"
    )
    fields = {
        "project": "",
        "head": "",
        "brief": str(brief),
        "brief_text": brief.read_text(encoding="utf-8"),
    }
    monkeypatch.setattr(
        recovery_review_dispatch, "_record_review_dispatch", lambda *a, **k: None
    )

    report = recovery_review_dispatch._dispatch_composed_review(
        record,
        fields,
        config=None,
        launcher=None,
        allow_unreconciled_runs=True,
        prefer_local=False,
    )

    assert report["dispatched"] is False
    text = brief.read_text(encoding="utf-8")
    assert recovery_review_dispatch.WRITE_GRANTS_HEADING in text
    assert f"{GRANTED} (source: worker)" in text
    assert f"{DECLARED} (source: dispatch)" in text
    assert REASON in text
    assert "self-grant" in text
    assert "inside the run's declared scope" in text
