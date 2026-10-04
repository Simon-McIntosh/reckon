"""A deliberate orchestrator-lane dispatch carries one run's stated reason."""

from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew, ledger
from tests import test_dispatch_names_its_backend as backend_tests
from tests import test_ledger as ledger_tests

pytest_plugins = (
    "tests.test_dispatch_names_its_backend",
    "tests.test_ledger",
)


def _cli_dispatch(
    repo: Path, monkeypatch: pytest.MonkeyPatch, *, node: str, extra=(), dry_run=True
):
    config = deepcopy(backend_tests.CONFIG)
    config["backends"]["clive"]["serves_orchestrators"] = True
    monkeypatch.setattr(cli_module, "_resolved_flight", lambda *_a, **_k: config)
    monkeypatch.setattr(
        cli_module, "_model_availability_refusal", lambda *_a, **_k: None
    )
    result = CliRunner().invoke(
        cli_module.main,
        [
            *backend_tests._arguments(repo, node=node, dry_run=dry_run),
            "--backend",
            "clive",
            *extra,
        ],
    )
    return backend_tests._payload(result), result


def test_reason_flag_applies_to_one_dispatch_only(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before, before_result = _cli_dispatch(dispatch_repo, monkeypatch, node="before")
    assert before_result.exit_code == 0
    assert before["orchestrator_lane_stop"]["state"] == "declared"
    assert before["orchestrator_lane_override"] is None

    reason = "recover a time-critical review while the local lane is unavailable"
    flagged, flagged_result = _cli_dispatch(
        dispatch_repo,
        monkeypatch,
        node="flagged",
        extra=("--allow-orchestrator-lane", reason),
    )
    assert flagged_result.exit_code == 0
    assert flagged["orchestrator_lane_stop"]["state"] == "overridden"
    assert flagged["orchestrator_lane_override"] == {"lane": "clive", "reason": reason}

    after, after_result = _cli_dispatch(dispatch_repo, monkeypatch, node="after")
    assert after_result.exit_code == 0
    assert after["orchestrator_lane_stop"]["state"] == "declared"
    assert after["orchestrator_lane_override"] is None

    launched, launch_result = _cli_dispatch(
        dispatch_repo,
        monkeypatch,
        node="launched",
        extra=("--allow-orchestrator-lane", reason, "--no-watch"),
        dry_run=False,
    )
    assert launch_result.exit_code == 0
    assert crew.read_pointer(launched["run_id"])["orchestrator_lane_override"] == {
        "lane": "clive",
        "reason": reason,
    }


def test_empty_reason_is_refused(
    dispatch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload, result = _cli_dispatch(
        dispatch_repo,
        monkeypatch,
        node="empty-reason",
        extra=("--allow-orchestrator-lane", " "),
    )
    assert result.exit_code == 1
    assert payload["error"] == "dispatch-refused"
    assert "non-empty reason" in payload["detail"]


def test_reason_reaches_the_promoted_ledger_row(home: Path, repo: Path) -> None:
    config = deepcopy(ledger_tests.CONFIG)
    config["backends"]["alpha"]["serves_orchestrators"] = True
    reason = "restore a blocked review"
    record = crew.dispatch(
        node=ledger_tests._node(id="lane-audit"),
        project=ledger_tests.PROJECT,
        repo=repo,
        config=config,
        session="lane-audit",
        launcher=lambda plan, *, log_path, stderr_path, prompt_path: os.getpid(),
        orchestrator_lane_reason=reason,
    )
    Path(record["log_path"]).write_text(
        (ledger_tests.FIXTURES / "codex-turn.jsonl").read_text()
    )
    ledger_tests._deliver(record)
    work = ledger_tests._commit_work(repo)
    ledger_tests._init_ledger(repo)
    promoted = crew.complete(
        record["run_id"],
        gate="passed",
        commits=[work],
        review_waiver=ledger_tests._UNREVIEWED_PROMOTION_WAIVED,
    )["record"]
    expected = {"lane": "alpha", "reason": reason}
    assert promoted["orchestrator_lane_override"] == expected
    assert (
        ledger.runs(ledger_tests.PROJECT, repo)[0]["orchestrator_lane_override"]
        == expected
    )
