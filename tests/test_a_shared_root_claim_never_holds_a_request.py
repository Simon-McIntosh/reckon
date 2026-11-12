"""A claim under a shared root never holds a queued request.

The pair reproduced here is the one the live admit pass held from 13:54 UTC on
11 November: the holder declared the picker package directory beside two files
inside it, and the request declared one file in that same package. The overlap
is therefore the holder's containing directory claim against a file under a
declared shared root, not a file claim against the same file, which is the shape
the shared-path comparison was written for.

A directory claim under a shared root shares as the files beneath it do, so that
pair is admitted by the queue and passes the launch-time check. An exclusive
pattern still fences, and so does a directory claim that is or contains one.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from reckon import crew
from reckon.crew import dispatch_claims, queue_admission, queue_edit, runs
from reckon.crew.dispatch_claims import _RepositoryScopeClaim

PROJECT = "proj"
EXCLUSIVE = "docs/plans/x.html"
FIGURE_DIR = "docs/figures/topic"

#: The holder's declared scope, as recorded for the run the live pass held.
HOLDER_PATHS = [
    "reckon/crew/picker/snapshot.py",
    "reckon/crew/picker/__init__.py",
    "reckon/crew/picker",
]
#: The request's declared scope, as recorded for the held request.
REQUEST_PATHS = ["reckon/crew/picker/lane_context.py"]
#: The holder's package directory, claimed on its own by two peers.
DIRECTORY = "reckon/crew/picker"


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    repo = tmp_path / "repo"
    repo.mkdir()
    for path in [*HOLDER_PATHS[:2], *REQUEST_PATHS, EXCLUSIVE, f"{FIGURE_DIR}/a.svg"]:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("VALUE = 1\n")
    state = repo / "docs" / "state" / PROJECT
    state.mkdir(parents=True, exist_ok=True)
    (state / "shared-write-paths.json").write_text(
        json.dumps(
            {
                "shared_roots": ["reckon/", "tests/", "docs/evidence/"],
                "exclusive_patterns": [
                    "docs/plans/*.html",
                    "docs/state/",
                    "docs/figures/",
                ],
            }
        )
    )
    # The launch check compares claim repositories by the identity it derives
    # from git, so the temporary tree must be a repository of its own; without
    # one, the identity walks up to an enclosing checkout and no claim matches.
    for arguments in (
        ["init", "-q"],
        ["add", "--", "docs"],
        [
            "-c",
            "user.email=worker@local",
            "-c",
            "user.name=worker",
            "commit",
            "-q",
            "-m",
            "seed",
        ],
    ):
        subprocess.run(["git", "-C", str(repo), *arguments], check=True)
    return repo


def _pointer(run_id: str, phase: str, paths: list[str], repo: Path) -> dict[str, Any]:
    """One live pointer, written where the claim refresh reads it back."""
    pointer = {
        "run_id": run_id,
        "phase": phase,
        "project": PROJECT,
        "repo": str(repo),
        "session": "coordinator-a",
        "node": {"id": run_id, "write_paths": list(paths)},
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _hold(repo: Path, holder_paths: list[str], request_paths: list[str]) -> str | None:
    """The queue's verdict on the pair, through the live claim comparison."""
    holder = _pointer("r-holder", "working", holder_paths, repo)
    request = _pointer("r-request", "queued", request_paths, repo)
    return queue_admission._live_claim_hold(
        request,
        [holder],
        dispositions={},
        shared_files=runs._shared_write_paths(PROJECT, repo),
    )


def _launch_refusal(
    repo: Path, holder_paths: list[str], request_paths: list[str]
) -> str:
    """The launch-time claim check's answer for the same pair, as a string."""
    node = _request_node(request_paths)
    resolved = repo.resolve()
    claims = [
        _RepositoryScopeClaim(
            project=PROJECT,
            repository=resolved,
            run_id="r-holder",
            node_id="r-holder",
            path=path,
            absolute_path=(repo / path).resolve(),
            declared_path=path,
            registered_at="2026-11-11T08:02:03Z",
            launched=True,
            binding=True,
        )
        for path in holder_paths
    ]
    try:
        dispatch_claims._raise_repository_scope_conflict(
            node,
            project=PROJECT,
            repo=resolved,
            authority={"repositories": [resolved]},
            claims=claims,
        )
    except crew.ScopeConflict as refused:
        return str(refused)
    return ""


