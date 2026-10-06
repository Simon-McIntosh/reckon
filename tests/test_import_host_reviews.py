"""The host review import commits records and quarantines everything else.

A project's host staging store also collected whatever a session left there —
scratch directories, caches, probe files. The import sorts each project's store
into the records a committed review tree can hold and the non-records it cannot,
dry-runs by default, and under ``--write`` commits each recognised plan-review
and run-review record through the shared committed writer and moves every
non-record to a quarantine directory outside the store without deleting it.
These cases drive the script against a synthesised config home and checkout:
the dry run lists the inventory with path, size and modification time and writes
nothing; ``--write`` commits each record once — a duplicate that resolves the
same committed path is not imported twice — moves the non-records to quarantine,
and leaves a second pass importing zero.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import pytest

PROJECT = "demo-store"

DISPATCH_TS = "2026-10-06T09:00:00+00:00"
COMPLETION_TS = "2026-10-06T09:07:30+00:00"

REVIEWED_RUN = "r-run-a"
PLAN_REVIEW_RUN = "r-review-plan"
RUN_REVIEW_RUN = "r-review-run"
LEGACY_REVIEW_RUN = "r-legacy-review-run"
DERIVED_REVIEWED_RUN = "r-derived-run"
_RUN_RECORD_IDS = (
    REVIEWED_RUN,
    "r-old-run",
    DERIVED_REVIEWED_RUN,
    RUN_REVIEW_RUN,
    PLAN_REVIEW_RUN,
    LEGACY_REVIEW_RUN,
)


def _load_script():
    """Import ``scripts/import_host_reviews.py`` by path, as the CLI runs it."""
    script = Path(__file__).resolve().parents[1] / "scripts" / "import_host_reviews.py"
    spec = importlib.util.spec_from_file_location("import_host_reviews", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A synthesised config home and checkout, isolated from the real ones."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    (tmp_path / "config").mkdir()
    store = tmp_path / "config" / "crew" / "reviews" / PROJECT
    store.mkdir(parents=True)
    repo = tmp_path / "repo"
    (repo / "docs" / "state" / PROJECT).mkdir(parents=True)
    return {"tmp": tmp_path, "store": store, "repo": repo}


def _run_record(repo: Path, run_id: str) -> None:
    """Write one run's committed per-run record so its times resolve."""
    path = repo / "docs" / "state" / PROJECT / "runs" / f"{run_id}.json"
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


def _write_record(store: Path, name: str, body: dict) -> Path:
    path = store / name
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def _fixture_records(store: Path) -> None:
    """One run review, its duplicate, a plan review, a legacy review, non-records."""
    _write_record(
        store,
        f"{REVIEWED_RUN}.json",
        {
            "project": PROJECT,
            "review_run_id": RUN_REVIEW_RUN,
            "reviewed_run_id": REVIEWED_RUN,
            "status": "parsed",
            "findings": [],
        },
    )
    # The same review run staged again under its reviewed-head sibling: one
    # committed path, so the second file is a duplicate rather than a record.
    _write_record(
        store,
        f"{REVIEWED_RUN}.at-{'a' * 40}.json",
        {
            "project": PROJECT,
            "review_run_id": RUN_REVIEW_RUN,
            "reviewed_run_id": REVIEWED_RUN,
            "reviewed_head_sha": "a" * 40,
            "status": "parsed",
            "findings": [],
        },
    )
    _write_record(
        store,
        "plan-demo.v2.json",
        {
            "project": PROJECT,
            "review_run_id": PLAN_REVIEW_RUN,
            "plan_slug": "demo",
            "plan_version": 2,
            "status": "ready",
            "findings": [],
        },
    )
    # An older vintage names its review run as reviewer_run_id only.
    _write_record(
        store,
        f"{LEGACY_REVIEW_RUN}.json",
        {
            "project": PROJECT,
            "reviewer_run_id": LEGACY_REVIEW_RUN,
            "reviewed_run_id": "r-old-run",
            "status": "parsed",
            "findings": [],
        },
    )
    # A review that names no review run of its own: recognised by its subject
    # and material, filed under a derived id.
    _write_record(
        store,
        f"{DERIVED_REVIEWED_RUN}.json",
        {
            "project": PROJECT,
            "reviewed_run_id": DERIVED_REVIEWED_RUN,
            "reviewed_head_sha": "b" * 40,
            "status": "parsed",
            "findings": [],
        },
    )
    # Non-records: a body that names a run but carries no review material, a
    # non-JSON file, and a scratch directory.
    (store / "not-a-review.json").write_text(
        json.dumps({"reviewed_run_id": "r-some-run"}), encoding="utf-8"
    )
    (store / "scratch.bin").write_bytes(b"\x00\x01not json")
    (store / ".probe-write.txt").write_text("scratch", encoding="utf-8")
    scratch = store / "scratch-dir"
    scratch.mkdir()
    (scratch / "leftover.txt").write_text("x" * 11, encoding="utf-8")


