"""A delivered review is checked and can be repaired without changing its verdict."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from reckon.cli import main
from reckon.crew import obligations as obligations_module
from reckon.crew import review, runs
from reckon.crew.recovery_review_subject import _review_brief_text

PROJECT = "review-contract-fixture"
REVIEWED_RUN = "r-reviewed-fixture"
REVIEW_RUN = "r-review-fixture"


@pytest.fixture()
def delivery(isolated_reckon_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep every pointer and review write in the suite's temporary home."""
    assert runs.crew_home().is_relative_to(isolated_reckon_home)
    monkeypatch.setattr(review, "unresolved_reviewed_revision", lambda _body: None)
    source = Path(
        review.run_review_record_paths(runs.run_dir(REVIEWED_RUN), REVIEWED_RUN)[0]
    )
    source.parent.mkdir(parents=True)
    runs._write_json(
        runs.pointer_path(REVIEWED_RUN),
        {
            "run_id": REVIEWED_RUN,
            "project": PROJECT,
            "role": "implement",
        },
    )
    runs._write_json(
        runs.pointer_path(REVIEW_RUN),
        {
            "run_id": REVIEW_RUN,
            "project": PROJECT,
            "role": "review",
            "repo": str(source.parent.parent),
            "worktree": str(source.parent.parent),
            "manifest_path": str(runs.run_dir(REVIEW_RUN) / "manifest.md"),
            "node": {
                "id": "review-of-fixture",
                "role": "review",
                "write_paths": [str(source)],
            },
        },
    )
    return source


def _record(**changes: object) -> dict:
    body = {
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN,
        "review_run_id": REVIEW_RUN,
        "reviewed_base_sha": "a" * 40,
        "reviewed_head_sha": "b" * 40,
        "scores": dict.fromkeys(review.REVIEW_DIMENSIONS, 17),
        "added_failure_count": 0,
        "added_failure_ids": [],
        "findings": [],
        "status": "parsed",
    }
    body.update(changes)
    return body


def _invoke(*args: str):
    return CliRunner().invoke(main, ["crew", *args])


def _raw_field(raw: bytes, key: str, following: str) -> bytes:
    return raw.split(f'"{key}"'.encode(), 1)[1].split(f'"{following}"'.encode(), 1)[0]


def test_check_review_names_unfileable_fields_and_accepts_a_valid_record(
    delivery: Path,
) -> None:
    for changed, field in (
        ({"status": "complete"}, "status"),
        ({"reviewed_run_id": ""}, "reviewed_run_id"),
    ):
        delivery.write_text(json.dumps(_record(**changed)), encoding="utf-8")
        result = _invoke("check", "--review", "--record", str(delivery))
        assert result.exit_code == 1, result.output
        assert field in result.output
        if field == "status":
            assert all(
                value in result.output for value in review.RECOGNISED_REVIEW_STATUSES
            )
    delivery.write_text(json.dumps(_record()), encoding="utf-8")
    result = _invoke("check", "--review", "--record", str(delivery))
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["findings"] == []
    assert not review.review_path(PROJECT, REVIEWED_RUN).exists()
    filed = review.file_delivered_run_reviews(
        runs.read_pointer(REVIEW_RUN), run_directory=runs.run_dir(REVIEWED_RUN)
    )
    assert len(filed) == 1 and not filed.refused
    assert filed[0].is_file()


def test_store_refusal_matches_whitespace_only_reviewed_run_id() -> None:
    body = _record(reviewed_run_id="   ")
    assert any("no subject" in reason for reason in review.store_review_refusals(body))
    with pytest.raises(ValueError, match="no subject"):
        review.store_review(body)


