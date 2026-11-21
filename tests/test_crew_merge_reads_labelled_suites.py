"""A manifest whose suite arms are labelled merges as an unlabelled pair does.

A run armed on more than one population records each population's arms apart:
``baseline_suite_UNIT`` beside ``after_suite_UNIT``, ``baseline_suite_VALIDATION``
beside ``after_suite_VALIDATION``. ``crew merge`` read only the unlabelled field
names, found no pair, and refused a promoted, passing run for the missing
top-level fields. Each label's pair is judged on its own: a clean set of pairs
merges, a pair that adds a failure is refused naming the label and the id, and a
label missing a side is refused naming the missing side.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from reckon import ledger
from reckon.cli import main
from reckon.crew.promotion_records import _evaluate_suite_delta
from reckon.crew.runs import _write_json, pointer_path
from tests.helpers.git import git
from tests.helpers.landing_repository import _write_origin_and_checkout


@pytest.fixture(autouse=True)
def _no_landing_measurement(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the landing's own measurement unavailable, as for a run whose trees
    cannot be named.

    The landing measures a superseded or labelled arm set itself when it can.
    A run whose arms it cannot measure is left with the refusal its manifest
    produces, which is what these cases judge: without the measurement the
    labelled pair is the only evidence, so an unsupported manifest is refused
    with the missing-field refusal rather than quietly re-measured.
    """
    from reckon.crew import promotion_records

    monkeypatch.setattr(promotion_records, "_measured_arm_tree", lambda _record: None)


def _observation(
    revision: str,
    command: str = "true",
    *,
    failure_ids: list[str] | None = None,
    log_path: str = "/durable/suite.log",
) -> dict[str, object]:
    ids = list(failure_ids or [])
    return {
        "revision": revision,
        "command": command,
        "exit_status": 0,
        "log_path": log_path,
        "completed": True,
        "failure_count": len(ids),
        "failure_ids": ids,
    }


