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


def _invoke(
    repo: Path,
    *,
    node: str,
    done_when: str = placement.DONE_WHEN,
    dry_run: bool = False,
):
    arguments = [
        *backend_tests._arguments(repo, node=node, dry_run=dry_run),
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


def _admission_path(
    repo: Path,
    config: dict,
    monkeypatch: pytest.MonkeyPatch,
    *,
    lane: str = "",
    refused: str = LOCAL,
) -> dict:
    """What the admission pass does with a request its own lane refused."""
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
        "backend": refused,
        "node": placement._node("oversized").as_dict(),
        "dispatch_options": {},
    }
    outcome = dict(
        queue_admission._route_competence_refusal(
            pointer,
            _refusal(),
            config_for=lambda _pointer: config,
            lane=lane,
        )
    )
    outcome["moved_to"] = captured.get("backend", "")
    return outcome


def _admission_destination(
    repo: Path,
    config: dict,
    monkeypatch: pytest.MonkeyPatch,
    *,
    lane: str = "",
    refused: str = LOCAL,
) -> str:
    """The lane the admission pass moves a refused request onto."""
    return _admission_path(repo, config, monkeypatch, lane=lane, refused=refused)[
        "moved_to"
    ]


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
    assert snapshot.widest_fitting_candidate(candidates, LOCAL).backend == CODEX

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
        snapshot.widest_fitting_candidate([broken, wide], "elsewhere")


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

    result = _invoke(dispatch_repo, node="unfittable")
    assert created == []
    payload = _payload(result)

    assert result.exit_code == 5, result.output
    assert payload["error"] == "competence-refusal"
    competence = payload["competence"]
    assert competence["reason"] == "context-window-exceeded"
    assert competence["estimated_tokens"] > competence["window_tokens"]
    assert competence["shortfall_tokens"] == (
        competence["estimated_tokens"] - competence["window_tokens"]
    )
    # This lane's own instruction chain fits: the claude chain it would load is
    # 50,392 tokens against a 100,000 window, while the request measured without a
    # lane is 250,392. The refusal must therefore be stated from the measure that
    # refused it rather than read off the resolution's verdict, whose context block
    # reports this lane within its window and carries no shortfall -- reading that
    # verdict as the refusal leaves CompetenceLimit on its horizon branch and
    # raises KeyError instead of the exit-5 context refusal.
    assert competence["estimated_tokens"] == 250_392
    assert competence["window_tokens"] == 100_000
    assert competence["shortfall_tokens"] == 150_392
    assert competence["context"]["allowed"] is False
    assert competence["context"]["shortfall_tokens"] == 150_392
    # The shape a dry run answers with is kept: the fields the verdict holds
    # beside its own context measure still reach the reader.
    assert competence["agent_key"]
    assert competence["cache_status"]
    assert competence["context"]["estimated_tokens"] == competence["estimated_tokens"]
    assert competence["context"]["window_tokens"] == competence["window_tokens"]
    assert competence["context"]["shortfall_tokens"] == competence["shortfall_tokens"]
    assert str(competence["shortfall_tokens"]) in payload["detail"]
    assert str(competence["window_tokens"]) in payload["detail"]
    assert created == []
    assert runs.list_live(project=PROJECT, phase="queued") == []


