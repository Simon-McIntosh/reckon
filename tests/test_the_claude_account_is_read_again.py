"""The claude account reads again: its probe names its reason, its read answers.

Three defects kept the account's windows unknown. The reserved probe discarded
the child's stream and published only its exit status, so a failure named no
cause. The account read was pointed at a host that refuses it. And the publish
gate probed only a window that had gone stale, which a window nobody ever
observed can never do. Each case below is driven from a recorded answer; no test
in this file makes a network request.
"""

from __future__ import annotations

import io
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import _backends
from reckon.crew import paid_lanes

NOW = datetime(2026, 10, 21, 5, 30, tzinfo=UTC)

# The answer the live surface returned for this account, recorded verbatim (its
# unrelated keys dropped, its two account windows kept whole).
RECORDED_ANSWER = {
    "five_hour": {
        "utilization": 62.0,
        "resets_at": "2026-10-22T00:20:00.348946+00:00",
    },
    "seven_day": {
        "utilization": 74.0,
        "resets_at": "2026-10-26T20:00:00.348964+00:00",
    },
}

BACKENDS = {
    "claude": {
        "launch": "cli",
        "command": "claude",
        "budget_check": True,
        "budget_group": "claude-sub",
    },
}


# ── The reserved probe keeps the reason its child reported ──────────────────


def test_a_probe_child_exiting_non_zero_publishes_the_reason_it_gave(monkeypatch):
    """A refusal that names a cause must not be folded away into an exit code.

    The harness reports a model the account is not served on the run stream and
    then exits 1. Kept alone, the exit status cannot tell that apart from a
    lapsed login, so the published failure carries the child's own words.
    """
    stream = "\n".join(
        [
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps(
                {
                    "type": "assistant",
                    "error": "model_not_found",
                    "api_error_status": 404,
                    "result": "There's an issue with the selected model.",
                }
            ),
        ]
    )
    completed = subprocess.CompletedProcess(
        args=["claude"], returncode=1, stdout=stream, stderr=""
    )
    monkeypatch.setattr(paid_lanes.subprocess, "run", lambda *args, **kwargs: completed)

    with pytest.raises(OSError, match="model_not_found") as raised:
        paid_lanes._run_reserved_probe(BACKENDS["claude"], Path("."))

    assert "exited 1" in str(raised.value)


def test_a_probe_child_with_no_output_states_its_exit_status_alone(monkeypatch):
    """The tail is added to the status, never substituted for it."""
    completed = subprocess.CompletedProcess(
        args=["claude"], returncode=3, stdout="", stderr=""
    )
    monkeypatch.setattr(paid_lanes.subprocess, "run", lambda *args, **kwargs: completed)

    with pytest.raises(OSError, match="exited 3") as raised:
        paid_lanes._run_reserved_probe(BACKENDS["claude"], Path("."))

    assert str(raised.value) == "reserved window probe exited 3"


# ── The account read answers on the live surface ────────────────────────────


def test_the_read_calls_the_endpoint_that_answers_with_the_beta_marker(monkeypatch):
    """The URL and the beta marker are the surface's contract, not a setting."""
    seen: dict[str, str] = {}

    class _Answer:
        def __enter__(self) -> io.StringIO:
            return io.StringIO(json.dumps(RECORDED_ANSWER))

        def __exit__(self, *exc: object) -> bool:
            return False

    def _open(request, timeout=None):
        seen["url"] = request.full_url
        seen["beta"] = request.get_header("Anthropic-beta")
        return _Answer()

    monkeypatch.setattr(_backends.urllib.request, "urlopen", _open)

    payload = _backends._fetch_claude_account({"accessToken": "recorded"})

    assert payload == RECORDED_ANSWER
    assert seen["url"] == "https://api.anthropic.com/api/oauth/usage"
    assert seen["beta"] == "oauth-2025-04-20"


def test_the_recorded_answer_is_read_on_both_windows():
    """Both account windows are read, and the binding one speaks for the lane."""
    block = _backends._parse_claude_account(RECORDED_ANSWER)

    assert block["headroom"] == "known"
    assert block["utilisation_pct"] == 74.0
    assert block["rate_limit_type"] == "seven_day"
    assert block["rate_limit_period_minutes"] == 10080
    assert block["resets_at"] == "2026-10-26T20:00:00.348964+00:00"
    assert block["quota_windows"][300]["used_percent"] == 62.0
    assert block["quota_windows"][300]["resets_at"] == (
        "2026-10-22T00:20:00.348946+00:00"
    )
    assert block["quota_windows"][300]["window_minutes"] == 300
    assert block["quota_windows"][300]["rate_limit_type"] == "five_hour"