def _write_manifest(path, arms: dict[str, object], *, commits: str = "abc123") -> None:
    lines = ["node: labelled", "status: complete", f"commits: {commits}"]
    for name, value in arms.items():
        lines.append(f"{name}: {json.dumps(value)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _record(tmp_path, arms, suite_command: str = "true") -> dict[str, object]:
    manifest = tmp_path / "manifest.md"
    _write_manifest(manifest, arms)
    return {
        "run_id": "r-labelled-suites",
        "suite_command": suite_command,
        "manifest_path": str(manifest),
    }


def _two_clean_pairs(revision: str, log: str) -> dict[str, object]:
    return {
        "baseline_suite_UNIT": _observation(revision, log_path=log),
        "after_suite_UNIT": _observation(revision, log_path=log),
        "baseline_suite_VALIDATION": _observation(revision, log_path=log),
        "after_suite_VALIDATION": _observation(revision, log_path=log),
    }


def test_two_clean_labelled_pairs_merge(tmp_path, monkeypatch):
    """A manifest carrying two labelled pairs, each adding nothing, merges."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    origin, repository = _write_origin_and_checkout(tmp_path)
    (home / "mounts.json").write_text(json.dumps({"proj": str(repository / "docs")}))
    worktree = tmp_path / "worktree"
    git(repository, "worktree", "add", "--detach", str(worktree), "HEAD")
    (worktree / "change.txt").write_text("introduced by run\n")
    git(worktree, "add", "change.txt")
    git(
        worktree,
        "commit",
        "-q",
        "-m",
        "fix: record a change",
        "-m",
        "Give the merge a visible marker.",
    )
    head = git(worktree, "rev-parse", "HEAD")
    base = git(repository, "rev-parse", "HEAD")
    run_id = "r-20261121T000000000000-labelled-suites"
    gate_log = tmp_path / "gate.log"
    gate_log.write_text("# command: true\nEXIT=0\n")
    suite_log = tmp_path / "suite.log"
    suite_log.write_text("# command: true\nEXIT=0\n")
    manifest = tmp_path / "manifest.md"
    _write_manifest(manifest, _two_clean_pairs(head, str(suite_log)), commits=head)
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": "proj",
            "repo": str(repository),
            "worktree": str(worktree),
            "base_sha": base,
            "launch": "in-harness",
            "role": "implement",
            "member": "worker-a",
            "backend": "native",
            "created_at": "2026-11-21T00:00:00Z",
            "manifest_path": str(manifest),
            "suite_command": "true",
            "node": {
                "id": "labelled-suites",
                "plan": "plan-a",
                "section": "merge-guard",
                "write_paths": ["change.txt"],
            },
        },
    )
    common = [
        "--run",
        run_id,
        "--gate",
        "passed",
        "--commit",
        head,
        "--no-impl-change",
        "the fixture moves no plan",
        "--gate-command",
        "true",
        "--gate-exit-status",
        "0",
        "--gate-log-path",
        str(gate_log),
    ]
    dry = CliRunner().invoke(main, ["crew", "complete", "--dry-run", *common])
    assert dry.exit_code == 0, dry.output
    assert json.loads(dry.output)["would_promote"] is True, dry.output
    merged = CliRunner().invoke(
        main,
        [
            "crew",
            "merge",
            "--project",
            "proj",
            "--head",
            head,
            *common,
            "--message",
            "fix: land the change",
            "--body",
            "Carry the reviewed change onto main.",
        ],
    )
    assert merged.exit_code == 0, merged.output
    assert "introduced by run" in git(repository, "show", "HEAD:change.txt")
    assert git(origin, "rev-parse", "refs/heads/main") == git(
        repository, "rev-parse", "HEAD"
    )


def test_a_clean_labelled_pair_is_a_clean_delta(tmp_path):
    """Both labelled pairs are judged, and each reports its own delta."""
    revision = "a" * 40
    record = _record(tmp_path, _two_clean_pairs(revision, "/durable/suite.log"))

    delta = _evaluate_suite_delta(
        "r-labelled-suites", record, waiver_reason="", dry_run=True
    )

    assert delta is not None
    assert delta["status"] == "clean"
    labels = sorted(pair["label"] for pair in delta["suite_pairs"])
    assert labels == ["unit", "validation"]
    assert all(pair["added_failure_ids"] == [] for pair in delta["suite_pairs"])


def test_a_labelled_pair_adding_a_failure_is_refused_naming_label_and_id(tmp_path):
    """A second label's added failure is refused naming that label and the id."""
    revision = "a" * 40
    added = "tests/test_unit.py::test_new_behaviour"
    arms = {
        "baseline_suite_UNIT": _observation(revision),
    }
    arms["after_suite_UNIT"] = _observation(revision)
    arms["baseline_suite_VALIDATION"] = _observation(revision)
    arms["after_suite_VALIDATION"] = _observation(revision, failure_ids=[added])
    record = _record(tmp_path, arms)

    with pytest.raises(ledger.SuiteDeltaError) as raised:
        _evaluate_suite_delta(
            "r-labelled-suites", record, waiver_reason="", dry_run=True
        )

    message = str(raised.value).lower()
    assert "validation" in message
    assert added in message
    assert added in raised.value.added_failure_ids


def test_a_label_missing_its_after_arm_is_refused_naming_the_missing_side(tmp_path):
    """A label carrying only a baseline is refused naming the absent side."""
    revision = "a" * 40
    arms = {"baseline_suite_UNIT": _observation(revision)}
    record = _record(tmp_path, arms)

    with pytest.raises(ledger.SuiteDeltaError) as raised:
        _evaluate_suite_delta(
            "r-labelled-suites", record, waiver_reason="", dry_run=True
        )

    assert raised.value.missing_fields == ["after_suite_unit"]
    assert "after_suite_unit" in str(raised.value).lower()
