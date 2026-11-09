"""A manifest's gate evidence is read in the shapes workers actually write.

Two honest deliveries were misread by promotion. A ``test_logs`` line carrying
two space-separated paths was read as one entry, so the gate fill opened a path
that does not exist, read no log, and refused a passing gate for a missing exit
status. A missing newline folded ``baseline_suite: {…}`` into the trailing prose
of ``blockers``, so the manifest recorded no baseline arm and a red-base
admission had nothing to read.

The cases below hold the reader to the shapes workers write: several
entry-shaped tokens on one line are several entries, an entry beside a prose
word stays one entry, an arm glued onto a word is refused at the parse, and a
promotion refused for a passing verdict beside a nonzero exit names the arm
shapes a manifest recording neither arm is missing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import promotion_evidence
from reckon.crew.node import CrewError
from reckon.crew.promotion_records import _default_gate_evidence_from_manifest
from reckon.crew.reports import ManifestParseError, parse_manifest


def _write(tmp_path: Path, body: str) -> dict:
    """Write a text manifest and return the run record pointing at it."""
    manifest_path = tmp_path / "manifest.md"
    manifest_path.write_text(body, encoding="utf-8")
    return {"manifest_path": str(manifest_path)}


def _parsed(record: dict) -> dict:
    return parse_manifest(
        Path(record["manifest_path"]).read_text(encoding="utf-8"),
        path=record["manifest_path"],
    )


def _log(tmp_path: Path, name: str, exit_status: int) -> Path:
    path = tmp_path / name
    path.write_text(f"1 passed in 0.10s\nEXIT={exit_status}\n", encoding="utf-8")
    return path


HEADER = "node: a-node\nstatus: complete\n"


def test_a_space_joined_test_logs_line_reads_each_log(tmp_path: Path) -> None:
    """Two log paths on one line are two entries, and the fill reads the first."""
    first = _log(tmp_path, "first.log", 1)
    second = _log(tmp_path, "second.log", 0)
    record = _write(
        tmp_path,
        HEADER
        + "tests: python -m pytest -q tests/x.py\n"
        + f"test_logs: {first} {second}\n",
    )

    fields = _parsed(record)
    assert fields["test_logs"] == [str(first), str(second)]

    gate, _ = _default_gate_evidence_from_manifest(
        record, verdict="passed", gate_check=None, commits=()
    )
    assert gate["log_path"] == str(first)
    assert gate["exit_status"] == 1


def test_space_separated_paths_read_as_two_entries(tmp_path: Path) -> None:
    """``changed_paths`` and ``orientation_write_paths`` split on whitespace too."""
    record = _write(
        tmp_path,
        HEADER
        + "changed_paths: reckon/crew/reports.py tests/test_x.py\n"
        + 'orientation_write_paths: ["reckon/crew/reports.py", "tests/test_x.py"]\n',
    )
    fields = _parsed(record)
    assert fields["changed_paths"] == ["reckon/crew/reports.py", "tests/test_x.py"]
    assert fields["orientation_write_paths"] == [
        "reckon/crew/reports.py",
        "tests/test_x.py",
    ]

    space_joined = _write(
        tmp_path,
        HEADER + "orientation_write_paths: reckon/crew/reports.py tests/test_x.py\n",
    )
    assert _parsed(space_joined)["orientation_write_paths"] == [
        "reckon/crew/reports.py",
        "tests/test_x.py",
    ]


def test_four_space_separated_commits_read_as_four(tmp_path: Path) -> None:
    """Several commit ids on one line are several commits."""
    shas = ["1f74899e3f", "a337e0e6c3", "5e949d7c44", "23278f47c0"]
    record = _write(tmp_path, HEADER + "commits: " + " ".join(shas) + "\n")

    assert _parsed(record)["commits"] == shas


def test_a_path_beside_a_prose_word_stays_one_entry(tmp_path: Path) -> None:
    """A token that is not entry-shaped leaves the present reading in place."""
    record = _write(tmp_path, HEADER + "test_logs: logs/run.log and the sweep\n")

    assert _parsed(record)["test_logs"] == ["logs/run.log and the sweep"]


def test_a_commit_beside_its_subject_stays_one_entry(tmp_path: Path) -> None:
    """A commit id followed by its subject is one entry, as before."""
    record = _write(
        tmp_path, HEADER + "commits: 1f74899e3f docs(plan): declare the receipt\n"
    )

    assert _parsed(record)["commits"] == ["1f74899e3f docs(plan): declare the receipt"]


def test_a_glued_suite_arm_key_is_refused(tmp_path: Path) -> None:
    """A missing newline that folds an arm into prose refuses at the parse."""
    record = _write(
        tmp_path,
        HEADER
        + "blockers: none. The arm is passed back herebaseline_suite: "
        + '{"revision": "abc", "exit_status": 0}\n',
    )

    with pytest.raises(ManifestParseError) as raised:
        _parsed(record)
    message = str(raised.value)
    assert "'blockers'" in message
    assert "'baseline_suite'" in message
    assert "its own line" in message


def test_a_mention_of_an_arm_without_an_object_parses(tmp_path: Path) -> None:
    """Prose naming the arm stays prose when no object follows it.

    The manifest records no arm of that name, so this case turns on the mention
    alone: a rule that refused every mention would refuse it, while the glued
    rule requires an object after the key.
    """
    record = _write(
        tmp_path,
        HEADER + "blockers: none. The baseline_suite arm is recorded below.\n",
    )

    assert _parsed(record)["blockers"] == [
        "none. The baseline_suite arm is recorded below."
    ]


def test_a_quoted_arm_after_a_space_parses(tmp_path: Path) -> None:
    """A quotation of the arm's shape after a space is text, not a glued key."""
    record = _write(
        tmp_path,
        HEADER
        + "evidence_inputs: a missing newline glues baseline_suite: { onto prose\n",
    )

    assert _parsed(record)["evidence_inputs"] == [
        "a missing newline glues baseline_suite: { onto prose"
    ]


def _armless_record(tmp_path: Path) -> dict:
    return _write(tmp_path, HEADER + "checkpoint: no arms recorded here\n")


def test_a_passing_verdict_beside_a_nonzero_exit_names_the_missing_arms(
    tmp_path: Path,
) -> None:
    """A manifest recording no arm is refused naming both arms and their fields."""
    gate = {"command": "python -m pytest -q", "exit_status": 1, "log_path": "x.log"}

    with pytest.raises(CrewError) as raised:
        promotion_evidence._require_verdict_matches_exit_status(
            "r-test", _armless_record(tmp_path), gate, verdict="passed"
        )
    message = str(raised.value)
    assert "baseline_suite" in message
    assert "after_suite" in message
    for field in ledger.suite_observation_missing_fields({}, name="baseline_suite"):
        assert field in message
    for field in ledger.suite_observation_missing_fields({}, name="after_suite"):
        assert field in message


def test_the_arm_shape_is_read_from_the_ledger_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A field added to the ledger's arm contract is named by the refusal."""
    real = ledger.suite_observation_missing_fields

    def with_extra(observation, *, name):
        return [*real(observation, name=name), f"{name}.revision_matches_head"]

    monkeypatch.setattr(ledger, "suite_observation_missing_fields", with_extra)
    gate = {"command": "python -m pytest -q", "exit_status": 1, "log_path": "x.log"}

    with pytest.raises(CrewError) as raised:
        promotion_evidence._require_verdict_matches_exit_status(
            "r-test", _armless_record(tmp_path), gate, verdict="passed"
        )
    assert "baseline_suite.revision_matches_head" in str(raised.value)
    assert "after_suite.revision_matches_head" in str(raised.value)
