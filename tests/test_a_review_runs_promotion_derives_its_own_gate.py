"""A review run's promotion derives its own gate, total and manifest wait.

A review run changes no code, so the evidence its promotion rests on is the
record it stored rather than a suite result, and its manifest's ``tests:``
field names the checks the node ran in whatever shape the node's report takes.
Four behaviours follow, each a case below:

  * a review promoted with ``crew complete`` and no gate flags, whose manifest's
    ``tests:`` field holds a JSON list rather than a command;
  * a record carrying all six dimension scores and no total, promoted with the
    total those scores sum to;
  * a review whose manifest appears a few seconds after the promotion starts;
  * a review whose manifest never appears, refused naming the path.

A fifth case holds the other direction: an explicit gate check the caller
passes is kept, so the derived check fills only what the caller left absent.

Every crew directory is environment-resolved under ``tmp_path``, so nothing
here reads or writes the operator's own store.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import review as review_module
from reckon.crew.node import CrewError
from reckon.crew.runs import _write_json, pointer_path
from tests.helpers.git import git as _git

PROJECT = "review-gate-fixture"
PLAN = "review-gate-target"
SUBJECT = "r-20261006T110000000000-reviewed-subject"
REVIEW = "r-20261006T120000000000-promoting-review"
DISPATCHED_HEAD = "c" * 40
BASE = "a" * 40
DISPATCH_TS = "2026-10-06T11:00:00Z"
COMPLETION_TS = "2026-10-06T11:30:00Z"

# A manifest whose ``tests:`` line holds a JSON list: the shape the field takes
# on a review node, and the one a promotion used to read as a command string.
TESTS_JSON_LIST = '["pytest -q tests/a.py", "pytest -q tests/b.py"]'


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture()
def repository(isolated_reckon_home: Path, tmp_path: Path) -> Path:
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
    (isolated_reckon_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _write_run_record(repository: Path, run_id: str) -> Path:
    """Land the reviewed run's project record, as that run's promotion would."""
    path = repository / "docs" / "state" / PROJECT / "runs" / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
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


def _manifest_body(tests: str = TESTS_JSON_LIST) -> str:
    return (
        "node: review-of-a-subject\n"
        "status: complete\n"
        "commits: none\n"
        "changed_paths: []\n"
        f"tests: {tests}\n"
    )


def _review_pointer(repository: Path, *, declared: list[str], manifest: Path) -> None:
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
            "role": "review",
            "manifest_path": str(manifest),
            "node": {
                "id": f"review-of-{SUBJECT}",
                "plan": PLAN,
                "section": "s3",
                "time_budget": "25m",
                "role": "review",
                "write_paths": list(declared),
            },
        },
    )


def _completed_review(*, total: bool) -> dict:
    record: dict = {
        "project": PROJECT,
        "reviewed_run_id": SUBJECT,
        "review_run_id": REVIEW,
        "reviewed_base_sha": BASE,
        "reviewed_head_sha": DISPATCHED_HEAD,
        "status": "parsed",
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 18),
        "absent": [],
        "added_failure_count": 0,
        "added_failure_ids": [],
        "findings": [],
    }
    if total:
        record["total"] = 18 * len(review_module.REVIEW_DIMENSIONS)
    return record


def _store(record: dict) -> Path:
    path = review_module.review_path(
        PROJECT, SUBJECT, reviewed_head_sha=DISPATCHED_HEAD
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _promote(repository: Path, **flags) -> dict:
    return crew.complete(REVIEW, gate="passed", root=repository, **flags)


def _committed_review(repository: Path) -> dict:
    committed = review_module.committed_review_root(PROJECT, root=repository)
    assert committed is not None
    return _read(committed / "run" / SUBJECT / f"{REVIEW}.json")


def _stage_delivered(
    repository: Path, tmp_path: Path, *, total: bool = True
) -> tuple[Path, Path]:
    _write_run_record(repository, SUBJECT)
    delivered = _store(_completed_review(total=total))
    manifest = tmp_path / f"{REVIEW}.manifest.md"
    return delivered, manifest


def test_case1_no_gate_flags_with_a_json_list_tests_field(
    repository: Path, tmp_path: Path
) -> None:
    delivered, manifest = _stage_delivered(repository, tmp_path)
    manifest.write_text(_manifest_body(), encoding="utf-8")
    _review_pointer(repository, declared=[str(delivered)], manifest=manifest)

    _promote(repository)

    assert _committed_review(repository)["review_run_id"] == REVIEW


def test_case2_six_scores_and_no_total(repository: Path, tmp_path: Path) -> None:
    delivered, manifest = _stage_delivered(repository, tmp_path, total=False)
    manifest.write_text(_manifest_body(), encoding="utf-8")
    _review_pointer(repository, declared=[str(delivered)], manifest=manifest)

    _promote(repository)

    stored = _committed_review(repository)
    assert stored["total"] == 18 * len(review_module.REVIEW_DIMENSIONS)


def test_case3_manifest_appears_a_few_seconds_late(
    repository: Path, tmp_path: Path
) -> None:
    delivered, manifest = _stage_delivered(repository, tmp_path)
    _review_pointer(repository, declared=[str(delivered)], manifest=manifest)

    def write_later() -> None:
        time.sleep(3.0)
        manifest.write_text(_manifest_body(), encoding="utf-8")

    writer = threading.Thread(target=write_later, daemon=True)
    writer.start()
    try:
        _promote(repository)
    finally:
        writer.join(timeout=10)

    assert _committed_review(repository)["review_run_id"] == REVIEW


def test_case4_manifest_never_appears(repository: Path, tmp_path: Path) -> None:
    delivered, manifest = _stage_delivered(repository, tmp_path)
    _review_pointer(repository, declared=[str(delivered)], manifest=manifest)

    with pytest.raises(CrewError) as refusal:
        _promote(repository)

    assert str(manifest) in str(refusal.value)


def test_case5_an_explicit_gate_check_is_kept(repository: Path, tmp_path: Path) -> None:
    """A caller's own gate command and log are kept, not replaced by the record."""
    delivered, manifest = _stage_delivered(repository, tmp_path)
    manifest.write_text(_manifest_body(), encoding="utf-8")
    _review_pointer(repository, declared=[str(delivered)], manifest=manifest)
    gate_log = tmp_path / "operator-gate.log"
    gate_log.write_text("EXIT=0\n", encoding="utf-8")
    command = "pytest -q tests/operator_check.py"

    result = _promote(
        repository,
        gate_check={
            "command": command,
            "log_path": str(gate_log),
            "exit_status": 0,
        },
    )

    written = result["record"]["gate_check"]
    # The command is the operator's own, and the log is the one they named,
    # preserved into the run directory rather than replaced by the record the
    # derived check would have cited.
    assert written["command"] == command, written
    assert written["log_path"] != str(delivered), written
    assert Path(written["log_path"]).read_text(encoding="utf-8") == "EXIT=0\n"


@pytest.mark.parametrize(
    "half", ["command", "log"], ids=["command-without-log", "log-without-command"]
)
def test_case6_a_half_filled_gate_check_is_refused(
    repository: Path, tmp_path: Path, half: str
) -> None:
    """One of the command and its log is refused rather than completed.

    Completing it would pair the caller's half with evidence the promotion
    derived from the record, which records a run nobody performed.
    """
    delivered, manifest = _stage_delivered(repository, tmp_path)
    manifest.write_text(_manifest_body(), encoding="utf-8")
    _review_pointer(repository, declared=[str(delivered)], manifest=manifest)
    gate_log = tmp_path / "operator-gate.log"
    gate_log.write_text("EXIT=0\n", encoding="utf-8")
    given = (
        {"command": "pytest -q tests/operator_check.py"}
        if half == "command"
        else {"log_path": str(gate_log), "exit_status": 0}
    )

    with pytest.raises(CrewError) as refusal:
        _promote(repository, gate_check=given)

    assert "gate-command" in str(refusal.value), refusal.value
    assert "gate-log-path" in str(refusal.value), refusal.value
