"""A review promotes without ``--outcome`` when its record shortens its own id.

A review run's stored record names the run that produced it. One record field,
``review_run_id``, is the id the store indexes by; the record reported on
13 November carried that name with its trailing hash suffix dropped, while the
``reviewed_run_id`` it reviewed was exact. The record held the whole review ---
every dimension scored, no finding --- and the run's own record check passed,
yet ``crew complete`` demanded ``--outcome`` because the name it read was not
character-for-character the run's exact id.

The name is a run id, so it is resolved to a run rather than compared as text.
A name that reaches exactly one run promotes on the review the record already
holds. A name that reaches no run, or more than one, is still refused, and the
refusal names the runs it reached.

Negative control: restoring the exact-id comparison leaves the test's first
case refused, which is what that case asserts against.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import _plan_html, crew
from reckon.crew import review as review_module
from reckon.crew.node import CrewError
from reckon.crew.promotion_checks import _resolve_promotion_outcome
from reckon.crew.runs import _write_json, pointer_path, run_dir
from tests.helpers.git import git as _git

PROJECT = "review-node-name-fixture"
PLAN = "review-node-name-target"
REVIEWED_RUN_ID = "r-20261019T214105811291-reviewed-run"
REVIEW_RUN_SUFFIX = "3d58ba4688d8"
REVIEW_RUN_ID = f"r-20261019T230558980003-review-of-reviewed-run-{REVIEW_RUN_SUFFIX}"
REVIEW_RECORD_NAME = "r-20261019T230558980003-review-of-reviewed-run"
SIBLING_RUN_ID = "r-20261019T230558980003-review-of-reviewed-run-9f9f9f9f9f9f"
UNRELATED_RUN_ID = "r-20261019T230559000000-review-of-another-run-aaaaaaaaaaaa"
REVIEW_NODE_ID = f"review-of-{REVIEWED_RUN_ID}"
DISPATCHED_HEAD = "a" * 40
REVIEWED_BASE = "c" * 40
TOTAL = 20 * len(review_module.REVIEW_DIMENSIONS)
DISPATCH_TIME = "2026-10-19T23:05:58Z"
DISPATCH_INSTANT = datetime(2026, 10, 19, 23, 5, 58, tzinfo=UTC).timestamp()


def _write_resource(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{state['slug']}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


@pytest.fixture()
def repository(isolated_reckon_home: Path, tmp_path: Path) -> Path:
    """A committed repository whose docs directory is the project's mount."""
    root = tmp_path / "repo"
    _write_resource(
        root / "docs" / "plans" / f"{PLAN}.html",
        {
            "type": "plan",
            "slug": PLAN,
            "title": "Review Node Name Target",
            "status": "active",
            "version": 0,
            "comments": {},
        },
    )
    (root / "docs" / "state" / "proj-placeholder").mkdir(parents=True, exist_ok=True)
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt", "docs")
    _git(root, "commit", "-q", "-m", "chore: seed repository")
    (isolated_reckon_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _record_directory() -> Path:
    return review_module.run_review_record_directory(Path(run_dir(REVIEWED_RUN_ID)))


def _record_path() -> Path:
    return _record_directory() / f"{REVIEWED_RUN_ID}.at-{DISPATCHED_HEAD}.json"


def _declared_paths() -> list[str]:
    return review_module.run_review_record_paths(
        Path(run_dir(REVIEWED_RUN_ID)),
        REVIEWED_RUN_ID,
        reviewed_head_sha=DISPATCHED_HEAD,
    )


def _round(named: str = REVIEW_RECORD_NAME) -> dict:
    """A complete review record describing the dispatched head.

    ``named`` is the ``review_run_id`` the record carries: by default the run's
    own id without its suffix, which is the shape the reported record had.
    """
    return {
        "status": "parsed",
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN_ID,
        "review_run_id": named,
        "reviewed_base_sha": REVIEWED_BASE,
        "reviewed_head_sha": DISPATCHED_HEAD,
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 20),
        "findings": [],
        "total": TOTAL,
        "timestamp": "2026-10-19T23:05:00Z",
    }


