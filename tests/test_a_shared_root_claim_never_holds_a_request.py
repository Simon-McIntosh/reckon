"""A claim under a shared root never holds a queued request.

The pair reproduced here is the one the live admit pass held from 13:54 UTC on
11 November: the holder declared the picker package directory beside two files
inside it, and the request declared one file in that same package. The overlap
is therefore the holder's containing directory claim against a file under a
declared shared root, not a file claim against the same file, which is the shape
the shared-path comparison is written for.

Three facts are asserted: the pair is admitted, the same pair on an exclusive
pattern is still held naming that pattern, and a request that claims a directory
is still held.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import queue_admission, runs

PROJECT = "proj"
EXCLUSIVE = "docs/plans/x.html"

#: The holder's declared scope, as recorded for the run the live pass held.
HOLDER_PATHS = [
    "reckon/crew/picker/snapshot.py",
    "reckon/crew/picker/__init__.py",
    "reckon/crew/picker",
]
#: The request's declared scope, as recorded for the held request.
REQUEST_PATHS = ["reckon/crew/picker/lane_context.py"]


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    repo = tmp_path / "repo"
    repo.mkdir()
    for path in [*HOLDER_PATHS[:2], *REQUEST_PATHS, EXCLUSIVE]:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("VALUE = 1\n")
    state = repo / "docs" / "state" / PROJECT
    state.mkdir(parents=True, exist_ok=True)
    (state / "shared-write-paths.json").write_text(
        json.dumps(
            {
                "shared_roots": ["reckon/", "tests/"],
                "exclusive_patterns": ["docs/plans/*.html"],
            }
        )
    )
    return repo


def _pointer(
    run_id: str, phase: str, paths: list[str], repo: Path
) -> dict[str, Any]:
    """One live pointer, written where the claim refresh reads it back."""
    pointer = {
        "run_id": run_id,
        "phase": phase,
        "project": PROJECT,
        "repo": str(repo),
        "session": "coordinator-a",  # noqa: S105 - a session name, not a secret
        "node": {"id": run_id, "write_paths": list(paths)},
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _hold(repo: Path, holder_paths: list[str], request_paths: list[str]) -> str | None:
    holder = _pointer("r-holder", "working", holder_paths, repo)
    request = _pointer("r-request", "queued", request_paths, repo)
    return queue_admission._live_claim_hold(
        request,
        [holder],
        dispositions={},
        shared_files=runs._shared_write_paths(PROJECT, repo),
    )


def test_the_reproduced_pair_is_admitted(repository: Path) -> None:
    hold = _hold(repository, HOLDER_PATHS, REQUEST_PATHS)

    assert hold is None, hold


def test_the_same_pair_is_shared_by_an_exclusive_pattern(repository: Path) -> None:
    hold = _hold(repository, [EXCLUSIVE], [EXCLUSIVE])

    assert hold is not None
    assert "docs/plans/*.html" in hold, hold


def test_a_directory_claim_is_still_held(repository: Path) -> None:
    hold = _hold(repository, ["reckon/crew/picker/snapshot.py"], ["reckon/crew/picker"])

    assert hold is not None
    assert "holds 'reckon/crew/picker'" in hold, hold