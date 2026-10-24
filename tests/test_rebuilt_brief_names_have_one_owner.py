"""The brief names a launch re-composes have one owner.

Two readers ask whether a brief is one a launch composes afresh: the queue's
steering copy asks it before freezing a copy, and the composition asks it
before writing. When each keeps its own set, a name added to one is honoured by
that reader alone — the queue skips its copy while the composition leaves the
path untouched, so the request launches from a path that was never written, and
nothing fails. Declaring the names once, in ``runs`` beside ``STEERING_FILE``
because that module cannot import the review module, makes the two answers the
same answer. This pins it by adding a name to the declaration at runtime and
requiring both readers to answer for it.
"""

from __future__ import annotations

import json

import pytest

from reckon.crew import recovery_review_subject, runs

RUN_ID = "r-fixture-brief-names-one-owner"
ADDED_NAME = "synthesis-brief.md"
#: A path inside the run directory that no reader treats as a brief, used as
#: the negative arm: an added name must be honoured while an ordinary one is not.
ORDINARY_NAME = "notes.md"


@pytest.fixture
def crew_home(tmp_path, monkeypatch):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path))
    run_directory = tmp_path / "crew" / "runs" / RUN_ID
    run_directory.mkdir(parents=True)
    live_directory = tmp_path / "crew" / "live"
    live_directory.mkdir()
    (live_directory / f"{RUN_ID}.json").write_text(
        json.dumps({"run_id": RUN_ID, "project": "fixture"}), encoding="utf-8"
    )
    return run_directory


def test_the_declaration_names_both_review_briefs():
    """The two names the readers must share are the ones actually declared."""
    assert runs.RUN_REVIEW_BRIEF_NAME in runs.REBUILT_BRIEF_NAMES
    assert runs.FIGURE_REVIEW_BRIEF_NAME in runs.REBUILT_BRIEF_NAMES


def test_a_name_added_to_the_declaration_is_honoured_by_both_readers(
    crew_home, monkeypatch
):
    """One name added at the declaration is honoured by both readers.

    The added name stands for any future brief a launch starts composing: if a
    reader keeps a private set, that reader answers for the two names it happens
    to carry and silently disagrees about the new one.
    """
    monkeypatch.setattr(
        runs, "REBUILT_BRIEF_NAMES", runs.REBUILT_BRIEF_NAMES | {ADDED_NAME}
    )

    def fake_fields(record, **kwargs):
        fake_fields.calls.append((record.get("run_id"), kwargs.get("rubric")))
        return {}

    fake_fields.calls = []
    monkeypatch.setattr(recovery_review_subject, "_review_dispatch_fields", fake_fields)

    steering_source = crew_home / ADDED_NAME

    assert runs.is_rebuilt_brief(steering_source) is True
    assert recovery_review_subject.materialize_run_review_brief(steering_source) is True
    assert fake_fields.calls == [(RUN_ID, None)]


def test_an_undeclared_name_is_answered_by_neither_reader(crew_home, monkeypatch):
    """The negative arm: the declaration is what earns the yes.

    Without it a reader could answer True for every path under a run directory,
    which the preceding case alone would not catch.
    """
    monkeypatch.setattr(
        recovery_review_subject, "_review_dispatch_fields", lambda *a, **k: {}
    )
    ordinary = crew_home / ORDINARY_NAME

    assert runs.is_rebuilt_brief(ordinary) is False
    assert recovery_review_subject.materialize_run_review_brief(ordinary) is False
