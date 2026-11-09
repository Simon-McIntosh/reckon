"""A run's claim has one reader, and every caller agrees with it.

The claim is written beside its run's pointer, and two callers used to parse it
independently: the launch read the registration instant for the worker receipt,
and the admission pass read the whole record. Both now reach one path-taking
reader in the module that writes the claim, so the run id it validates against
and the shapes it refuses are stated once. These cases show the callers agree on
a well-formed claim, on a claim naming another run, and on an unreadable file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import dispatch_claims, dispatch_launch

RUN_ID = "r-one-reader"
OTHER_RUN_ID = "r-someone-else"
REGISTERED_AT = "2026-11-09T10:00:00+00:00"


def _claim(run_id: str = RUN_ID, registered_at: str = REGISTERED_AT) -> str:
    return json.dumps(
        {"run_id": run_id, "claim_registered_at": registered_at, "step": "starting"}
    )


def _directory_with(tmp_path: Path, content: str | None) -> Path:
    directory = tmp_path / RUN_ID
    directory.mkdir(parents=True, exist_ok=True)
    if content is not None:
        (directory / "claim.json").write_text(content, encoding="utf-8")
    return directory


def _both_callers(
    monkeypatch: pytest.MonkeyPatch, directory: Path
) -> tuple[dict[str, object], str]:
    """The record reader and the receipt reader, against the same run directory."""
    monkeypatch.setattr(dispatch_claims, "run_dir", lambda _run_id: directory)
    return (
        dispatch_claims.read_launch_claim(RUN_ID),
        dispatch_launch._claim_registration_for_worker(directory, RUN_ID),
    )


def _agreement(claim: dict[str, object], receipt: str) -> tuple[bool, str]:
    """The one fact both callers must state identically: a claim is read, or not."""
    return (
        bool(claim),
        str(claim.get("claim_registered_at") or "") if claim else "",
    ) == (bool(receipt), receipt)


@pytest.mark.parametrize(
    "content",
    [
        _claim(),
        _claim(run_id=OTHER_RUN_ID),
        "{not json",
        None,
    ],
    ids=["well-formed", "another-run", "unreadable", "absent"],
)
def test_both_callers_agree_on_every_claim_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str | None
) -> None:
    directory = _directory_with(tmp_path, content)
    claim, receipt = _both_callers(monkeypatch, directory)

    assert _agreement(claim, receipt), (claim, receipt)


def test_a_well_formed_claim_is_read_once_and_agreed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _directory_with(tmp_path, _claim())
    claim, receipt = _both_callers(monkeypatch, directory)

    assert claim == {
        "run_id": RUN_ID,
        "claim_registered_at": REGISTERED_AT,
        "step": "starting",
    }
    assert receipt == REGISTERED_AT


def test_a_claim_for_another_run_reads_as_no_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _directory_with(tmp_path, _claim(run_id=OTHER_RUN_ID))
    claim, receipt = _both_callers(monkeypatch, directory)

    assert claim == {}
    assert receipt == ""


def test_an_unreadable_claim_reads_as_no_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _directory_with(tmp_path, "{not json")
    claim, receipt = _both_callers(monkeypatch, directory)

    assert claim == {}
    assert receipt == ""


def test_the_reader_takes_the_directory_it_is_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record is read from the run directory named, never resolved again."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "claim.json").write_text(_claim(), encoding="utf-8")
    monkeypatch.setattr(
        dispatch_claims, "run_dir", lambda _run_id: tmp_path / "not-here"
    )

    assert dispatch_claims.read_claim(elsewhere, RUN_ID)["run_id"] == RUN_ID
    assert dispatch_claims.read_launch_claim(RUN_ID) == {}
    assert dispatch_launch._claim_registration_for_worker(elsewhere, RUN_ID) == (
        REGISTERED_AT
    )
