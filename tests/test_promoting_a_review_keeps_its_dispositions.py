"""A disposition made before its review run is promoted survives the promotion.

A coordinator disposes a finding and a sub-floor dimension on the record its
review left in the host staging store, then promotes the review run — the order
the obligations list invites. The promotion rewrites the record, and these
cases hold it to carrying every disposition already recorded against that
record into whatever it writes, so a disposition is never dropped by a later
write of the same review.

Every crew directory and the committed tree are resolved under temporary
directories; nothing touches the operator's own store.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import crew, flight
from reckon.crew import obligations as obligations_module
from reckon.crew import review as review_module
from reckon.crew import review_dispositions as dispositions_module
from reckon.crew import runs
from reckon.crew.runs import _write_json, pointer_path
from tests.conftest import EXECUTABLE_GATE_COMMAND
from tests.helpers.git import git as _git

PROJECT = "promoting-a-review-fixture"
PLAN = "promoting-a-review-target"

SUBJECT = "r-20261007T100000000000-reviewed-subject"
REVIEW = "r-20261007T110000000000-review-run"
SESSION = "session-that-owns-the-subject"
SHAS: dict[str, str] = {}  # the fixture pins the reviewed base and head to real commits
DISPATCH_TS = "2026-10-07T10:00:00Z"
COMPLETION_TS = "2026-10-07T10:30:00Z"

LOW_DIMENSION = "evidence"
FLOOR = 16
LOW_SCORE = 8


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture()
def repository(isolated_reckon_home: Path, tmp_path: Path, monkeypatch) -> Path:
    """A git checkout whose docs directory is the project's mount."""
    root = tmp_path / "repo"
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (plans / f"{PLAN}.html").write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n',
        encoding="utf-8",
    )
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs"),
        ("commit", "-q", "-m", "test: seed repository"),
    ):
        _git(root, *arguments)
    SHAS["base"] = _git(root, "rev-parse", "HEAD")
    (root / "docs" / "reviewed-work.md").write_text("reviewed work\n", encoding="utf-8")
    _git(root, "add", "docs/reviewed-work.md")
    _git(root, "commit", "-q", "-m", "test: reviewed head")
    SHAS["head"] = _git(root, "rev-parse", "HEAD")
    (isolated_reckon_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    monkeypatch.setattr(
        flight,
        "resolve",
        lambda *args, **kw: SimpleNamespace(
            config={
                "default_backend": "worker",
                "gates": {
                    "dimension_floors": {LOW_DIMENSION: FLOOR},
                },
            }
        ),
    )
    return root


def _write_run_record(repository: Path) -> Path:
    path = repository / "docs" / "state" / PROJECT / "runs" / f"{SUBJECT}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "run_id": SUBJECT,
                "project": PROJECT,
                "dispatched_at": DISPATCH_TS,
                "completed_at": COMPLETION_TS,
            }
        ),
        encoding="utf-8",
    )
    _git(repository, "add", str(path.relative_to(repository)))
    _git(repository, "commit", "-q", "-m", "chore: land a run record")
    return path


def _subject_pointer(repository: Path) -> None:
    """The reviewed run is live in this session, so its review owes a duty."""
    _write_json(
        pointer_path(SUBJECT),
        {
            "run_id": SUBJECT,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": _git(repository, "rev-parse", "HEAD"),
            "launch": "in-harness",
            "backend": "native",
            "created_at": DISPATCH_TS,
            "session": SESSION,
            "manifest_path": "",
            "node": {"id": f"work-of-{SUBJECT}", "plan": PLAN, "section": "s2"},
        },
    )


def _review_record() -> dict:
    return {
        "project": PROJECT,
        "reviewed_run_id": SUBJECT,
        "review_run_id": REVIEW,
        "reviewed_base_sha": SHAS["base"],
        "reviewed_head_sha": SHAS["head"],
        "status": "parsed",
        "scores": {
            **dict.fromkeys(review_module.REVIEW_DIMENSIONS, 18),
            LOW_DIMENSION: LOW_SCORE,
        },
        "absent": [],
        "findings": [
            {
                "id": "evidence-1",
                "type": "evidence",
                "text": "The claim names no measurement.",
            }
        ],
        "total": 18 * (len(review_module.REVIEW_DIMENSIONS) - 1) + LOW_SCORE,
    }