def _write_round(path: Path, body: dict, mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")
    stamp = os.path.getmtime(path) if mtime is None else mtime
    os.utime(path, (stamp, stamp))
    return path


def _pointer(
    run_id: str, repository: Path, tmp_path: Path, declared: list[str]
) -> None:
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: review-of-reviewed-run\n"
        "status: complete\n"
        "checkpoint: the review record is stored\n",
        encoding="utf-8",
    )
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "launch": "in-harness",
            "role": "review",
            "backend": "native",
            "created_at": DISPATCH_TIME,
            "manifest_path": str(manifest),
            "node": {
                "id": REVIEW_NODE_ID,
                "plan": PLAN,
                "section": "s1",
                "time_budget": "25m",
                "role": "review",
                "write_paths": declared,
            },
        },
    )


def _deliver(repository: Path, tmp_path: Path, named: str = REVIEW_RECORD_NAME) -> None:
    """Stage the review run's pointer, record and treated exit as delivered."""
    _pointer(REVIEW_RUN_ID, repository, tmp_path, _declared_paths())
    _write_round(_record_path(), _round(named))
    run_root = Path(run_dir(REVIEW_RUN_ID))
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "exit.json").write_text(
        json.dumps({"run_id": REVIEW_RUN_ID, "exit_code": 0}), encoding="utf-8"
    )


def test_a_record_naming_its_run_without_the_suffix_promotes(
    repository: Path, tmp_path: Path
) -> None:
    """The name resolves to the one run and the outcome is the stored review."""
    _deliver(repository, tmp_path)
    pointer = json.loads(pointer_path(REVIEW_RUN_ID).read_text(encoding="utf-8"))

    assert (
        _resolve_promotion_outcome(REVIEW_RUN_ID, pointer, verdict="not-run")
        == f"review scored {TOTAL}, 0 finding(s)"
    )

    result = crew.complete(REVIEW_RUN_ID, gate="not-run", root=repository)

    assert result["record"]["run_id"] == REVIEW_RUN_ID
    assert result["pointer_removed"] is True
    assert not pointer_path(REVIEW_RUN_ID).exists()


def test_a_name_matching_two_runs_is_refused_naming_both(
    repository: Path, tmp_path: Path
) -> None:
    """A shortened name that reaches two runs is ambiguity, not a delivery."""
    _deliver(repository, tmp_path)
    _pointer(SIBLING_RUN_ID, repository, tmp_path, _declared_paths())

    with pytest.raises(CrewError) as raised:
        crew.complete(REVIEW_RUN_ID, gate="not-run", root=repository)

    refusal = str(raised.value)
    assert "record names a different review run" in refusal, refusal
    assert REVIEW_RUN_ID in refusal, refusal
    assert SIBLING_RUN_ID in refusal, refusal


def test_a_name_matching_no_run_is_refused(repository: Path, tmp_path: Path) -> None:
    """A name that resolves to no run is refused, naming the run it was read on."""
    _deliver(repository, tmp_path, named=UNRELATED_RUN_ID)

    with pytest.raises(CrewError) as raised:
        crew.complete(REVIEW_RUN_ID, gate="not-run", root=repository)

    refusal = str(raised.value)
    assert "record names a different review run" in refusal, refusal
    assert UNRELATED_RUN_ID in refusal, refusal
    assert REVIEW_RUN_ID in refusal, refusal


def test_an_exact_name_still_promotes(repository: Path, tmp_path: Path) -> None:
    """The exact-id path is unchanged: a record naming its run exactly delivers."""
    _deliver(repository, tmp_path, named=REVIEW_RUN_ID)

    result = crew.complete(REVIEW_RUN_ID, gate="not-run", root=repository)

    assert result["record"]["review"]["total"] == TOTAL
