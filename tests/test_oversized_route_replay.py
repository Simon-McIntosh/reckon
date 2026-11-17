"""The replay of two recorded pick refusals reproduces both refusals.

The script under ``docs/evidence/fragments/work-is-never-stuck-behind-the-
admission-loop/`` rebuilds each attempt from the rules the code keeps, runs the
picker's own candidate scan over it, and reads the deterministic fallback for
the answer. This module runs it and reads its report, so a change that stops
withholding a reserved lane, or that lets the fallback place a node on an
unlifted reservation, shows up here as a refusal the replay no longer
reproduces.

The record's own exclusion rows are held here beside the rows the rules keep,
because the difference between them is the subject: the record predates the
enforced claude reservation and the move of a codex reservation from exclusion
to evidence, and the report has to name every recorded row the picker no longer
emits beside the rule that replaced it.
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

#: The exclusion rows each attempt's pick-timing row records, verbatim.
RECORDED: dict[str, list[str]] = {
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

#: The rows the rules now keep. The enforced claude reservation takes the local
#: wrapper with the claude family, so clive is withheld as claude rather than
#: reached by its context-fit and competence verdicts; and a codex reservation
#: travels beside the candidate as evidence instead of removing it, so the lane
#: attempt excludes nothing at all.
KEPT: dict[str, list[str]] = {
    "attempt-1": [
        "claude reserved-lane",
        "claude reserved-lane",
        "claude reserved-lane",
        "claude reserved-lane",
        "clive reserved-lane",
        "native in-harness-backend",
    ],
    "attempt-4": [],
}

#: The fallback's refusal, where the policy's own words decide once no router is
#: asked: an unlifted reservation is never where the fallback puts a node.
FALLBACK_REFUSAL = (
    "fallback_reason=the fallback places nothing: every offered candidate is a"
    " reserved lane with no lift in force; default-backend-ineligible"
)


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
        # A replay is not interactive: the picker's client can reach a prompt,
        # and inheriting the suite's stdin would block that prompt forever.
        stdin=subprocess.DEVNULL,
        check=False,
    )
    log.write_text(completed.stdout + f"EXIT={completed.returncode}\n")
    return completed.returncode, completed.stdout


@pytest.fixture(scope="module")
def replay(tmp_path_factory) -> tuple[int, str]:
    tmp_path = tmp_path_factory.mktemp("oversized-route-replay")
    code, out = _run(tmp_path)
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
        if not stripped.startswith("codes "):
            continue
        target, codes = stripped[len("codes ") :].split(": ", 1)
        backend = target.split("/")[0]
        rows.extend(f"{backend} {code}" for code in codes.split())
    return rows


def _answers(out: str) -> list[str]:
    """Every attempt's answer line, which is the report's verdict per attempt."""
    return [
        line.strip() for line in out.splitlines() if line.strip().startswith("answer: ")
    ]


def test_the_replay_runs_to_a_clean_exit(replay):
    code, out = replay
    assert code == 0, out
    assert (
        out.rstrip()
        .splitlines()[-1]
        .startswith("replayed: 2 attempts, refusals reproduced")
    )


def test_both_attempts_refuse_and_reproduce_the_rows_the_rules_keep(replay):
    _code, out = replay
    blocks = _blocks(out)
    for label, rows in KEPT.items():
        assert label in blocks, f"{label} is missing from the report:\n{out}"
        block = "\n".join(blocks[label])
        assert "answer: action=refuse decision_source=no-router placed=none" in block
        assert FALLBACK_REFUSAL in block
        assert _replayed_rows(blocks[label]) == rows, block
        assert "outcome: reproduced" in block


def test_each_attempt_names_the_recorded_rows_the_picker_no_longer_emits(replay):
    _code, out = replay
    blocks = _blocks(out)
    for label, rows in RECORDED.items():
        block = "\n".join(blocks[label])
        assert f"recorded rows: {len(rows)}" in block, block
        for row in rows:
            if row in KEPT[label]:
                continue
            backend, code = row.split()
            assert f"superseded: ('{backend}', '{code}')" in block, block
        if any(row not in KEPT[label] for row in rows):
            assert "rule behind those rows: " in block, block


def test_the_named_lane_attempt_offers_that_lane_alone(replay):
    _code, out = replay
    blocks = _blocks(out)
    assert "offer surface: the 'codex' lane alone" in "\n".join(blocks["attempt-4"])
    assert "every configured backend" in "\n".join(blocks["attempt-1"])


def test_every_attempt_is_refused_and_the_fallback_places_nothing(replay):
    _code, out = replay
    answers = _answers(out)
    assert len(answers) == 2, answers
    for answer in answers:
        assert "action=refuse" in answer, answer
        assert "placed=none" in answer, answer
    assert "outcome: DIFFERS" not in out


def test_the_report_names_the_unrecorded_input_and_its_conditional_branch(replay):
    _code, out = replay
    assert "budget-window-reading: unrecorded, reconstructed" in out
    assert "serving-probe: not reached" in out
    assert "the record does not discriminate between an absent reading" in out
