"""Promotion preserves the client receipt as durable lane evidence."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import _plan_html, crew, ledger
from reckon.crew import rollout
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "receipt-project"
PLAN = "receipt-plan"
OBSERVED_AT = "2030-01-02T03:04:05Z"


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setattr(rollout, "CLIENT_SESSIONS_DIR", tmp_path / "sessions")

    root = tmp_path / "repo"
    state_dir = root / "docs" / "state" / PROJECT
    state_dir.mkdir(parents=True)
    plan_path = root / "docs" / "plans" / f"{PLAN}.html"
    plan_path.parent.mkdir(parents=True)
    plan = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    plan_path.write_text(
        _plan_html.write_state(
            plan,
            {
                "type": "plan",
                "slug": PLAN,
                "title": "Receipt plan",
                "status": "active",
                "version": 0,
                "comments": {},
            },
        ),
        encoding="utf-8",
    )
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs"),
        ("commit", "-q", "-m", "test: seed repository"),
    ):
        _git(root, *arguments)
    return root


def _write_receipt(
    session_root: Path,
    session_id: str,
    *,
    context_window: int,
    windows: list[tuple[int, int, int]],
) -> None:
    directory = session_root / "2030" / "01" / "02"
    directory.mkdir(parents=True, exist_ok=True)
    quota_names = ("primary", "secondary")
    rate_limits = {
        name: {
            "window_minutes": window_minutes,
            "used_percent": used_percent,
            "resets_at": resets_at,
        }
        for name, (window_minutes, used_percent, resets_at) in zip(
            quota_names[: len(windows)], windows, strict=True
        )
    }
    record = {
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": 10,
                    "cached_input_tokens": 2,
                    "output_tokens": 1,
                },
                "last_token_usage": {"input_tokens": 8, "output_tokens": 1},
                "model_context_window": context_window,
            },
            "rate_limits": rate_limits,
        },
    }
    path = directory / f"rollout-2030-01-02T00-00-00-{session_id}.jsonl"
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")


def _write_pointer(
    repository: Path,
    run_id: str,
    session_id: str,
    *,
    backend: str,
) -> None:
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": _git(repository, "rev-parse", "HEAD"),
            "launch": "in-harness",
            "role": "implement",
            "backend": backend,
            "session_id": session_id,
            "created_at": "2030-01-02T03:00:00Z",
            "node": {
                "id": f"receipt-{run_id}",
                "plan": PLAN,
                "section": "receipt",
                "time_budget": "20m",
                "write_paths": [],
            },
        },
    )


def _promote(
    repository: Path,
    run_id: str,
    session_id: str,
    *,
    backend: str,
) -> dict:
    _write_pointer(repository, run_id, session_id, backend=backend)
    return crew.complete(
        run_id,
        gate="passed",
        completed_at=OBSERVED_AT,
        root=repository,
    )


def _stored_row(repository: Path, run_id: str) -> dict:
    ledger_path = repository / "docs" / "state" / PROJECT / "crew.json"
    data = json.loads(ledger_path.read_text(encoding="utf-8"))
    rows = [row for row in data["data"]["runs"] if row["run_id"] == run_id]
    assert len(rows) == 1
    return rows[0]


def _windows_by_length(receipt: dict) -> dict[int, dict]:
    return {row["window_minutes"]: row for row in receipt["quota_windows"]}


def test_every_quota_horizon_reaches_the_result_and_the_ledger(
    repository: Path,
) -> None:
    assert "lane_receipt" in ledger.RECORD_FIELDS
    session_id = "two-horizons"
    short_window = 5 * 60
    weekly_window = 7 * 24 * 60
    short_used = 73
    weekly_used = 41
    context_window = 121_600
    _write_receipt(
        rollout.CLIENT_SESSIONS_DIR,
        session_id,
        context_window=context_window,
        windows=[
            (short_window, short_used, 1_900_000_300),
            (weekly_window, weekly_used, 1_900_010_080),
        ],
    )

    result = _promote(
        repository,
        "r-two-horizons",
        session_id,
        backend="metered",
    )
    stored = _stored_row(repository, "r-two-horizons")

    returned = result["lane_receipt"]
    assert returned == result["record"]["lane_receipt"]
    assert stored["lane_receipt"] == returned
    assert stored is not result["record"]
    assert returned["effective_context_window"] == context_window
    windows = _windows_by_length(returned)
    assert set(windows) == {short_window, weekly_window}
    assert windows[short_window]["used_percent"] == short_used
    assert windows[weekly_window]["used_percent"] == weekly_used
    assert windows[weekly_window]["used_percent"] != short_used
    assert windows[short_window]["resets_at"] == 1_900_000_300
    assert windows[weekly_window]["resets_at"] == 1_900_010_080
    assert all(row["observed_at"] == OBSERVED_AT for row in windows.values())


def test_unmetered_is_not_the_same_reading_as_measured_zero(
    repository: Path,
) -> None:
    zero_session = "measured-zero"
    unmetered_session = "unmetered-zero"
    window = 5 * 60
    receipt_root = rollout.CLIENT_SESSIONS_DIR
    for session_id in (zero_session, unmetered_session):
        _write_receipt(
            receipt_root,
            session_id,
            context_window=121_600,
            windows=[(window, 0, 1_900_000_300)],
        )

    measured = _promote(
        repository,
        "r-measured-zero",
        zero_session,
        backend="metered",
    )["lane_receipt"]
    unmetered = _promote(
        repository,
        "r-unmetered-zero",
        unmetered_session,
        backend="clive",
    )["lane_receipt"]

    assert measured["quota_state"] == "measured"
    assert _windows_by_length(measured)[window]["used_percent"] == 0
    assert unmetered["quota_state"] == "unmeasured"
    assert unmetered["quota_windows"] == []
    assert unmetered["unmeasured"]["quota_windows"] == "unmetered"
    assert unmetered != measured


def test_an_absent_receipt_is_unmeasured_and_promotion_still_succeeds(
    repository: Path,
) -> None:
    result = _promote(
        repository,
        "r-missing-receipt",
        "no-such-receipt",
        backend="metered",
    )
    stored = _stored_row(repository, "r-missing-receipt")
    receipt = result["lane_receipt"]

    assert result["pointer_removed"] is True
    assert result["record"]["gate"] == "passed"
    assert stored["gate"] == "passed"
    assert stored["lane_receipt"] == receipt
    assert receipt["quota_state"] == "unmeasured"
    assert receipt["quota_windows"] == []
    assert receipt["effective_context_window"] == "unmeasured"
    assert receipt["unmeasured"]["quota_windows"] == "missing_rollout"
    assert receipt["unmeasured"]["effective_context_window"] == "missing_rollout"
    assert receipt["observed_at"] == OBSERVED_AT
