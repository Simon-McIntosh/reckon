"""Every budget reading's stale flag is judged by one source-aware rule.

A candidate and the return-time block describe the same reading, so they must
agree about whether it still describes the present. One function,
``lane_context._budget_block``, owns that judgment and both surfaces derive
through it: an account-surface reading is never stale by age, a paid-lanes
document reading is stale past the shelf life the document publishes (the
flight's shelf life when it publishes none), and a ledger reading is stale past
the flight's shelf life.
"""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime, timedelta

import pytest

from reckon import budget, ledger
from reckon.crew.node import TaskNode
from reckon.crew.picker import lane_context, prompts, snapshot
from reckon.crew.picker.types import PickRequest

FLIGHT_MINUTES = 5.0


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Point every crew-home-resolved path at a fresh directory."""

    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))


@pytest.fixture
def config():
    return {
        "default_backend": "codex",
        "local_backend": "codex",
        "backends": {
            "codex": {
                "launch": "cli",
                "command": "codex",
                "budget_group": "codex-sub",
                "model": "m",
            }
        },
        "roles": {"implement": {}},
        "budget": {
            "utilisation_ceiling_pct": 100,
            "reserve_reserve_pct": 5,
            "resume_reserve_pct": 5,
            "coordinator_reserve_pct": 3,
            "bookend_reserve_pct": 20,
            "pace_multiple": 1.1,
            "evidence_shelf_life_minutes": FLIGHT_MINUTES,
        },
        "review_excluded_backends": [],
    }


def _node() -> TaskNode:
    return TaskNode(
        id="one-staleness-rule",
        goal="judge one reading one way",
        plan="jev-learns-from-its-routes",
        role="implement",
        spec_level="guided",
        done_when="the flags agree",
        time_budget="45m",
    )


def _candidate_and_return_time(report, config, repo, moment):
    """Both surfaces' view of one composed report, for the codex backend."""

    request = PickRequest(project="reckon", node=_node(), session="s")
    candidates = snapshot.candidates(
        request,
        config,
        repo,
        records=[],
        budget_snapshot=report,
        verdict_inputs={},
        cached_only=True,
    )
    candidate = next(item for item in candidates if item.backend == "codex")
    blocks = lane_context.return_times(
        {},
        request.node,
        candidates,
        budget_snapshot=report,
        config=config,
        now=moment,
    )
    return candidate, blocks["codex"]


def _document(now: datetime, *, age_seconds: float, shelf_seconds: int = 900) -> dict:
    """A paid-lanes document whose codex account was read ``age_seconds`` ago."""

    observed = now - timedelta(seconds=age_seconds)

    def window(period: str, utilisation: float, reset: datetime) -> dict:
        return {
            "period": period,
            "state": budget.OBSERVED,
            "utilisation": utilisation,
            "resets_at": reset.isoformat(),
            "source": "rollout-receipt",
            "observed_at": observed.isoformat(),
            "age_seconds": age_seconds,
            "stale": False,
        }

    return {
        "document": "paid-lanes",
        "generated_by": "reckon.crew.paid_lanes",
        "observed_at": now.isoformat(),
        "suggested_shelf_life_seconds": shelf_seconds,
        "accounts": {
            "codex": {
                "account": "codex",
                "state": budget.OBSERVED,
                "source": "rollout-receipt",
                "observed_at": observed.isoformat(),
                "windows": {
                    "five_hour": window("five_hour", 0.4, now + timedelta(hours=3)),
                    "seven_day": window("seven_day", 0.3, now + timedelta(days=4)),
                },
            }
        },
        "local_lane": {"state": budget.UNKNOWN},
    }


def _compose(config, repo, moment, monkeypatch, *, document=None, records=()):
    """Compose a budget report with the candidate-only stubs a pick needs."""

    if document is not None:
        monkeypatch.setattr(
            snapshot.paid_lanes, "read_document", lambda *a, **k: document
        )
    monkeypatch.setattr(snapshot, "_fit", lambda *a, **k: [])
    monkeypatch.setattr(snapshot, "estimated_context_tokens", lambda *a, **k: 0)
    return snapshot._compose_budget_report(
        "reckon",
        config,
        repo,
        list(records),
        moment,
        cached_only=True,
        session="",
    )


def _receipt_row(now: datetime, *, age_seconds: float) -> dict:
    """A committed ledger row carrying a codex lane receipt observed ``age_seconds`` ago."""

    observed = now - timedelta(seconds=age_seconds)
    return ledger.build_record(
        run_id="receipt",
        plan="reckon",
        gate="passed",
        backend="codex",
        completed_at=observed.isoformat(),
        lane_receipt={
            "quota_state": "measured",
            "observed_at": observed.isoformat(),
            "quota_windows": [
                {
                    "window_minutes": 10080,
                    "used_percent": 80.0,
                    "resets_at": (now + timedelta(days=6)).isoformat(),
                    "observed_at": observed.isoformat(),
                }
            ],
        },
    )


def test_the_rule_judges_each_source_by_its_own_bound():
    """The one function's four cases, read directly from its own bounds."""

    now = datetime.now(UTC)

    def block(source: str, age: float, *, document_backed=False, shelf=None):
        return lane_context._budget_block(
            {
                "source": source,
                "observed_at": (now - timedelta(seconds=age)).isoformat(),
                "suggested_shelf_life_seconds": shelf,
            },
            moment=now,
            shelf_life_minutes=FLIGHT_MINUTES,
            document_backed=document_backed,
        )

    fresh = block("document", 345, document_backed=True, shelf=900)
    assert fresh["budget_source"] == "document"
    assert fresh["budget_age_s"] == pytest.approx(345, abs=2)
    assert fresh["stale"] is False
    assert block("document", 1000, document_backed=True, shelf=900)["stale"] is True
    # No published shelf life: the flight's shelf life governs the document reading.
    assert block("document", 345, document_backed=True, shelf=None)["stale"] is True
    assert block("ledger", 400)["stale"] is True
    assert block("ledger", 200)["stale"] is False
    assert block("account-surface", 10**7)["stale"] is False