def _request_node(paths: list[str]) -> crew.TaskNode:
    """The held request's node, as every claim comparison reads it."""
    return crew.TaskNode(
        id="r-request",
        plan="plan-a",
        section="dispatch",
        goal="edit one declared file",
        done_when="the claim comparison admits the pair",
        write_paths=list(paths),
        spec_level="exact",
        time_budget="20m",
    )


def _claims(repo: Path, paths: list[str]) -> list[_RepositoryScopeClaim]:
    """The holder's claims, as the launch check and the conflict report read them."""
    return [
        _RepositoryScopeClaim(
            project=PROJECT,
            repository=repo.resolve(),
            run_id="r-holder",
            node_id="r-holder",
            path=path,
            absolute_path=(repo / path).resolve(),
            declared_path=path,
            registered_at="2026-11-11T08:02:03Z",
            launched=True,
            binding=True,
        )
        for path in paths
    ]


def _conflict_rows(
    repo: Path, holder_paths: list[str], request_paths: list[str]
) -> list[dict[str, Any]]:
    """The dispatch conflict report's answer for the same pair."""
    node = _request_node(request_paths)
    return dispatch_claims._live_conflict_rows(
        node,
        project=PROJECT,
        repo=repo.resolve(),
        authority={"repositories": [repo.resolve()]},
        claims=_claims(repo, holder_paths),
    )


def test_two_claims_on_one_shared_directory_share(repository: Path) -> None:
    shared = runs._shared_write_paths(PROJECT, repository)

    assert DIRECTORY in shared
    assert runs._claim_paths_share(shared, DIRECTORY, DIRECTORY)


def test_the_reproduced_pair_is_admitted(repository: Path) -> None:
    hold = _hold(repository, HOLDER_PATHS, REQUEST_PATHS)

    assert hold is None, hold


def test_the_reproduced_pair_passes_the_launch_check(repository: Path) -> None:
    refusal = _launch_refusal(repository, HOLDER_PATHS, REQUEST_PATHS)

    assert refusal == "", refusal


def test_the_queue_edit_admits_the_reproduced_pair(repository: Path) -> None:
    _pointer("r-holder", "working", HOLDER_PATHS, repository)
    record = {
        "run_id": "r-request",
        "project": PROJECT,
        "repo": str(repository),
        "node": {"id": "r-request", "write_paths": list(REQUEST_PATHS)},
    }

    assert queue_edit._live_claim_conflict(_request_node(REQUEST_PATHS), record) is None

    _pointer("r-holder", "working", [EXCLUSIVE], repository)
    exclusive = {
        **record,
        "node": {"id": "r-request", "write_paths": [EXCLUSIVE]},
    }
    refusal = queue_edit._live_claim_conflict(_request_node([EXCLUSIVE]), exclusive)

    assert refusal is not None and "docs/plans/*.html" in str(refusal)


def test_the_conflict_report_agrees_with_admission(repository: Path) -> None:
    assert _conflict_rows(repository, HOLDER_PATHS, REQUEST_PATHS) == []

    reported = _conflict_rows(repository, [EXCLUSIVE], [EXCLUSIVE])

    assert [row["claimed_path"] for row in reported] == [EXCLUSIVE], reported


def test_the_same_pair_is_held_by_an_exclusive_pattern(repository: Path) -> None:
    hold = _hold(repository, [EXCLUSIVE], [EXCLUSIVE])

    assert hold is not None
    assert "docs/plans/*.html" in hold, hold
    assert _launch_refusal(repository, [EXCLUSIVE], [EXCLUSIVE])


def test_a_claim_on_a_figure_directory_holds_a_file_inside_it(
    repository: Path,
) -> None:
    hold = _hold(repository, [FIGURE_DIR], [f"{FIGURE_DIR}/a.svg"])

    assert hold is not None
    assert "docs/figures/" in hold, hold
    assert _launch_refusal(repository, [FIGURE_DIR], [f"{FIGURE_DIR}/a.svg"])
