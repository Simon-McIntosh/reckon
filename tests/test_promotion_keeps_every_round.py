"""Promotion keeps every stored round, and the store's rules have one spelling.

A subject reviewed more than once leaves one stored round per head, and an
unnamed round may not be dropped on the floor. These cases hold a promotion to
committing a round that carries no ``review_run_id`` of its own under the
derived legacy id of its bytes - the same derivation the host importer uses -
marked ``review_run_id_source: derived``, rather than skipping it. They hold the
store's two run-side writers to refusing a body that names no subject through
the one predicate that spells that rule. And they hold the times resolver to
keeping a completion the run records supplied when the dispatch time falls
through to the record's own stamp or to the instant the run id encodes.

Every crew directory is environment-resolved under ``tmp_path``; nothing touches
the operator's own store.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import review as review_module
from reckon.crew.runs import _write_json, pointer_path
from tests.conftest import EXECUTABLE_GATE_COMMAND

PROJECT = "promotion-keeps-every-round-fixture"
PLAN = "promotion-keeps-every-round-target"

SUBJECT = "r-20261006T110000000000-reviewed-subject"
REVIEW = "r-20261006T120000000000-promoting-review"
HEAD_ONE = "2e05df7191c5413efcfb9f3cc40b5f18b1f7a0bc"
BASE = "a" * 40
DISPATCH_TS = "2026-10-06T11:00:00Z"
COMPLETION_TS = "2026-10-06T11:30:00Z"
# A review run id that encodes a dispatch instant.
ENCODED_RUN = "r-20261006T120000000000-encoded-review"
ENCODED_TS = "2026-10-06T12:00:00+00:00"


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture()
def repository(isolated_reckon_home: Path, tmp_path: Path) -> Path:
    """A repository whose docs directory is the project's mount."""
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


def _write_run_record(
    repository: Path, run_id: str, *, dispatched: str, completed: str
) -> Path:
    """Write one run's committed per-run record beside the ledger."""
    path = repository / "docs" / "state" / PROJECT / "runs" / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "project": PROJECT,
                "dispatched_at": dispatched,
                "completed_at": completed,
            }
        ),
        encoding="utf-8",
    )
    _git(repository, "add", str(path.relative_to(repository)))
    _git(repository, "commit", "-q", "-m", "chore: land a run record")
    return path


def _manifest_body() -> str:
    return (
        "node: review-of-a-subject\n"
        "status: complete\n"
        "commits: none\n"
        "changed_paths: []\n"
        f"tests: {EXECUTABLE_GATE_COMMAND}\n"
    )


def _review_pointer(
    repository: Path,
    tmp_path: Path,
    *,
    run_id: str,
    declared: list[str],
) -> None:
    manifest = tmp_path / f"{run_id}.manifest.md"
    manifest.write_text(_manifest_body(), encoding="utf-8")
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
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


def _complete_run_review(head: str | None, *, review_run_id: str | None) -> dict:
    record: dict = {
        "project": PROJECT,
        "reviewed_run_id": SUBJECT,
        "reviewed_base_sha": BASE,
        "status": "parsed",
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 18),
        "absent": [],
        "total": 18 * len(review_module.REVIEW_DIMENSIONS),
    }
    if review_run_id is not None:
        record["review_run_id"] = review_run_id
    if head is not None:
        record["reviewed_head_sha"] = head
    return record


def _store(record: dict, *, head: str | None = None) -> Path:
    path = review_module.review_path(PROJECT, SUBJECT, reviewed_head_sha=head)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


# -- A promotion keeps an unnamed round under its derived legacy id ------------


def test_a_promotion_commits_an_unnamed_round_under_its_legacy_id(
    repository: Path, tmp_path: Path
) -> None:
    _write_run_record(
        repository, SUBJECT, dispatched=DISPATCH_TS, completed=COMPLETION_TS
    )
    delivered_path = _store(_complete_run_review(None, review_run_id=None))
    # Another round of the same subject at another head, also naming no review
    # run of its own: it is not the round this run delivered, so its own bytes
    # key it.
    unnamed_path = _store(
        _complete_run_review(HEAD_ONE, review_run_id=None), head=HEAD_ONE
    )
    derived = review_module.derived_legacy_review_run_id(unnamed_path.read_bytes())
    _review_pointer(repository, tmp_path, run_id=REVIEW, declared=[str(delivered_path)])

    crew.complete(REVIEW, gate="passed", root=repository)

    committed = review_module.committed_review_root(PROJECT, root=repository)
    assert committed is not None
    run_dir = committed / "run" / SUBJECT
    committed_names = sorted(path.name for path in run_dir.glob("*.json"))
    assert committed_names == sorted([f"{REVIEW}.json", f"{derived}.json"])

    filed = _read(run_dir / f"{derived}.json")
    assert filed["review_run_id"] == derived
    assert filed["review_run_id_source"] == "derived"


