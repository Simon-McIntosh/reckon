"""A provider refusal is a triageable stop, not a completion or a crash.

The account, the limit kind, and the reset are the most triageable stop a
fleet can suffer, and the harness that reports the refusal labels it exactly
like an ordinary completion: ``type: result, subtype: success``, with the
refusal text sitting in the result field and ``is_error`` carrying the only
honest signal. Five real dispatches died on the same account spend limit on
2026-09-03 and every one of them displayed as working for ten minutes,
because nothing downstream had a failure to classify. The terminal events
embedded below are copied verbatim (session id, refusal text and error
status) from those five recorded streams rather than invented, because a
quota refusal needs an account that was actually spent and that is not
reproducible on demand.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from reckon import _backends, ledger
from reckon.crew import promotion
from reckon.crew.node import CrewError

FIXTURES = Path(__file__).parent / "fixtures" / "backends"
CLAUDE = {"launch": "cli", "command": "claude"}
CODEX = {"launch": "cli", "command": "codex"}


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "docs" / "state" / "proj").mkdir(parents=True)
    return root


# ── Ledger-side classification: what a completed run is charged as ─────────


def test_failing_promotion_requires_a_closed_failure_classification() -> None:
    with pytest.raises(CrewError) as error:
        promotion.complete("run-a", gate="failed", outcome="the check failed")

    message = str(error.value)
    assert "--failure-classification" in message
    assert all(value in message for value in ledger.FAILURE_CLASSIFICATIONS)


def test_worker_pass_rate_uses_only_work_rejected_failures(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    records = [
        ledger.build_record(run_id=f"pass-{index}", plan="work", gate="passed")
        for index in range(8)
    ]
    records.extend(
        ledger.build_record(
            run_id=f"rejected-{index}",
            plan="work",
            gate="failed",
            failure_classification="work-rejected",
        )
        for index in range(2)
    )
    records.extend(
        ledger.build_record(
            run_id=f"excluded-{index}",
            plan="work",
            gate="failed",
            failure_classification=classification,
        )
        for index, classification in enumerate(ledger.FAILURE_CLASSIFICATIONS[1:])
    )
    for record in records:
        ledger.append_run("proj", record, root=root)

    stored = ledger.runs("proj", root=root)
    report = ledger.summary("proj", root=root)["worker_gate"]

    assert stored[8]["failure_classification"] == "work-rejected"
    assert report["passed"] == 8
    assert report["work_rejected"] == 2
    assert report["pass_rate"] == 0.8
    assert report["excluded"] == {
        classification: 1 for classification in ledger.FAILURE_CLASSIFICATIONS[1:]
    }
    assert report["unclassified"] == 0


# ── Stream-side classification: what phase a live run reports ──────────────

# The refusal text and error status are byte-identical across all five; only
# the session id (and therefore the account context) differs, which is what
# five independently dispatched, independently killed workers looks like.
_SPEND_REFUSAL_TEXT = (
    "You've hit your individual spend limit · run /usage-credits to ask "
    "your admin for a higher limit · your session limit resets 12pm "
    "(Europe/Paris)"
)

# One recorded terminal event per dispatch, node ids withheld per the
# repository's naming rule; the session ids are what actually distinguish them.
_FIVE_RECORDED_REFUSALS = [
    "e9466187-8dc1-4748-b10f-044fcb1bbdf1",
    "e9235989-8d30-4584-8f42-9e881c67a4b7",
    "cc02e920-760b-40e8-aa15-07a89f2a244f",
    "f8e24777-00b7-4e26-853a-45e35ad401b8",
    "4a867318-1d2e-4814-a73f-57b9088ce487",
]


def _spend_refusal_event(session_id: str) -> dict[str, Any]:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "result": _SPEND_REFUSAL_TEXT,
        "session_id": session_id,
        "api_error_status": 429,
    }


def _write_stream(tmp_path: Path, name: str, events: list[dict[str, Any]]) -> Path:
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
    return path


def _observe(
    path: Path, backend: dict[str, str] = CLAUDE, backend_name: str = "claude"
):
    return _backends.observe_log(
        backend_name=backend_name, backend=backend, log_path=path
    )


# ── The five recorded dispatches ─────────────────────────────────────────────


def test_each_of_the_five_recorded_refusals_classifies_as_blocked(
    tmp_path: Path,
) -> None:
    """Five independently dispatched workers, one spent account, one verdict."""
    for index, session_id in enumerate(_FIVE_RECORDED_REFUSALS):
        stream = _write_stream(
            tmp_path,
            f"stream-{index}.jsonl",
            [_spend_refusal_event(session_id)],
        )

        observation = _observe(stream)

        assert observation.phase == "blocked", session_id
        assert observation.phase not in ("complete", "abandoned"), session_id


def test_a_recorded_refusal_is_recognised_by_its_content_not_its_label(
    tmp_path: Path,
) -> None:
    """The harness calls it a success; nothing about the label is trusted."""
    stream = _write_stream(
        tmp_path, "stream.jsonl", [_spend_refusal_event(_FIVE_RECORDED_REFUSALS[0])]
    )
    raw = json.loads(stream.read_text().strip())
    assert raw["type"] == "result"
    assert raw["subtype"] == "success"

    observation = _observe(stream)

    assert observation.phase == "blocked"


def test_the_blocked_reason_names_the_backend_the_limit_kind_and_the_reset(
    tmp_path: Path,
) -> None:
    """The transition line alone must answer the triage question."""
    stream = _write_stream(
        tmp_path, "stream.jsonl", [_spend_refusal_event(_FIVE_RECORDED_REFUSALS[0])]
    )

    observation = _observe(stream, backend_name="claude-sonnet")

    assert "claude-sonnet" in observation.detail
    assert "spend-limit" in observation.detail
    assert observation.budget["rate_limit_type"] == "spend-limit"


def test_a_reset_the_refusal_does_not_carry_is_reported_as_unknown_not_omitted(
    tmp_path: Path,
) -> None:
    """A spend-limit refusal here names only a time of day, never a date."""
    stream = _write_stream(
        tmp_path, "stream.jsonl", [_spend_refusal_event(_FIVE_RECORDED_REFUSALS[0])]
    )

    observation = _observe(stream)

    assert observation.budget["resets_at"] is None
    assert "reset unknown" in observation.detail


def test_a_refusal_with_a_parseable_reset_states_it_in_the_reason() -> None:
    """A usage-limit refusal (the other recorded phrasing) names a full date."""
    observation = _observe(
        FIXTURES / "codex-usage-limit.jsonl", backend=CODEX, backend_name="codex"
    )

    assert observation.phase == "blocked"
    assert observation.budget["resets_at"] is not None
    assert observation.budget["resets_at"] in observation.detail
    assert "usage-limit" in observation.detail


# ── Negatives: a refusal blocks, and nothing else does ──────────────────────


def test_a_genuine_completion_does_not_become_blocked() -> None:
    """The harness's ordinary success path must be untouched."""
    observation = _observe(FIXTURES / "claude-turn.jsonl")

    assert observation.phase == "complete"