@pytest.mark.parametrize(
    "changed,field",
    [({"status": "complete"}, "status"), ({"reviewed_run_id": ""}, "reviewed_run_id")],
)
def test_review_manifest_audit_refuses_unfileable_delivery(
    delivery: Path, changed: dict, field: str
) -> None:
    delivery.write_text(json.dumps(_record(**changed)), encoding="utf-8")
    manifest = runs.run_dir(REVIEW_RUN) / "manifest.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: review-of-fixture\nstatus: complete\ncommits: none\nchanged_paths: []\ntests: review record checked\n",
        encoding="utf-8",
    )
    result = _invoke("check", "--manifest", "--run", REVIEW_RUN)
    assert result.exit_code == 1, result.output
    assert any(field in finding for finding in json.loads(result.output)["findings"])


def test_review_brief_reads_the_contract_vocabulary() -> None:
    brief = _review_brief_text({"run_id": REVIEWED_RUN, "node": {}}, "b" * 40)
    assert "crew check --review --record" in brief
    for key in (
        "project",
        "reviewed_run_id",
        "review_run_id",
        "reviewed_base_sha",
        "reviewed_head_sha",
        "added_failure_count",
        "added_failure_ids",
        "findings",
        "status",
    ):
        assert key in brief
    for value in review.RECOGNISED_REVIEW_STATUSES:
        assert value in brief


def test_delivery_reports_refusal_and_status_repair_files_without_changing_review(
    delivery: Path,
) -> None:
    original = _record(status="complete")
    delivery.write_text(json.dumps(original, indent=4), encoding="utf-8")
    delivered_bytes = delivery.read_bytes()
    reviewer = runs.read_pointer(REVIEW_RUN)
    filed = review.file_delivered_run_reviews(
        reviewer, run_directory=runs.run_dir(REVIEWED_RUN)
    )
    assert not filed
    assert "status 'complete'" in filed.refused[0]["reason"]

    result = _invoke(
        "repair",
        "--review",
        "--run",
        REVIEWED_RUN,
        "--record",
        str(delivery),
        "--set",
        "status=parsed",
        "--reason",
        "parsed rubric verified",
    )
    assert result.exit_code == 0, result.output
    receipt = json.loads(result.output)
    preserved = Path(receipt["as_delivered"])
    assert preserved.read_bytes() == json.dumps(original, indent=4).encode()
    repaired = json.loads(delivery.read_text(encoding="utf-8"))
    repaired_bytes = delivery.read_bytes()
    for key in ("scores", "findings"):
        assert (
            json.dumps(repaired[key], sort_keys=True).encode()
            == json.dumps(original[key], sort_keys=True).encode()
        )
    assert _raw_field(repaired_bytes, "scores", "added_failure_count") == _raw_field(
        delivered_bytes, "scores", "added_failure_count"
    )
    assert _raw_field(repaired_bytes, "findings", "status") == _raw_field(
        delivered_bytes, "findings", "status"
    )
    assert repaired["repair_reason"] == "parsed rubric verified"
    assert Path(receipt["filed"]).is_file()