def _run_cli(module, argv: list[str]) -> tuple[int, str]:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = module.main(argv)
    return code, buffer.getvalue()


def test_dry_run_lists_inventory_and_writes_nothing(harness) -> None:
    module = _load_script()
    store = harness["store"]
    repo = harness["repo"]
    _fixture_records(store)
    for run_id in _RUN_RECORD_IDS:
        _run_record(repo, run_id)

    code, out = _run_cli(module, ["--project", PROJECT, "--root", str(repo)])

    assert code == 0
    assert f"project: {PROJECT}" in out
    assert "recognised records:" in out
    # The inventory names each non-record by path, size and modification time.
    assert "non-records:" in out
    assert ".probe-write.txt" in out
    assert "scratch-dir" in out
    assert "dry run: nothing written" in out

    # Nothing was written: the committed tree is untouched and every host file
    # is still where it was.
    committed = repo / "docs" / "state" / PROJECT / "reviews"
    assert not committed.exists()
    assert (store / ".probe-write.txt").exists()
    assert (store / "scratch-dir" / "leftover.txt").exists()


def test_write_commits_records_once_and_quarantines_non_records(harness) -> None:
    module = _load_script()
    tmp = harness["tmp"]
    store = harness["store"]
    repo = harness["repo"]
    _fixture_records(store)
    for run_id in _RUN_RECORD_IDS:
        _run_record(repo, run_id)

    code, out = _run_cli(module, ["--project", PROJECT, "--root", str(repo), "--write"])

    assert code == 0
    committed = repo / "docs" / "state" / PROJECT / "reviews"

    # The run review is filed under the reviewed run, timed from the review
    # run's own record rather than the store clock.
    run_file = committed / "run" / REVIEWED_RUN / f"{RUN_REVIEW_RUN}.json"
    assert run_file.is_file()
    stored = json.loads(run_file.read_text(encoding="utf-8"))
    assert stored["dispatched_at"] == DISPATCH_TS
    assert stored["completed_at"] == COMPLETION_TS
    # The duplicate resolved the same path and was not written a second time.
    assert sorted(p.name for p in (committed / "run" / REVIEWED_RUN).iterdir()) == [
        f"{RUN_REVIEW_RUN}.json"
    ]

    # The plan review is filed under its plan slug.
    assert (committed / "plan" / "demo" / f"{PLAN_REVIEW_RUN}.json").is_file()

    # The legacy record keeps its own reviewer_run_id, which is the run's real
    # identity, rather than a derived one.
    legacy = committed / "run" / "r-old-run" / f"{LEGACY_REVIEW_RUN}.json"
    assert legacy.is_file()
    legacy_body = json.loads(legacy.read_text(encoding="utf-8"))
    assert legacy_body["review_run_id"] == LEGACY_REVIEW_RUN
    assert legacy_body.get("review_run_id_source") != "derived"

    # A record that names no review run is filed under the derived id its bytes
    # hash to and marked derived in the committed body.
    expected = (
        "legacy-"
        + hashlib.sha256(
            (store / f"{DERIVED_REVIEWED_RUN}.json").read_bytes()
        ).hexdigest()[:12]
    )
    derived = committed / "run" / DERIVED_REVIEWED_RUN / f"{expected}.json"
    assert derived.is_file()
    derived_body = json.loads(derived.read_text(encoding="utf-8"))
    assert derived_body["review_run_id"] == expected
    assert derived_body["review_run_id_source"] == "derived"
    # The staging file of a record is left in place: only non-records move.
    assert (store / f"{DERIVED_REVIEWED_RUN}.json").is_file()

    # The imported count equals the recognised records; the duplicate is not a
    # second import.
    assert "imported: 4" in out
    assert "duplicates: 1" in out

    # Every non-record was moved outside the store, nothing deleted.
    quarantine = tmp / "config" / "crew" / "reviews-quarantine" / PROJECT
    for name in (".probe-write.txt", "scratch.bin", "not-a-review.json"):
        assert not (store / name).exists()
        assert (quarantine / name).is_file()
    assert not (store / "scratch-dir").exists()
    assert (quarantine / "scratch-dir" / "leftover.txt").read_text(
        encoding="utf-8"
    ) == "x" * 11
    # The quarantine directory is outside the store.
    assert store not in quarantine.parents


