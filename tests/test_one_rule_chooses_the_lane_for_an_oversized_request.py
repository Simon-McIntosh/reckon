"""One rule chooses the lane for a request too large for its own.

Two paths place such a request: the admission pass, when a queued request's own
lane refuses it, and a dispatch whose picker answer never arrived. Each used to
choose for itself -- admission took the first candidate carrying no exclusion,
dispatch took the widest -- and neither read the same rule, so the same
candidates could resolve two different lanes. Dispatch's copy also read a
malformed window as an absent one, ranking a broken declaration above every real
window, and returned the oversized resolution unchanged when no candidate was
usable.

These tests fix one rule on both paths: the widest usable window among the
candidates the picker's own scan produced, unbounded above every declared
figure, refusing with the shortfall when nothing fits. Routing's context-fit and
competence verdicts run for real, on a request that is oversized because its
brief loads an input larger than the windows, so the figure the rule weighs is
the one a dispatch would weigh.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli
from reckon.crew import queue_admission, runs
from reckon.crew.node import CompetenceLimit, CrewError
from reckon.crew.picker import snapshot
from reckon.crew.picker.types import PickRequest
from tests import test_a_request_too_large_for_local_is_never_placed_there as placement
from tests import test_dispatch_names_its_backend as backend_tests

dispatch_module = importlib.import_module("reckon.crew.dispatch")

LOCAL = placement.LOCAL
CODEX = placement.CODEX
PROJECT = placement.PROJECT
SESSION = placement.SESSION


@pytest.fixture()
def dispatch_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = backend_tests.dispatch_repo.__wrapped__(tmp_path, monkeypatch)
    (repo / "corpus.txt").write_text("x" * placement.CORPUS_BYTES, encoding="utf-8")
    return repo


@pytest.fixture()
def scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """The candidate scan's live-fleet facts fixed; its fit judgements real."""
    placement.scan.__wrapped__(monkeypatch)


def _configure(repo: Path, monkeypatch: pytest.MonkeyPatch, **windows: object) -> dict:
    return placement._configure(repo, monkeypatch, **windows)


def _invoke(repo: Path, *, node: str, done_when: str = placement.DONE_WHEN):
    arguments = [
        *backend_tests._arguments(repo, node=node, dry_run=False),
        "--route",
        "picker",
        "--no-watch",
    ]
    arguments[arguments.index("--done-when") + 1] = done_when
    return CliRunner().invoke(cli.main, arguments)


def _codex_heavy_home(base: Path) -> Path:
    """A home whose codex instruction chain alone exceeds a lane's window.

    Two harnesses load different standing chains, so the request-level estimate
    -- measured without a lane -- and a lane's own verdict can disagree on one
    node. That disagreement is what an oversized request used to slip through:
    the lane's verdict was satisfied while the request's estimate was not.
    """
    home = base / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "AGENTS.md").write_text("x" * placement.CORPUS_BYTES)
    return home


def _payload(result) -> dict:
    return backend_tests._payload(result)


def _refusal() -> CompetenceLimit:
    return CompetenceLimit(
        {
            "allowed": False,
            "reason": "context-window-exceeded",
            "estimated_tokens": 250_000,
            "window_tokens": 230_144,
            "shortfall_tokens": 19_856,
        }
    )


def _admission_destination(
    repo: Path,
    config: dict,
    monkeypatch: pytest.MonkeyPatch,
    *,
    lane: str = "",
) -> str:
    """The lane the admission pass moves a refused request onto."""
    captured: dict[str, str] = {}

    def move(run_id: str, backend: str, **_kwargs) -> dict:
        captured["backend"] = backend
        return {"run_id": run_id, "backend": backend}

    monkeypatch.setattr(queue_admission, "move_queued_run", move)
    pointer = {
        "run_id": "r-test-oversized-request",
        "project": PROJECT,
        "repo": str(repo),
        "session": SESSION,
        "backend": LOCAL,
        "node": placement._node("oversized").as_dict(),
        "dispatch_options": {},
    }
    queue_admission._route_competence_refusal(
        pointer,
        _refusal(),
        config_for=lambda _pointer: config,
        lane=lane,
    )
    return captured.get("backend", "")