def test_subject_repair_fills_only_missing_identity_and_rejects_other_sets(
    delivery: Path,
) -> None:
    body = _record(project="", reviewed_run_id="", review_run_id="")
    delivery.write_text(json.dumps(body), encoding="utf-8")
    delivered_bytes = delivery.read_bytes()
    refused = _invoke(
        "repair",
        "--review",
        "--run",
        REVIEWED_RUN,
        "--record",
        str(delivery),
        "--set",
        "scores=20",
        "--reason",
        "invalid",
    )
    assert refused.exit_code != 0
    assert "status" in refused.output and "subject" in refused.output
    result = _invoke(
        "repair",
        "--review",
        "--run",
        REVIEWED_RUN,
        "--record",
        str(delivery),
        "--set",
        "subject",
        "--reason",
        "matched run records",
    )
    assert result.exit_code == 0, result.output
    repaired = json.loads(delivery.read_text(encoding="utf-8"))
    repaired_bytes = delivery.read_bytes()
    assert (
        repaired["project"],
        repaired["reviewed_run_id"],
        repaired["review_run_id"],
    ) == (PROJECT, REVIEWED_RUN, REVIEW_RUN)
    for key in ("scores", "findings"):
        assert (
            json.dumps(repaired[key], sort_keys=True).encode()
            == json.dumps(body[key], sort_keys=True).encode()
        )
    assert _raw_field(repaired_bytes, "scores", "added_failure_count") == _raw_field(
        delivered_bytes, "scores", "added_failure_count"
    )
    assert _raw_field(repaired_bytes, "findings", "status") == _raw_field(
        delivered_bytes, "findings", "status"
    )


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"status": "complete"}, "status 'complete'"),
        ({"reviewed_run_id": "   "}, "no subject"),
    ],
)
def test_complete_reports_a_refused_review_before_promotion(
    delivery: Path, change: dict, reason: str
) -> None:
    delivery.write_text(json.dumps(_record(**change)), encoding="utf-8")
    result = _invoke("complete", "--run", REVIEW_RUN, "--gate", "not-run")
    assert result.exit_code != 0, result.output
    receipt = json.loads(result.output)
    assert receipt["review_filed"] is False
    assert reason in receipt["refused"][0]["reasons"][0]


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"status": "complete"}, "status 'complete'"),
        ({"reviewed_run_id": "   "}, "no subject"),
    ],
)
def test_obligation_names_a_delivered_refusal_and_its_repair(
    delivery: Path, monkeypatch: pytest.MonkeyPatch, change: dict, reason: str
) -> None:
    delivery.write_text(json.dumps(_record(**change)), encoding="utf-8")
    monkeypatch.setattr(
        obligations_module.recovery, "_review_in_flight", lambda _pointer: REVIEW_RUN
    )
    monkeypatch.setattr(
        obligations_module.recovery, "_is_review_run", lambda _pointer: False
    )
    monkeypatch.setattr(
        obligations_module.recovery,
        "_review_head_and_tree",
        lambda _pointer: ("b" * 40, None),
    )
    monkeypatch.setattr(
        obligations_module.recovery,
        "select_review_for_head",
        lambda *_args, **_kwargs: (None, ""),
    )
    item = obligations_module._review_duty_item(
        PROJECT,
        {"run_id": REVIEWED_RUN, "classification": "scoring"},
        runs.read_pointer(REVIEWED_RUN),
        now=datetime.now(UTC),
        grace=900,
    )
    assert item["kind"] == "review-refused"
    assert reason in item["reason"]
    assert "crew repair --review" in item["next_command"]
    assert "redispatch" not in item["next_command"]
    monkeypatch.setattr(
        obligations_module.flight,
        "resolve",
        lambda _project: SimpleNamespace(config={}),
    )
    monkeypatch.setattr(
        obligations_module.runs,
        "list_live",
        lambda **_kwargs: [
            runs.read_pointer(REVIEWED_RUN),
            runs.read_pointer(REVIEW_RUN),
        ],
    )
    monkeypatch.setattr(
        obligations_module,
        "_classified_rows",
        lambda _project: [
            {
                "run_id": REVIEWED_RUN,
                "session": "session-fixture",
                "classification": "scoring",
            }
        ],
    )
    monkeypatch.setattr(
        obligations_module, "_live_review_runs", lambda *_args: {REVIEWED_RUN}
    )
    monkeypatch.setattr(
        obligations_module, "_sub_floor_items", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        obligations_module, "_held_worktrees", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        obligations_module, "_acknowledgements_in_force", lambda *_args, **_kwargs: {}
    )
    monkeypatch.setattr(
        obligations_module.runs,
        "drain",
        lambda *_args, **_kwargs: {"unreconciled_runs": 1},
    )
    report = obligations_module.obligations(PROJECT, "session-fixture")
    assert report["obligations"][0]["kind"] == "review-refused"
    assert "crew repair --review" in report["obligations"][0]["next_command"]
