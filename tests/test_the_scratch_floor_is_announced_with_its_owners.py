"""A refusal at the scratch floor names the reclaimable bytes by owner.

A dispatch, a ``crew resume`` and a review dispatch that meet the scratch floor
must answer with more than the free bytes and the floor: the coordinator who
meets the refusal needs to know whose bytes it could reclaim. The refusal names
the lander's trees with no holder, each run's scratch with its run id and
owning session, and everything else by top-level path.

The scratch root is a temporary directory this test builds, so nothing here
writes to the host's real ``/tmp``. The floor is answered by a fabricated
``statvfs`` that reports less free space than the configured minimum, so the
three refusals are reached without filling a filesystem.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import reckon.crew.dispatch_launch as dispatch_launch_module
from reckon import cli, crew_dispatch_commands, flight
from reckon.crew import runs
from tests import test_resume_and_lane_change_follow_their_own_attempt as lane
from tests.test_resume_and_lane_change_follow_their_own_attempt import (  # noqa: F401
    crew_home,
    operator_home,
    repo,
)

PROJECT = "proj"
PLAN = "plan-a"

RUN_A = "r-20261123T000000000000-owner-a"
RUN_B = "r-20261123T000000000000-owner-b"
SESSION_A = "session-a"
SESSION_B = "session-b"

LANDER_BYTES = 4096

# The response to the fabricated `statvfs`: one gigabyte free against a
# hundred-gigabyte filesystem, so the ten-percent floor is ten gigabytes.
FREE_BYTES = 1024**3
FLOOR_BYTES = 10 * 1024**3


def _fake_statvfs(_path) -> SimpleNamespace:
    return SimpleNamespace(f_bavail=1, f_blocks=100, f_frsize=1024**3)


def _build_scratch_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The section's temporary scratch root.

    An unheld lander tree, two run scratch directories belonging to two
    sessions, and an unrelated directory, each with a distinct byte size so a
    breakdown's figures can be told apart.
    """
    root = tmp_path / "scratch"
    root.mkdir()
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(root))
    lander = root / "landing-base-abc"
    lander.mkdir()
    (lander / "blob").write_bytes(b"x" * LANDER_BYTES)
    for run_id, size in ((RUN_A, 1024), (RUN_B, 2048)):
        directory = root / run_id
        directory.mkdir()
        (directory / "scratch.bin").write_bytes(b"y" * size)
    unrelated = root / "unrelated-dir"
    unrelated.mkdir()
    (unrelated / "f").write_bytes(b"z" * 512)
    monkeypatch.setattr(dispatch_launch_module.os, "statvfs", _fake_statvfs)
    return root


def _write_run_scratch_pointers(crew_home: Path) -> None:  # noqa: F811
    """Two live pointers so each run's scratch names its owning session."""
    assert crew_home.exists()
    for run_id, session in ((RUN_A, SESSION_A), (RUN_B, SESSION_B)):
        path = runs.pointer_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "project": PROJECT,
                    "session": session,
                    "phase": "working",
                    "node": {"id": run_id, "plan": PLAN, "section": "s3"},
                }
            ),
            encoding="utf-8",
        )


def _assert_owner_breakdown(payload: dict) -> None:
    assert payload["error"] == "tmp-headroom-refusal", payload
    assert payload["free_bytes"] == FREE_BYTES, payload
    assert payload["floor_bytes"] == FLOOR_BYTES, payload
    owners = payload["reclaimable_by_owner"]
    by_kind: dict[str, list[dict]] = {}
    for owner in owners:
        by_kind.setdefault(owner["kind"], []).append(owner)
    lander = by_kind["lander-tree"]
    assert [entry["bytes"] for entry in lander] == [LANDER_BYTES], lander
    assert lander[0]["held"] is False, lander
    run_owners = {entry["run_id"]: entry for entry in by_kind["run"]}
    assert set(run_owners) == {RUN_A, RUN_B}, run_owners
    assert run_owners[RUN_A]["session"] == SESSION_A, run_owners[RUN_A]
    assert run_owners[RUN_A]["bytes"] == 1024, run_owners[RUN_A]
    assert run_owners[RUN_B]["session"] == SESSION_B, run_owners[RUN_B]
    assert run_owners[RUN_B]["bytes"] == 2048, run_owners[RUN_B]
    unrelated = [
        entry for entry in by_kind["path"] if entry["path"].endswith("unrelated-dir")
    ]
    assert unrelated and unrelated[0]["bytes"] == 512, by_kind["path"]


def test_a_refused_dispatch_names_every_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crew_home: Path,  # noqa: F811 - imported fixture, requested by name
) -> None:
    _build_scratch_root(tmp_path, monkeypatch)
    _write_run_scratch_pointers(crew_home)
    config = flight.resolve(host_path=tmp_path / "absent.yaml").config
    monkeypatch.setattr(
        crew_dispatch_commands, "_dispatch_resolved_flight", lambda *a, **k: config
    )
    monkeypatch.setattr(
        crew_dispatch_commands, "_model_availability_refusal", lambda *a, **k: None
    )
    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "dispatch",
            "--project",
            PROJECT,
            "--plan",
            PLAN,
            "--section",
            "s3",
            "--node",
            "work",
            "--session",
            "test",
            "--route",
            "deterministic",
            "--dry-run",
            "--repo",
            str(tmp_path / "repo"),
        ],
    )
    assert result.exit_code == 75, result.output
    _assert_owner_breakdown(json.loads(result.output))


def test_a_refused_resume_names_every_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,  # noqa: F811 - imported fixture, requested by name
    crew_home: Path,  # noqa: F811 - imported fixture, requested by name
) -> None:
    _build_scratch_root(tmp_path, monkeypatch)
    _write_run_scratch_pointers(crew_home)
    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *a, **k: lane.CONFIG
    )
    run_id = f"r-resume-{SESSION_A}"
    record = lane._stopped_pointer(tmp_path, repo, run_id, backend="alpha")
    record["session"] = SESSION_A
    path = runs.pointer_path(run_id)
    path.write_text(json.dumps(record), encoding="utf-8")

    result = CliRunner().invoke(
        cli.main,
        ["crew", "resume", "--run", run_id, "--advice", "continue"],
    )
    assert result.exit_code == 75, result.output
    payload = json.loads(result.output)
    _assert_owner_breakdown(payload)
    assert payload["run_id"] == run_id, payload


def test_a_refused_review_dispatch_names_every_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,  # noqa: F811 - imported fixture, requested by name
    crew_home: Path,  # noqa: F811 - imported fixture, requested by name
) -> None:
    _build_scratch_root(tmp_path, monkeypatch)
    _write_run_scratch_pointers(crew_home)
    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *a, **k: lane.CONFIG
    )
    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "review",
            "--project",
            PROJECT,
            "--plan",
            PLAN,
            "--session",
            "reviewer",
            "--dry-run",
        ],
    )
    assert result.exit_code == 75, result.output
    _assert_owner_breakdown(json.loads(result.output))