def test_a_lane_whose_own_chain_fits_refuses_the_request_census(
    dispatch_repo: Path,
    scan: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # The two measures can disagree: the fallback censuses the request with no
    # backend settings, while the fit check weighs the resolved lane's own
    # instruction chain. Here the lane's chain leaves it 250,392 tokens against
    # a 300,000 window, so the fit check calls the lane fit, and the request
    # census measured through the codex chain does not fit any lane. The
    # refusal must still be the context refusal with a shortfall: read off the
    # store's verdict, this lane's payload carries no shortfall at all and
    # CompetenceLimit answers it on its horizon branch, which raises KeyError
    # instead of exiting 5.
    monkeypatch.setenv("HOME", str(_codex_heavy_home(tmp_path)))
    config = _configure(
        dispatch_repo,
        monkeypatch,
        local_window=300_000,
        extra_windows={CODEX: 150_000, "alpha": 190_000},
    )
    config["backends"][LOCAL].update({"launch": "cli", "command": "claude"})
    placement._stub_fallback(monkeypatch)

    result = _invoke(dispatch_repo, node="census-disagrees")
    payload = _payload(result)
    competence = payload["competence"]

    assert result.exit_code == 5, result.output
    assert payload["error"] == "competence-refusal"
    assert competence["reason"] == "context-window-exceeded"
    assert competence["window_tokens"] == 300_000
    assert competence["estimated_tokens"] > competence["window_tokens"]
    assert competence["shortfall_tokens"] == (
        competence["estimated_tokens"] - competence["window_tokens"]
    )
    assert str(competence["shortfall_tokens"]) in payload["detail"]


def test_the_refused_lane_is_never_chosen_even_when_it_is_the_widest() -> None:
    # Both callers used to skip the lane that refused the request themselves,
    # so the rule's own answer could be the refused lane and each caller's skip
    # was the only thing standing between the request and a move onto it. The
    # rule excludes it, and the path is asserted through the admission caller.
    refused = {
        "backend": "refuser",
        "reasons": [],
        "context": {"window_tokens": 900_000},
    }
    narrow = {"backend": "narrow", "reasons": [], "context": {"window_tokens": 100_000}}

    assert (
        snapshot.widest_fitting_candidate([refused, narrow], "refuser")["backend"]
        == "narrow"
    )
    assert snapshot.widest_fitting_candidate([refused, narrow], "elsewhere")[
        "backend"
    ] == ("refuser")


def test_the_refused_widest_lane_is_not_chosen_by_the_admission_pass(
    dispatch_repo: Path, scan: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The refused lane is the widest candidate carrying no reason and the only
    # one, which is the case a first-usable rule answers with that lane: the
    # request would be moved back onto the lane that refused it. The other two
    # lanes are narrower than the request, so they carry the fit refusal and are
    # excluded on their own account.
    config = _configure(
        dispatch_repo,
        monkeypatch,
        local_window=100_000,
        extra_windows={CODEX: 900_000, "alpha": 50_000},
    )
    outcome = _admission_path(dispatch_repo, config, monkeypatch, refused=CODEX)

    assert outcome["moved_to"] == ""
    assert outcome["queued_on"] == ""
    assert "shortfall" in outcome["obligation"].lower()


def test_the_admission_pass_hands_the_rule_the_pointers_own_backend(
    dispatch_repo: Path, scan: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The exclusion has to be the pointer's own backend, not whatever lane the
    # pass happens to dislike: alpha is the widest candidate here, so a rule
    # handed any other name would return it and move the request onto the lane
    # that refused it.
    refused: list[str] = []
    real = snapshot.widest_fitting_candidate

    def spy(candidates, refused_backend):
        refused.append(refused_backend)
        return real(candidates, refused_backend)

    monkeypatch.setattr(snapshot, "widest_fitting_candidate", spy)
    config = _configure(
        dispatch_repo,
        monkeypatch,
        local_window=100_000,
        extra_windows={CODEX: 400_000, "alpha": 900_000},
    )
    destination = _admission_destination(
        dispatch_repo, config, monkeypatch, refused="alpha"
    )

    assert refused == ["alpha"]
    assert destination == CODEX


def test_the_refusal_is_the_payload_its_dry_run_previews(
    dispatch_repo: Path, scan: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The real refusal and the preview of it are one payload, so a reader who
    # ran the dry run has already seen every field the dispatch answers with:
    # the lane's agent key, the cache status it was judged against, the nested
    # fit verdict, and the estimate, window and shortfall that verdict states.
    _configure(
        dispatch_repo,
        monkeypatch,
        local_window=230_144,
        extra_windows={CODEX: 190_000},
    )
    placement._stub_fallback(monkeypatch)

    real = _payload(_invoke(dispatch_repo, node="one-payload"))
    preview = _payload(_invoke(dispatch_repo, node="one-payload", dry_run=True))

    assert real["error"] == preview["error"] == "competence-refusal"
    assert real["competence"] == preview["competence"]
    verdict = real["competence"]
    assert verdict["shortfall_tokens"] == verdict["context"]["shortfall_tokens"]


def test_the_fallback_refusal_renders_routing_s_own_detail(
    dispatch_repo: Path, scan: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The refusal is one payload built in one place, so the detail a dispatch
    # answers with is the detail routing renders for the same lane and the same
    # figures -- the contributing files and how the largest of them arrived --
    # rather than a shorter sentence assembled beside it. The preview answers
    # through the competence verdict, which builds its detail from the same fit
    # check, so the two strings are compared directly.
    _configure(
        dispatch_repo,
        monkeypatch,
        local_window=230_144,
        extra_windows={CODEX: 190_000},
    )
    placement._stub_fallback(monkeypatch)

    live = _payload(_invoke(dispatch_repo, node="detail-parity"))
    preview = _payload(_invoke(dispatch_repo, node="detail-parity", dry_run=True))
    live_detail = live["competence"]["detail"]

    assert live["error"] == "competence-refusal"
    assert live["competence"]["shortfall_tokens"] == (
        live["competence"]["estimated_tokens"] - live["competence"]["window_tokens"]
    )
    # The detail is routing's own rendering of the fit check: the figures, then
    # the inputs that carried the estimate, and how the largest of them arrived.
    assert live_detail.startswith("context-window-exceeded: estimated ")
    assert str(live["competence"]["estimated_tokens"]) in live_detail
    assert str(live["competence"]["window_tokens"]) in live_detail
    assert "corpus.txt" in live_detail
    assert "which arrived as a" in live_detail
    assert live_detail == preview["competence"]["detail"]
