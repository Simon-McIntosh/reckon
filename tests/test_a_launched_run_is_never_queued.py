"""A launched run is never read as queued.

Dispatch stores a request with ``phase: queued`` and the admission pass
advances that phase when it launches the request, recording ``launched_at`` on
the pointer in a separate write. A pointer can therefore carry both for as long
as the phase write has not followed, and the same is true after the run has
finished: the run ``r-20261112T060747023508`` read as ``run-queued``, "position
1 of the queued requests", while its pointer held ``launched_at`` and the run
had already completed. The queued phase was the stale field. A coordinator
obeying such a row waits on a queue place that no longer exists instead of
promoting the finished run.

So the queued-run duty and the placements read from
``queue_admission.queued_placements`` count only a pointer still waiting to be
launched: a pointer carrying ``launched_at`` is never queued, whatever its
phase says, and its duty is read from the run's own manifest instead.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import queue_admission, runs

obligations_module = __import__("reckon.crew.obligations", fromlist=["obligations"])

PROJECT = "launched-run-fixture"
SESSION = "coordinator-fixture"
LAUNCHED_RUN = "r-launched-request"
STILL_QUEUED_RUN = "r-still-queued-request"
OBSERVED_AT = datetime(2026, 11, 12, 7, 30, tzinfo=UTC)


@pytest.fixture()
def mounted_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary crew home and one mounted project, so nothing real is read."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(home / "mounts.json"))
    repo = tmp_path / "repo"
    docs = repo / "docs"
    (docs / "state" / PROJECT).mkdir(parents=True)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed launched-run fixture"),
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(docs)}), encoding="utf-8"
    )
    monkeypatch.setattr(obligations_module, "_utc_now", lambda: OBSERVED_AT)
    return repo


def _write_pointer(
    run_id: str,
    *,
    phase: str,
    queued_at: str,
    launched_at: str = "",
    manifest: str = "",
) -> dict[str, object]:
    record: dict[str, object] = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "phase": phase,
        "queued_at": queued_at,
        "created_at": queued_at,
        "process_alive": False,
        "launcher_host": "some-host.invalid",
    }
    if launched_at:
        record["launched_at"] = launched_at
    if manifest:
        record["manifest_path"] = manifest
    runs._write_json(runs.pointer_path(run_id), record)
    return record


def _pointer(run_id: str) -> dict[str, object]:
    return json.loads(runs.pointer_path(run_id).read_text(encoding="utf-8"))


def _launched_pointer(tmp_path: Path) -> dict[str, object]:
    """A launched, finished run whose phase write never followed its launch."""
    manifest = tmp_path / "launched-manifest.md"
    manifest.write_text(
        "status: complete\n\n"
        "goal: the batch entry's context fit is judged at queue time\n",
        encoding="utf-8",
    )
    return _write_pointer(
        LAUNCHED_RUN,
        phase="queued",
        queued_at="2026-11-12T06:52:00Z",
        launched_at="2026-11-12T06:55:24Z",
        manifest=str(manifest),
    )


def test_a_launched_pointer_holds_no_queue_place(
    mounted_home: Path, tmp_path: Path
) -> None:
    launched = _launched_pointer(tmp_path)
    still_queued = _write_pointer(
        STILL_QUEUED_RUN, phase="queued", queued_at="2026-11-12T07:01:00Z"
    )

    placements = queue_admission.queued_placements(
        [launched, still_queued], now=OBSERVED_AT
    )

    assert LAUNCHED_RUN not in placements
    assert placements[STILL_QUEUED_RUN]["position"] == 1


def test_a_launched_pointer_owes_no_queued_duty(
    mounted_home: Path, tmp_path: Path
) -> None:
    _launched_pointer(tmp_path)
    _write_pointer(STILL_QUEUED_RUN, phase="queued", queued_at="2026-11-12T07:01:00Z")

    duties = obligations_module.obligations(PROJECT, SESSION)["obligations"]

    by_run = {str(item.get("run_id") or ""): item for item in duties}
    assert LAUNCHED_RUN in by_run, duties
    assert by_run[LAUNCHED_RUN]["kind"] != "run-queued", by_run[LAUNCHED_RUN]
    assert not by_run[LAUNCHED_RUN].get("position")
    assert by_run[STILL_QUEUED_RUN]["kind"] == "run-queued"
    assert by_run[STILL_QUEUED_RUN]["position"] == 1


def test_a_launched_pointer_is_not_read_as_queued_by_the_classifier(
    mounted_home: Path, tmp_path: Path
) -> None:
    """The row's own classification is the queued one, not only its duty."""
    launched = _launched_pointer(tmp_path)

    assert queue_admission.awaits_a_lane_slot(launched) is False
    assert queue_admission.awaits_a_lane_slot(
        _write_pointer(
            STILL_QUEUED_RUN, phase="queued", queued_at="2026-11-12T07:01:00Z"
        )
    )
    assert _pointer(LAUNCHED_RUN).get("phase") == "queued"
