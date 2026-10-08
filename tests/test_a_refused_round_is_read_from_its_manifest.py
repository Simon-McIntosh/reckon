"""A refused repair round is read from its manifest, however it is worded.

The reflex resumes a reviewed run once, retries once more if the resumed turn
ends without answering, and then exhausts the round. Whether that ended turn
*refused* the round or died mid-work must not depend on the wording it chose.
The signal is the manifest it left: a terminal status written after this round's
resume began, while the reviewed head has not moved. The round token the advice
opens with is corroborating evidence where present, never the only test — a
terminal manifest that paraphrases the blocker and quotes nothing has still
refused.

Four cases fix the reading:

* a terminal manifest that paraphrases the blocker and omits the token
  suppresses the retry;
* a terminal manifest written before the round's resume began does not;
* a manifest left with no terminal status is still retried;
* a manifest quoting this round's token still suppresses.

The declared negative control restores the token-only test — requiring the
literal round-token line — and the paraphrased-refusal case must then turn red.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from reckon.crew import recovery, recovery_repair_dispatch, runs
from tests.test_the_reflex_resumes_an_unpromoted_run import (  # noqa: F401
    CONFIG,
    FINDING,
    RUN_ID,
    _completed_pointer,
    _store_review,
    _stub_dispatch,
    _stub_resume,
    _sweep,
    isolated_project,
)

pytestmark = pytest.mark.arms_watch_producer

# The declared mutation, printed verbatim as the red log's first line.
DECLARED_MUTATION = (
    "restore the token-only refusal test in _reviewed_run_refused_the_round "
    "(require the literal Repair round line); the paraphrased-refusal case must "
    "turn red"
)

MUTATION_ENV = "RECKON_REFUSED_ROUND_MANIFEST_NEGATIVE_CONTROL"

# A refusal that paraphrases the blocker and quotes no round token, so only the
# manifest's terminal status and its write time can settle the round.
PARAPHRASED_REFUSAL = (
    "node: {node}\n"
    "status: blocked\n"
    "blockers: the settled head still blocks this finding; the round's advice "
    "cannot be carried out\n"
    "write scope: reckon/crew/thing.py\n"
)

# A manifest a turn leaves while still working: present and fresh, but no
# terminal status, so it is not evidence that the turn ended.
IN_PROGRESS_MANIFEST = (
    "node: {node}\nstatus: in-progress\ncheckpoint: editing the loop\n"
)

# The verbatim refusal: terminal, and quoting the round token the advice opens
# with, kept as the corroborating-evidence case.
VERBATIM_REFUSAL = (
    "node: {node}\n"
    "status: blocked\n"
    "blockers: the round's advice cannot be carried out — {token}\n"
)


def _token_only_refusal(record: dict, round_id: str) -> bool:
    """The reading the declared mutation restores: the literal token alone."""
    path = str(record.get("manifest_path") or "")
    if not path:
        return False
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return False
    if recovery._repair_round_token(round_id) not in text:
        return False
    try:
        parsed = recovery.parse_manifest(text, path=path)
    except Exception:  # noqa: BLE001 - an unreadable manifest is not a refusal
        return False
    return recovery.manifest_status_is_terminal(parsed.get("status"))


@pytest.fixture(autouse=True)
def _declared_negative_control(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply the declared mutation when its environment variable is set."""
    if os.environ.get(MUTATION_ENV) == "1":
        monkeypatch.setattr(
            recovery_repair_dispatch, "_reviewed_run_refused_the_round", _token_only_refusal
        )


def _round_token(advice: str) -> str:
    """The round token line from the advice the sweep composed."""
    for line in advice.splitlines():
        if line.startswith(recovery.REPAIR_ROUND_TOKEN_LINE):
            return line
    raise AssertionError(f"advice carries no round token line: {advice!r}")


def _write_manifest(record: dict, body: str) -> None:
    Path(record["manifest_path"]).write_text(
        body.format(node=record["node"]["id"]), encoding="utf-8"
    )


def _age_manifest(record: dict, *, seconds_before_start: float) -> None:
    """Date the manifest before the round's own resume stamp."""
    recorded = runs.read_pointer(RUN_ID)["repair_dispatch"]
    started = recovery.parse_utc(recorded["at"])
    assert started is not None, "the round recorded no start stamp"
    when = started.timestamp() - seconds_before_start
    os.utime(record["manifest_path"], (when, when))


