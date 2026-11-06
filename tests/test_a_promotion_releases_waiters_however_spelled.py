"""A section landing wakes waits with the same project, plan, and section."""

from pathlib import Path

import pytest

from reckon.crew import promotion, queue_admission
from tests import test_a_queued_request_waits_on_its_dependencies as waits


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "crew"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


def _candidate(monkeypatch: pytest.MonkeyPatch, *, project: str, ref: str) -> dict:
    pointer = {
        "run_id": "r-waiter",
        "project": project,
        "phase": queue_admission.AWAITING_UPSTREAM_PHASE,
        "waits": [ref],
    }
    monkeypatch.setattr(queue_admission.runs, "list_live", lambda: [pointer])
    monkeypatch.setattr(
        queue_admission,
        "_resolve_and_move",
        lambda *_args, **_kwargs: queue_admission.AWAITING_UPSTREAM_PHASE,
    )
    return pointer


@pytest.mark.parametrize(
    ("landed", "waiting"),
    [("§8", "s8"), ("s8", "§8")],
)
def test_section_spellings_release_in_both_directions(
    crew_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    landed: str,
    waiting: str,
) -> None:
    _candidate(monkeypatch, project="sample", ref=f"plan#{waiting}")

    result = promotion._release_waiters_of_the_landed_section(
        {"project": "sample", "node": {"plan": "plan", "section": landed}}
    )

    assert result["candidates"] == ["r-waiter"]


def test_project_qualified_wait_matches_the_promoted_project(
    crew_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _candidate(monkeypatch, project="sample", ref="sample:plan#s8")

    result = promotion._release_waiters_of_the_landed_section(
        {"project": "sample", "node": {"plan": "plan", "section": "§8"}}
    )

    assert result["candidates"] == ["r-waiter"]


@pytest.mark.parametrize(
    ("project", "ref"),
    [("sample", "other-plan#s8"), ("other-project", "plan#s8")],
)
def test_other_plan_or_project_is_not_released(
    crew_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    project: str,
    ref: str,
) -> None:
    _candidate(monkeypatch, project=project, ref=ref)

    result = promotion._release_waiters_of_the_landed_section(
        {"project": "sample", "node": {"plan": "plan", "section": "§8"}}
    )

    assert result["candidates"] == []


def test_another_pending_wait_keeps_the_candidate_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = waits.chain_repo.__wrapped__(tmp_path, monkeypatch)
    waits._declare_wait_refs(
        repository,
        section="s3",
        refs=[f"{waits.PLAN}#s1", f"{waits.PLAN}#s2"],
    )
    waits._queue_chain(repository)
    waits._declare(repository, "s1")

    result = queue_admission._release_waiters(
        [f"{waits.PLAN}#§1"], project=waits.PROJECT
    )

    assert "r-n3" in result["candidates"]
    assert "r-n3" not in result["promoted"]
    assert waits._phase_of(repository, "s3") == queue_admission.AWAITING_UPSTREAM_PHASE


def test_discard_still_releases_a_run_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = waits.chain_repo.__wrapped__(tmp_path, monkeypatch)
    waits._declare_wait_refs(repository, section="s3", refs=["run:r-n1"])
    waits._queue_chain(repository)

    released = promotion.discard("r-n1")

    assert released["released"]["candidates"] == ["r-n3"]
    assert waits._phase_of(repository, "s3") == queue_admission.BLOCKED_UPSTREAM_PHASE