def test_a_genuine_ordinary_failure_does_not_become_blocked() -> None:
    """An unusable model id ends the turn but says nothing about the account.

    Same shape as a refusal at this layer -- ``type: result``, ``is_error:
    true`` -- so misreading this as exhaustion would hold every later wave on
    evidence that was never a measurement.
    """
    observation = _observe(FIXTURES / "claude-failed-turn.jsonl")

    assert observation.phase == "failed"
    assert observation.phase != "blocked"


def test_a_refusal_recovered_mid_stream_is_not_blocked_by_its_own_history(
    tmp_path: Path,
) -> None:
    """A run that hit the limit three times and kept going is not blocked.

    Recorded from the sixth worker of the same incident: it hit the spend
    limit three times, recovered, and produced two further genuine turns
    before its process was later killed outright with no further terminal
    event -- killed rather than blocked, because classification reads the
    stream's true terminal event and does not latch onto an earlier one.
    """
    session_id = "a20f3716-05bb-4138-baea-a0c042b0b243"
    events = [
        _spend_refusal_event(session_id),
        _spend_refusal_event(session_id),
        _spend_refusal_event(session_id),
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "I'll wait for the settle notification rather than poll further.",
            "session_id": session_id,
            "api_error_status": None,
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "Holding for segment 2.",
            "session_id": session_id,
            "api_error_status": None,
        },
    ]
    stream = _write_stream(tmp_path, "stream.jsonl", events)

    observation = _observe(stream)

    assert observation.phase == "complete"
    assert observation.phase != "blocked"


def test_a_genuine_crash_with_no_terminal_event_is_not_blocked(tmp_path: Path) -> None:
    """A process that dies mid-turn writes no terminal event at all.

    Reading ``working`` forever is correct here -- only the process table can
    tell a stuck worker from a dead one -- but it must never read ``blocked``,
    which claims a specific, triageable, resumable condition that this stream
    gives no evidence for.
    """
    session_id = "e9466187-8dc1-4748-b10f-044fcb1bbdf1"
    events = [
        {"type": "system", "subtype": "init", "session_id": session_id},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "working on it"}]},
            "session_id": session_id,
        },
    ]
    stream = _write_stream(tmp_path, "stream.jsonl", events)

    observation = _observe(stream)

    assert observation.phase == "working"
    assert observation.phase != "blocked"


def test_an_absent_log_is_not_blocked() -> None:
    """No process has reported yet; starting is not a triage state."""
    observation = _observe(FIXTURES / "no-such-stream.jsonl")

    assert observation.phase == "starting"
    assert observation.phase != "blocked"