def test_a_paraphrased_refusal_suppresses_the_retry(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal manifest that omits the token has still refused the round.

    The resumed turn ends by refusing the round in its own words — terminal
    status, no copied opening line — and the worker is gone with the head
    unmoved. The second sweep must read the fresh terminal manifest as a refusal
    and record the round exhausted rather than resume a second worker.
    """
    config_home, project_repo, head_sha = request.getfixturevalue("isolated_project")
    record = _completed_pointer(config_home, project_repo)
    _store_review(head_sha, [FINDING])
    resumed = _stub_resume(monkeypatch)
    _stub_dispatch(monkeypatch)

    _sweep()
    first = runs.read_pointer(RUN_ID)["repair_dispatch"]
    assert first["status"] == "resumed"
    assert first["attempt"] == 1

    _write_manifest(record, PARAPHRASED_REFUSAL)
    _sweep()

    assert len(resumed) == 1, "a paraphrased refusal was re-sent into the same dead end"
    recorded = runs.read_pointer(RUN_ID)["repair_dispatch"]
    assert recorded["status"] == "exhausted", recorded


def test_a_terminal_manifest_older_than_the_round_still_retries(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal manifest the round did not write is the run's earlier state.

    The run already carried a terminal manifest — the status it held before any
    repair. That manifest answers nothing about this round, so the ended turn
    reads as a mid-work death and the retry stands.
    """
    config_home, project_repo, head_sha = request.getfixturevalue("isolated_project")
    record = _completed_pointer(config_home, project_repo)
    _store_review(head_sha, [FINDING])
    resumed = _stub_resume(monkeypatch)
    _stub_dispatch(monkeypatch)

    _sweep()
    _write_manifest(record, PARAPHRASED_REFUSAL)
    _age_manifest(record, seconds_before_start=3600)
    _sweep()

    assert len(resumed) == 2, "a manifest predating the round suppressed the retry"
    recorded = runs.read_pointer(RUN_ID)["repair_dispatch"]
    assert recorded["status"] == "resumed"
    assert recorded["attempt"] == 2


def test_a_fresh_manifest_without_a_terminal_status_still_retries(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn that left no terminal status has not refused the round.

    The manifest is fresh but carries an in-progress status, the shape a turn
    still working leaves. It is not a refusal, so the retry reaches a second
    resume exactly as before.
    """
    config_home, project_repo, head_sha = request.getfixturevalue("isolated_project")
    record = _completed_pointer(config_home, project_repo)
    _store_review(head_sha, [FINDING])
    resumed = _stub_resume(monkeypatch)
    _stub_dispatch(monkeypatch)

    _sweep()
    _write_manifest(record, IN_PROGRESS_MANIFEST)
    _sweep()

    assert len(resumed) == 2, "a non-terminal manifest suppressed the retry"
    recorded = runs.read_pointer(RUN_ID)["repair_dispatch"]
    assert recorded["status"] == "resumed"
    assert recorded["attempt"] == 2


def test_a_manifest_quoting_the_round_token_still_suppresses(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The token remains corroborating evidence: quoting it is a refusal.

    The verbatim refusal — terminal, written this round, quoting the round's own
    token — must still read as a refusal, whether or not the freshness test
    would reach the same answer.
    """
    config_home, project_repo, head_sha = request.getfixturevalue("isolated_project")
    record = _completed_pointer(config_home, project_repo)
    _store_review(head_sha, [FINDING])
    resumed = _stub_resume(monkeypatch)
    _stub_dispatch(monkeypatch)

    _sweep()
    Path(record["manifest_path"]).write_text(
        VERBATIM_REFUSAL.format(
            node=record["node"]["id"], token=_round_token(resumed[0]["advice"])
        ),
        encoding="utf-8",
    )
    _sweep()

    assert len(resumed) == 1, (
        "a token-quoting refusal was re-sent into the same dead end"
    )
    recorded = runs.read_pointer(RUN_ID)["repair_dispatch"]
    assert recorded["status"] == "exhausted", recorded


if __name__ == "__main__":  # pragma: no cover - reproduces the red log
    print(DECLARED_MUTATION)
    os.environ[MUTATION_ENV] = "1"
    raise SystemExit(pytest.main(["-p", "no:cacheprovider", "-q", str(Path(__file__))]))