def test_both_paths_move_the_request_to_the_widest_fitting_lane(
    dispatch_repo: Path, scan: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Both lanes fit and beta is the wider one, so a rule that took the first
    # usable candidate would resolve alpha here and disagree with the dispatch.
    config = _configure(
        dispatch_repo,
        monkeypatch,
        local_window=230_144,
        extra_windows={"alpha": 300_000, CODEX: 400_000},
    )
    placement._stub_fallback(monkeypatch)

    result = _invoke(dispatch_repo, node="widest")
    payload = _payload(result)

    assert result.exit_code == 0, result.output
    assert payload["backend"] == CODEX
    assert payload["route_fallback"] == {
        "from_backend": LOCAL,
        "to_backend": CODEX,
        "reason": "context-fit",
    }
    assert _admission_destination(dispatch_repo, config, monkeypatch) == CODEX


def test_the_candidates_are_measured_by_routing_and_not_by_the_test(
    dispatch_repo: Path, scan: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The rule weighs the figure routing measures the request at, which is the
    # brief's declared input plus the standing chain -- not a number the test
    # supplied. The control is the same node without that input: the estimate
    # falls below the same window and the request stays on its own lane.
    config = _configure(dispatch_repo, monkeypatch, local_window=230_144)
    request = PickRequest(PROJECT, placement._node("measured"))
    candidates = snapshot.candidates(request, config, dispatch_repo)
    blocks = {candidate.backend: candidate.context for candidate in candidates}

    assert blocks[LOCAL]["estimated_tokens"] > 230_144
    assert blocks[CODEX]["window_tokens"] is None
    assert snapshot.widest_fitting_candidate(candidates).backend == CODEX

    (dispatch_repo / "corpus.txt").unlink()
    placement._stub_fallback(monkeypatch)
    payload = _payload(_invoke(dispatch_repo, node="fitting-brief"))

    assert payload["backend"] == LOCAL
    assert not payload.get("route_fallback")


@pytest.mark.parametrize("declared", [0, "not-a-number"])
def test_a_broken_window_raises_on_both_paths_rather_than_being_chosen(
    dispatch_repo: Path, scan: None, monkeypatch: pytest.MonkeyPatch, declared: object
) -> None:
    config = _configure(dispatch_repo, monkeypatch, local_window=230_144)
    config["backends"]["alpha"]["usable_input_window"] = declared
    placement._stub_fallback(monkeypatch)

    result = _invoke(dispatch_repo, node="broken-window")
    payload = _payload(result)

    assert result.exit_code != 0
    assert payload["error"] == "dispatch-refused"
    assert "input window" in payload["detail"]
    with pytest.raises(CrewError) as refused:
        _admission_destination(dispatch_repo, config, monkeypatch)
    assert "input window" in str(refused.value)


@pytest.mark.parametrize("declared", [0, "not-a-number"])
def test_the_rule_reads_a_broken_window_as_broken(declared: object) -> None:
    # The fault was a reader that returned "no window" for a broken one, so the
    # broken candidate ranked as unbounded above a real window. The rule must
    # refuse the declaration instead of choosing it.
    broken = {
        "backend": "broken",
        "reasons": [],
        "context": {"window_tokens": declared},
    }
    wide = {"backend": "wide", "reasons": [], "context": {"window_tokens": 100_000}}

    with pytest.raises(CrewError):
        snapshot.widest_fitting_candidate([broken, wide])


def test_no_usable_lane_refuses_with_the_shortfall_and_creates_no_worktree(
    dispatch_repo: Path,
    scan: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # The resolved lane's own verdict is satisfied here while the request's
    # estimate is not: its claude chain is small and the request is measured
    # against the codex chain. Reading that verdict as permission to keep the
    # request on the lane is exactly how an oversized request was placed on the
    # lane the fit check had just refused.
    monkeypatch.setenv("HOME", str(_codex_heavy_home(tmp_path)))
    config = _configure(
        dispatch_repo,
        monkeypatch,
        local_window=100_000,
        extra_windows={CODEX: 150_000, "alpha": 190_000},
    )
    config["backends"][LOCAL].update({"launch": "cli", "command": "claude"})
    placement._stub_fallback(monkeypatch)
    created: list[tuple] = []

    def refuse_to_create(*args, **_kwargs):
        created.append(args)
        raise AssertionError("a worktree was created for an unfittable request")

    monkeypatch.setattr(dispatch_module, "_create_worktree", refuse_to_create)

    result = _invoke(
        dispatch_repo,
        node="unfittable",
        done_when="pytest -q tests/test_one_rule_chooses_the_lane_for_an_oversized_request.py reports seven cases",
    )
    assert created == []
    payload = _payload(result)

    assert result.exit_code == 5, result.output
    assert payload["error"] == "competence-refusal"
    competence = payload["competence"]
    assert competence["reason"] == "context-window-exceeded"
    assert competence["estimated_tokens"] > competence["window_tokens"]
    assert str(competence["shortfall_tokens"]) in payload["detail"]
    assert str(competence["window_tokens"]) in payload["detail"]
    assert created == []
    assert runs.list_live(project=PROJECT, phase="queued") == []