def _review_pointer(repository: Path, tmp_path: Path, declared: list[Path]) -> None:
    manifest = tmp_path / "review.manifest.md"
    # The review run's node id spells the run it reviews, and its declared write
    # paths are the record's own two names inside that run's directory.
    gate_log = tmp_path / "review-gate.log"
    gate_log.write_text("1 passed in 0.01s\nEXIT=0\n", encoding="utf-8")
    manifest.write_text(
        "node: review-of-a-subject\n"
        "status: complete\n"
        "commits: none\n"
        "changed_paths: []\n"
        f"tests: {EXECUTABLE_GATE_COMMAND}\n"
        f"test_logs: [{gate_log}]\n",
        encoding="utf-8",
    )
    _write_json(
        pointer_path(REVIEW),
        {
            "run_id": REVIEW,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": _git(repository, "rev-parse", "HEAD"),
            "launch": "in-harness",
            "backend": "native",
            "created_at": DISPATCH_TS,
            "session": SESSION,
            "role": "review",
            "manifest_path": str(manifest),
            "node": {
                "id": f"review-of-{SUBJECT}",
                "plan": PLAN,
                "section": "s2",
                "time_budget": "25m",
                "role": "review",
                "write_paths": [str(path) for path in declared],
            },
        },
    )


def _staged_from_the_reviewer() -> list[Path]:
    """The record as the reviewer leaves it, in the store and in its run dir.

    The supervisor files a delivered record into the store when the review run
    ends, and the reviewer's own copy stays in the reviewed run's record
    directory. Promotion files those copies into the store again, which is the
    write a disposition recorded on the store copy has to survive.
    """
    record = _review_record()
    keyed = review_module.review_path(PROJECT, SUBJECT, reviewed_head_sha=SHAS["head"])
    delivered = [
        Path(path)
        for path in review_module.run_review_record_paths(
            runs.run_dir(SUBJECT), SUBJECT, reviewed_head_sha=SHAS["head"]
        )
    ]
    for path in (keyed, *delivered):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")
    return [keyed, *delivered]


def _committed_record(repository: Path) -> dict:
    committed = review_module.committed_review_root(PROJECT, root=repository)
    assert committed is not None
    directory = committed / review_module.COMMITTED_RUN_DIRNAME / SUBJECT
    files = sorted(directory.glob(f"{REVIEW}.json"))
    assert files, f"no committed record for {REVIEW} under {directory}"
    return _read(files[0])


def _sub_floor_duties() -> list[dict]:
    view = obligations_module.obligations(PROJECT, SESSION)
    return [
        row
        for row in view.get("duties") or []
        if row.get("kind") == obligations_module.SUB_FLOOR_DUTY_KIND
    ]


def _dispose_before_promotion() -> tuple[Path, Path]:
    """Answer the finding and the low dimension on the record the reader selects."""
    finding_path = dispositions_module.record_finding_disposition(
        PROJECT, SUBJECT, 0, kind="folded", node="a-repair-node"
    )
    dimension_path = dispositions_module.record_dimension_disposition(
        PROJECT, SUBJECT, LOW_DIMENSION, kind="exempted", reason="the bar was set"
    )
    return finding_path, dimension_path


def test_a_finding_and_a_dimension_disposed_before_promotion_survive_it(
    repository: Path, tmp_path: Path
) -> None:
    _write_run_record(repository)
    _subject_pointer(repository)
    staged_paths = _staged_from_the_reviewer()
    _review_pointer(repository, tmp_path, staged_paths[1:])

    # Before the promotion the record is disposed on, and the reading the
    # coordinator acts from reports the dimension answered.
    finding_path, dimension_path = _dispose_before_promotion()
    assert _sub_floor_duties() == []
    staged = _read(finding_path)
    assert staged[review_module.FINDING_DISPOSITIONS_KEY]["0"]["kind"] == "folded"
    assert (
        _read(dimension_path)[review_module.DIMENSION_DISPOSITIONS_KEY][LOW_DIMENSION][
            "kind"
        ]
        == "exempted"
    )

    crew.complete(REVIEW, gate="passed", root=repository)

    committed = _read(
        review_module.committed_review_root(PROJECT, root=repository)
        / review_module.COMMITTED_RUN_DIRNAME
        / SUBJECT
        / f"{REVIEW}.json"
    )
    assert committed[review_module.FINDING_DISPOSITIONS_KEY]["0"]["kind"] == "folded"
    assert (
        committed[review_module.DIMENSION_DISPOSITIONS_KEY][LOW_DIMENSION]["kind"]
        == "exempted"
    )
    # The obligation the coordinator already answered is not raised again.
    assert _sub_floor_duties() == []
