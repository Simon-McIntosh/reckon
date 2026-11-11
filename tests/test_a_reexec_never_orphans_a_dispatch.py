"""A node claim belongs to one execution of its holder process."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from reckon.crew import dispatch_claims as claims
from reckon.crew.node import CrewError


@pytest.fixture()
def crew_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home / "crew" / "claims"


def _take_claim(run_id: str) -> claims._NodeDispatchClaim:
    return claims._claim_node_dispatch(
        project="identity-fixture",
        worktree_identity="same-worktree",
        node_id="same-node",
        run_id=run_id,
        session="same-session",
    )


def _record(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_changed_exec_identity_reclaims_claim_and_allows_new_dispatch(
    crew_store: Path,
) -> None:
    # An import alone publishes nothing; the identity a claim publishes is the
    # one a later dispatch judges this holder by.
    imported = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os; import reckon.crew.dispatch_claims; print(os.getpid())",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert not (crew_store / f"exec-{int(imported.stdout)}.identity").exists()

    first = _take_claim("first-run")
    recorded = _record(first.path)
    identity_file = crew_store / f"exec-{os.getpid()}.identity"
    assert recorded["process_start_time"] == claims._process_start_time(os.getpid())
    assert recorded["exec_identity"] == identity_file.read_text(encoding="utf-8")

    identity_file.write_text("identity-from-next-exec", encoding="utf-8")
    second = _take_claim("second-run")

    assert second.reclaimed is not None
    assert second.reclaimed["run_id"] == "first-run"
    assert Path(second.reclaimed["moved_to"]).exists()
    assert _record(second.path)["run_id"] == "second-run"
    second.release()


def test_unchanged_exec_identity_keeps_claim_held(crew_store: Path) -> None:
    first = _take_claim("first-run")
    recorded = _record(first.path)
    identity_file = crew_store / f"exec-{os.getpid()}.identity"
    assert recorded["exec_identity"] == identity_file.read_text(encoding="utf-8")

    with pytest.raises(CrewError, match="first-run"):
        _take_claim("second-run")

    assert _record(first.path)["run_id"] == "first-run"
    first.release()


def test_legacy_claim_with_no_exec_identity_stays_held(crew_store: Path) -> None:
    first = _take_claim("first-run")
    recorded = _record(first.path)
    recorded.pop("exec_identity")
    first.path.write_text(json.dumps(recorded), encoding="utf-8")
    (crew_store / f"exec-{os.getpid()}.identity").unlink(missing_ok=True)

    with pytest.raises(CrewError, match="first-run"):
        _take_claim("second-run")

    assert _record(first.path)["run_id"] == "first-run"
    first.release()
