"""The replay of two recorded pick refusals reproduces both refusals.

The script under ``docs/evidence/fragments/work-is-never-stuck-behind-the-
admission-loop/`` rebuilds each attempt from the dispatch that made it and asks
the picker the same question in process. This module runs it and reads its
report, so a change to the picker's candidate scan that stops withholding the
reserved lanes, or that starts offering one, shows up here as a refusal the
replay no longer reproduces.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

#: The suite points every test at a throwaway configuration home. The replay's
#: subject is a recorded fleet decision, and the routing verdict it reproduces
#: reads the project's promotion ledger, so the child runs against the same
#: store the promotion-time invocation reads and writes nothing to it: the pick
#: is asked with the pick-timings append suppressed and no ledger write path.
_ISOLATED = ("RECKON_HOME", "XDG_CONFIG_HOME", "FLEET_SESSION_TMP_ROOT")
SCRIPT = (
    REPO
    / "docs/evidence/fragments/work-is-never-stuck-behind-the-admission-loop"
    / "replay_oversized_route.py"
)

#: The two attempts the plan records, and the exclusion rows each carried.
RECORDED = {
    "attempt-1": [
        "claude reserved-lane",
        "claude reserved-lane",
        "claude reserved-lane",
        "claude reserved-lane",
        "clive context-fit",
        "clive context-fit",
        "clive competence",
        "codex reserved-lane",
        "codex reserved-lane",
        "codex reserved-lane",
        "native in-harness-backend",
    ],
    "attempt-4": [
        "codex reserved-lane",
        "codex reserved-lane",
        "codex reserved-lane",
    ],
}


def _run(tmp_path: Path) -> tuple[int, str, Path]:
    log = tmp_path / "replay.log"
    env = {k: v for k, v in os.environ.items() if k not in _ISOLATED}
    env["PYTHONPATH"] = str(REPO)
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(REPO)],
        capture_output=True,
        text=True,
        timeout=240,
        env=env,
        check=False,
    )
    log.write_text(completed.stdout + f"EXIT={completed.returncode}\n")
    return completed.returncode, completed.stdout, log


@pytest.fixture(scope="module")
def replay(tmp_path_factory) -> tuple[int, str]:
    tmp_path = tmp_path_factory.mktemp("oversized-route-replay")
    code, out, _log = _run(tmp_path)
    return code, out


def _blocks(out: str) -> dict[str, list[str]]:
    """Split the report into one list of lines per attempt heading."""
    blocks: dict[str, list[str]] = {}
    current: str | None = None
    for line in out.splitlines():
        if line.startswith("=== "):
            current = line.split()[1]
            blocks[current] = []
        elif current is not None:
            blocks[current].append(line)
    return blocks


def _replayed_rows(block: list[str]) -> list[str]:
    """Each exclusion row's backend and reason code, from the printed reasons."""
    rows = []
    for line in block:
        stripped = line.strip()
        if not stripped.startswith("excluded "):
            continue
        backend = stripped.split()[1].split("/")[0]
        reasons = stripped.split(": ", 1)[1]
        rows.extend(
            f"{backend} {reason.split(':', 1)[0].strip()}"
            for reason in reasons.split("; ")
        )
    return rows


def test_the_replay_runs_to_a_clean_exit(replay):
    code, out = replay
    assert code == 0, out
    assert (
        out.rstrip()
        .splitlines()[-1]
        .startswith("replayed: 2 attempts, refusals reproduced")
    )


def test_both_attempts_refuse_and_reproduce_their_recorded_rows(replay):
    _code, out = replay
    blocks = _blocks(out)
    for label, rows in RECORDED.items():
        assert label in blocks, f"{label} is missing from the report:\n{out}"
        block = "\n".join(blocks[label])
        assert "answer: action=refuse decision_source=refused" in block
        assert (
            "fallback_reason=no-eligible-candidates; default-backend-ineligible"
            in block
        )
        assert _replayed_rows(blocks[label]) == rows, block
        assert "exclusion set: reproduced exactly, in the picker's order" in block
        assert "outcome: reproduced" in block


def test_the_named_lane_attempt_offers_that_lane_alone(replay):
    _code, out = replay
    blocks = _blocks(out)
    assert "offer surface: the 'codex' lane alone" in "\n".join(blocks["attempt-4"])
    assert "every configured backend" in "\n".join(blocks["attempt-1"])


def test_every_attempt_is_refused_and_nothing_is_offered(replay):
    _code, out = replay
    assert "offered " not in out, out


def test_the_report_names_the_unrecorded_input_and_its_conditional_branch(replay):
    _code, out = replay
    assert "budget-window-reading: unrecorded, reconstructed" in out
    assert "serving-probe: not reached" in out
    assert "the record does not discriminate between an absent reading" in out