def test_the_answer_publishes_both_clocks_as_the_fractions_the_document_carries():
    """62.0 and 74.0 on the answer are 0.62 and 0.74 on the document's clocks.

    The surface states utilisation as a percentage where the document carries a
    fraction, so the conversion is asserted where the two meet rather than
    assumed from either side.
    """
    block = _backends._parse_claude_account(RECORDED_ANSWER)

    reading = paid_lanes._probe_reading(block, NOW)

    figures = {figure.period: figure for figure in reading.figures}
    assert figures["five_hour"].utilisation == pytest.approx(0.62)
    assert figures["seven_day"].utilisation == pytest.approx(0.74)
    assert figures["five_hour"].resets_at == "2026-10-22T00:20:00.348946+00:00"
    assert figures["seven_day"].observed_at == NOW


def test_an_answer_naming_neither_window_is_refused_in_the_readers_own_words():
    """A shape the reader cannot name a window in is unknown, never a low figure."""
    for payload in (
        ["not", "an", "object"],
        {"unrelated": "shape"},
        {"five_hour": {"utilization": "high"}},
        {"seven_day": {"utilization": 74.0}},
    ):
        block = _backends._parse_claude_account(payload)

        assert block["headroom"] == "unknown"
        assert block["utilisation_pct"] is None
        assert "account answer" in block["detail"]


# ── No test reads the login's own account ───────────────────────────────────


def test_a_test_cannot_read_the_logins_own_account():
    """The suite guard replaces the account-surface read with a no-answer stub.

    Asserted on the replacement rather than on a reading, because a reading
    cannot tell a guarded read from an unguarded one: an unguarded read on a
    logged-out machine answers unknown exactly as the stub does, so a
    reading-shaped assertion would pass with the guard removed and only fail on
    a machine that happens to be logged in. The claude dialect's own read
    returns a budget block on every path it can take — known, or unknown naming
    the reason — so it answers nothing at all only where it has been replaced,
    and that holds whatever this machine's account state is.
    """
    from reckon import _backends

    claude = _backends.dialect_for({"command": "claude"})

    assert claude.read_account_surface(backend={"command": "claude"}) is None


# ── An all-unknown account is due for a probe ───────────────────────────────


def _prior_document(*, attempted_at: datetime) -> dict:
    """A published document whose claude account has never been read."""
    return {
        "accounts": {
            "claude": {
                "probe": {
                    "attempted_at": attempted_at.isoformat(),
                    "failure": "account read failed — HTTP Error 403: Forbidden",
                },
                "reserved_probe": {
                    "attempted_at": attempted_at.isoformat(),
                    "failure": "OSError: reserved window probe exited 1",
                },
            }
        }
    }


def _publish(tmp_path, monkeypatch, *, attempted_at: datetime):
    """Publish with the probe replaced by a recorder, so nothing leaves the box."""
    reached: list[str] = []

    def _record(**kwargs):
        reached.append(kwargs["backend_name"])
        return _backends.unknown_budget("recorder: no request was made")

    monkeypatch.setattr(_backends, "probe_budget", _record)

    path = tmp_path / "paid-lanes.json"
    paid_lanes.write_document_atomically(
        _prior_document(attempted_at=attempted_at), path
    )
    document = paid_lanes.publish_document(
        {"backends": BACKENDS},
        {},
        path=path,
        moment=NOW,
        local_lane={"state": "unknown"},
    )
    return reached, document


def test_an_account_whose_windows_are_all_unknown_is_due_for_a_probe(
    tmp_path, monkeypatch
):
    """Unknown is itself a reason to read: no attempt has ever resolved it.

    A window nobody has observed can never go stale, so without this the account
    could only leave unknown through a read the gate declines to make. The last
    attempt here is older than the guard, so the probe is reached.
    """
    reached, document = _publish(
        tmp_path, monkeypatch, attempted_at=NOW - timedelta(hours=2)
    )

    windows = document["accounts"]["claude"]["windows"].values()
    assert reached == ["claude"]
    assert windows and all(window["state"] == paid_lanes.UNKNOWN for window in windows)
    assert document["accounts"]["claude"]["probe"]["attempted_at"] == NOW.isoformat()


def test_an_all_unknown_account_attempted_inside_the_guard_is_not_probed(
    tmp_path, monkeypatch
):
    """The recent guard bounds the unknown case exactly as it bounds a stale one."""
    reached, document = _publish(
        tmp_path, monkeypatch, attempted_at=NOW - timedelta(seconds=60)
    )

    assert reached == []
    assert (
        document["accounts"]["claude"]["probe"]["attempted_at"]
        == (NOW - timedelta(seconds=60)).isoformat()
    )