def test_derived_legacy_id_is_idempotent_across_a_second_pass(harness) -> None:
    module = _load_script()
    store = harness["store"]
    repo = harness["repo"]
    # Only the derived-id record is present, so the test isolates its id.
    _write_record(
        store,
        f"{DERIVED_REVIEWED_RUN}.json",
        {
            "project": PROJECT,
            "reviewed_run_id": DERIVED_REVIEWED_RUN,
            "reviewed_head_sha": "b" * 40,
            "status": "parsed",
            "findings": [],
        },
    )
    _run_record(repo, DERIVED_REVIEWED_RUN)
    original = (store / f"{DERIVED_REVIEWED_RUN}.json").read_bytes()

    argv = ["--project", PROJECT, "--root", str(repo), "--write"]
    assert _run_cli(module, argv)[0] == 0
    # The same bytes derive the same id, so the second pass imports nothing and
    # the committed file is untouched.
    committed = (
        repo / "docs" / "state" / PROJECT / "reviews" / "run" / DERIVED_REVIEWED_RUN
    )
    first = sorted(p.name for p in committed.iterdir())
    code, out = _run_cli(module, argv)
    assert code == 0
    assert "imported: 0" in out
    assert sorted(p.name for p in committed.iterdir()) == first
    assert (store / f"{DERIVED_REVIEWED_RUN}.json").read_bytes() == original


def test_a_second_write_pass_imports_zero(harness) -> None:
    module = _load_script()
    store = harness["store"]
    repo = harness["repo"]
    _fixture_records(store)
    for run_id in _RUN_RECORD_IDS:
        _run_record(repo, run_id)

    argv = ["--project", PROJECT, "--root", str(repo), "--write"]
    assert _run_cli(module, argv)[0] == 0
    code, out = _run_cli(module, argv)

    assert code == 0
    assert "imported: 0" in out


def test_exit_contract_zero_with_a_target_and_one_without(harness) -> None:
    module = _load_script()
    repo = harness["repo"]
    store = harness["store"]
    _fixture_records(store)

    # A resolvable checkout target is a real run: a clean exit.
    assert _run_cli(module, ["--project", PROJECT, "--root", str(repo)])[0] == 0
    # No committed tree resolves from a root with no docs directory: the script
    # names the refusal and exits non-zero rather than reporting a phantom run.
    missing = harness["tmp"] / "no-docs"
    missing.mkdir()
    assert _run_cli(module, ["--project", PROJECT, "--root", str(missing)])[0] == 1