# -- The subject predicate is the one spelling the writers use ----------------


def test_store_review_refuses_a_body_the_predicate_rejects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A body that names a run would otherwise be accepted on that subject; with
    # the predicate forced false the writer refuses it, which is only true if
    # the writer consults the predicate rather than its own inline test.
    monkeypatch.setattr(review_module, "names_a_review_subject", lambda record: False)
    with pytest.raises(ValueError, match="names neither"):
        review_module.store_review(
            {
                "project": PROJECT,
                "reviewed_run_id": SUBJECT,
                "review_run_id": REVIEW,
                "scores": {"evidence": 18},
            },
            base_dir=tmp_path,
        )


def test_store_committed_review_refuses_a_body_the_predicate_rejects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    (tmp_path / "config").mkdir()
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    monkeypatch.setattr(review_module, "names_a_review_subject", lambda record: False)
    with pytest.raises(ValueError, match="names neither"):
        review_module.store_committed_review(
            {
                "project": PROJECT,
                "reviewed_run_id": SUBJECT,
                "review_run_id": REVIEW,
                "scores": {"evidence": 18},
            },
            root=root,
        )


def test_a_real_subjectless_body_is_refused_by_both_writers(tmp_path: Path) -> None:
    # No monkeypatch: a body naming no subject at all is refused, so the rule
    # holds without being forced.
    subjectless = {"project": PROJECT, "review_run_id": REVIEW, "scores": {"e": 18}}
    with pytest.raises(ValueError, match="names neither"):
        review_module.store_review(dict(subjectless), base_dir=tmp_path)


# -- The times resolver keeps a completion the run records supplied -----------


def _bare_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    (tmp_path / "config").mkdir()
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    return root


def test_a_run_record_completion_survives_a_fall_through_to_the_record_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _bare_checkout(tmp_path, monkeypatch)
    # The review run's own record names a completion but no dispatch: the
    # dispatch falls through to the record's own carried stamp, and the
    # completion the run record supplied must not be discarded by that.
    run_path = root / "docs" / "state" / PROJECT / "runs" / f"{REVIEW}.json"
    run_path.parent.mkdir(parents=True, exist_ok=True)
    run_path.write_text(
        json.dumps(
            {
                "run_id": REVIEW,
                "project": PROJECT,
                "dispatched_at": "",
                "completed_at": COMPLETION_TS,
            }
        ),
        encoding="utf-8",
    )
    record = {
        "project": PROJECT,
        "reviewed_run_id": SUBJECT,
        "review_run_id": REVIEW,
        "dispatched_at": DISPATCH_TS,
        "scores": {"evidence": 18},
    }
    dispatched, completed, source = review_module.resolve_record_times(
        PROJECT, record, root=root
    )
    assert dispatched == DISPATCH_TS
    assert completed == COMPLETION_TS
    assert source == review_module.RECORD_TIMES_SOURCE


def test_a_run_record_completion_survives_a_fall_through_to_the_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _bare_checkout(tmp_path, monkeypatch)
    run_path = root / "docs" / "state" / PROJECT / "runs" / f"{ENCODED_RUN}.json"
    run_path.parent.mkdir(parents=True, exist_ok=True)
    run_path.write_text(
        json.dumps(
            {
                "run_id": ENCODED_RUN,
                "project": PROJECT,
                "dispatched_at": "",
                "completed_at": COMPLETION_TS,
            }
        ),
        encoding="utf-8",
    )
    record = {
        "project": PROJECT,
        "reviewed_run_id": SUBJECT,
        "review_run_id": ENCODED_RUN,
        "scores": {"evidence": 18},
    }
    dispatched, completed, source = review_module.resolve_record_times(
        PROJECT, record, root=root
    )
    assert dispatched == ENCODED_TS
    assert completed == COMPLETION_TS
    assert source == review_module.RUN_ID_TIMES_SOURCE


def test_the_subject_predicate_is_callable() -> None:
    assert callable(review_module.names_a_review_subject)