def test_a_document_reading_beyond_the_flight_shelf_reads_fresh_on_both(
    tmp_path, config, monkeypatch
):
    """345 s old against a 900 s published shelf, under a 5 min flight shelf."""

    now = datetime.now(UTC)
    document = _document(now, age_seconds=345, shelf_seconds=900)
    report = _compose(config, tmp_path, now, monkeypatch, document=document)

    candidate, block = _candidate_and_return_time(report, config, tmp_path, now)

    assert candidate.stale is False
    assert candidate.budget_source == "document"
    assert block["stale"] is False
    assert block["budget_source"] == "document"


def test_a_document_reading_past_its_own_shelf_reads_stale_on_both(
    tmp_path, config, monkeypatch
):
    """The same reading at 1,000 s is past the document's 900 s shelf."""

    now = datetime.now(UTC)
    document = _document(now, age_seconds=1000, shelf_seconds=900)
    report = _compose(config, tmp_path, now, monkeypatch, document=document)

    candidate, block = _candidate_and_return_time(report, config, tmp_path, now)

    assert candidate.stale is True
    assert candidate.budget_source == "document"
    assert block["stale"] is True
    assert block["budget_source"] == "document"


def test_a_ledger_reading_past_the_flight_shelf_reads_stale_on_both(
    tmp_path, config, monkeypatch
):
    """A recorded lane receipt 400 s old is past the 5 min flight shelf."""

    now = datetime.now(UTC)
    report = _compose(
        config, tmp_path, now, monkeypatch, records=[_receipt_row(now, age_seconds=400)]
    )

    candidate, block = _candidate_and_return_time(report, config, tmp_path, now)

    assert candidate.stale is True
    assert candidate.budget_source == "ledger"
    assert block["stale"] is True
    assert block["budget_source"] == "ledger"


def test_an_account_surface_reading_is_never_stale_by_age_on_both(
    tmp_path, config, monkeypatch
):
    """Two hours old, past the flight shelf, and still fresh because it describes now."""

    now = datetime.now(UTC)
    observed = now - timedelta(hours=2)
    crafted = {
        "checked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "backends": [
            {
                "backend": "codex",
                "held": False,
                "state": {
                    "headroom": "known",
                    "source": "account-surface",
                    "observed_at": observed.isoformat(),
                },
            }
        ],
        "groups": [
            {
                "group": "codex-sub",
                "members": ["codex"],
                "source": "account-surface",
                "allowance": {
                    "state": budget.OBSERVED,
                    "observed_at": observed.isoformat(),
                    "utilisation": 0.4,
                    "burn_multiple": 1.0,
                    "resets_at": (now + timedelta(hours=2)).isoformat(),
                },
            }
        ],
        "window_sources": {"codex": "account-surface"},
    }
    monkeypatch.setattr(
        snapshot.budget, "preflight", lambda *a, **k: json.loads(json.dumps(crafted))
    )
    monkeypatch.setattr(snapshot.budget, "summary", lambda report: "")
    report = _compose(config, tmp_path, now, monkeypatch)

    candidate, block = _candidate_and_return_time(report, config, tmp_path, now)

    assert candidate.stale is False
    assert candidate.budget_source == "account-surface"
    assert block["stale"] is False
    assert block["budget_source"] == "account-surface"


def test_reaging_judges_a_document_reading_by_the_documents_own_shelf(
    tmp_path, config, monkeypatch
):
    """A reuse that ages the reading past 900 s flips it, by the same rule."""

    now = datetime.now(UTC)
    document = _document(now, age_seconds=345, shelf_seconds=900)
    report = _compose(config, tmp_path, now, monkeypatch, document=document)
    value = {
        "report": report,
        "built_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    later = now + timedelta(seconds=800)
    reaged = snapshot._reage_budget_report(value, later, config)
    state = next(e["state"] for e in reaged["backends"] if e["backend"] == "codex")

    assert state["source"] == "document"
    assert state["expired"] is True


def test_the_glossary_states_the_three_cases():
    entry = prompts._GLOSSARY["stale"]
    assert "account-surface" in entry
    assert "document" in entry
    assert "ledger" in entry


def test_the_picker_derives_staleness_only_through_the_one_rule():
    """Both snapshot callers delegate, and the rule is owned in one place."""

    compose_source = inspect.getsource(snapshot._compose_budget_report)
    reage_source = inspect.getsource(snapshot._reage_budget_report)
    for source in (compose_source, reage_source):
        assert "_budget_block(" in source
        # The caller takes the stale flag from the rule and never computes one:
        # no line weighing an age against a shelf-life bound of its own.
        assert 'block["stale"]' in source
        for line in source.splitlines():
            assert not (
                ("shelf" in line or "bound" in line) and (">" in line or "<" in line)
            ), line

    rule_source = inspect.getsource(lane_context._budget_block)
    assert "stale = " in rule_source
    computing = {
        name
        for name, value in vars(lane_context).items()
        if inspect.isfunction(value)
        and value.__module__ == lane_context.__name__
        and "stale = " in inspect.getsource(value)
    }
    assert computing == {"_budget_block"}