def test_recognition_needs_a_subject_and_rejects_a_bare_reference() -> None:
    module = _load_script()
    raw = b"some bytes"
    derived = "legacy-" + hashlib.sha256(raw).hexdigest()[:12]

    # A plan review and a run review are recognised; the explicit review run id
    # is kept and nothing is derived.
    assert module._review_identity(
        raw,
        {"review_run_id": "r-x", "plan_slug": "p", "plan_version": 1, "findings": []},
    ) == ("plan", "p", "r-x", False)
    assert module._review_identity(
        raw, {"review_run_id": "r-x", "reviewed_run_id": "r-y", "scores": {}}
    ) == ("run", "r-y", "r-x", False)
    # The legacy review-run field is kept as the identity rather than derived.
    assert module._review_identity(
        raw, {"reviewer_run_id": "r-x", "reviewed_run_id": "r-y", "rubric": "design"}
    ) == ("run", "r-y", "r-x", False)
    # No review run id: recognised by subject and material, filed under the
    # stable derived id.
    assert module._review_identity(
        raw, {"reviewed_run_id": "r-y", "reviewed_head_sha": "c" * 40, "findings": []}
    ) == ("run", "r-y", derived, True)

    # A bare reference carries neither subject nor material and is not a record.
    assert module._review_identity(raw, {"reviewed_run_id": "r-y"}) is None
    assert module._review_identity(raw, {"hello": "world"}) is None
    assert module._review_identity(raw, ["not", "a", "mapping"]) is None
    # A plan name with no version cannot resolve a committed path and is not
    # misread as a run just because the filename looks like one.
    assert module._review_identity(raw, {"plan_slug": "p", "findings": []}) is None
    assert (
        module._review_identity(
            raw,
            {"plan_slug": "p", "findings": []},
            "r-20260101T000000000000-a-run.json",
        )
        is None
    )


def test_recognition_takes_the_subject_from_a_filename_when_the_body_omits_it() -> None:
    module = _load_script()
    raw = b"run review bytes"
    derived = "legacy-" + hashlib.sha256(raw).hexdigest()[:12]
    # A review that carries findings but names neither a plan nor a reviewed run:
    # the store named the reviewed run in the filename.
    body = {"project": "demo-store", "findings": [], "reviewed_head_sha": "d" * 40}
    assert module._review_identity(raw, body, "r-abc.json") == (
        "run",
        "r-abc",
        derived,
        True,
    )
    # The ``.at-<head>`` sibling names the same reviewed run.
    assert module._review_identity(raw, body, f"r-abc.at-{'e' * 40}.json") == (
        "run",
        "r-abc",
        derived,
        True,
    )
    # An explicit review run id in the body is kept even when the subject comes
    # from the filename.
    assert module._review_identity(
        raw, dict(body, review_run_id="r-review"), "r-abc.json"
    ) == ("run", "r-abc", "r-review", False)
    # Without a filename and without a body subject there is nothing to file it
    # under; a name that is not a JSON file names nothing.
    assert module._review_identity(raw, body) is None
    assert module._review_identity(raw, body, "r-abc.txt") is None
    assert module._review_identity(raw, body, ".json") is None
    # The filename helper reads the run id out of both store spellings.
    assert module._filename_run_subject("r-abc.json") == "r-abc"
    assert module._filename_run_subject(f"r-abc.at-{'e' * 40}.json") == "r-abc"
    assert module._filename_run_subject("notes.txt") is None


def test_write_imports_a_filename_named_run_review(harness) -> None:
    module = _load_script()
    store = harness["store"]
    repo = harness["repo"]
    # A run review whose body names the reviewed run only in its filename: the
    # store would quarantine it before the subject falls back to the name.
    source = _write_record(
        store,
        "r-fn-run.json",
        {
            "project": PROJECT,
            "status": "parsed",
            "findings": [],
            "call_sites": [],
            "reviewed_head_sha": "f" * 40,
        },
    )
    _run_record(repo, "r-fn-run")

    code, out = _run_cli(module, ["--project", PROJECT, "--root", str(repo), "--write"])

    assert code == 0
    assert "imported: 1" in out
    expected = "legacy-" + hashlib.sha256(source.read_bytes()).hexdigest()[:12]
    committed = repo / "docs" / "state" / PROJECT / "reviews" / "run" / "r-fn-run"
    stored = json.loads((committed / f"{expected}.json").read_text(encoding="utf-8"))
    # The reviewed run recovered from the filename is written into the body so
    # the committed file is filed and readable under the run it reviewed.
    assert stored["reviewed_run_id"] == "r-fn-run"
    assert stored["review_run_id"] == expected
    assert stored["review_run_id_source"] == "derived"
    # The staging file is a record, so it stays in place.
    assert source.is_file()
