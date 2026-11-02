"""A request too large for the local lane is never placed there.

A pick that times out falls back to the configured default, and so does the
deterministic route. Neither used to weigh that lane's context window, so an
oversized request was queued on a lane that could only refuse it at launch.
These tests fix the placement: the fallback applies the router's own fit check,
moves the request to the fitting lane with the largest window, and refuses with
the shortfall when no permitted lane holds it.
"""

from __future__ import annotations

import importlib
import json
from copy import deepcopy
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli, crew_dispatch_commands
from reckon.crew import queue_admission, runs
from reckon.crew.node import TaskNode
from reckon.crew.picker import snapshot
from tests import test_dispatch_names_its_backend as backend_tests

dispatch_module = importlib.import_module("reckon.crew.dispatch")
dispatch_plan = importlib.import_module("reckon.crew.dispatch_plan")

LOCAL = "clive"
CODEX = "beta"
PROJECT = "proj"
SESSION = "session"
# A node is oversized when its own brief loads more than a lane's window, so the
# brief declares one input big enough that the estimate routing measures -- the
# standing chain plus that file -- clears the windows these tests declare. The
# file is sized rather than the estimate stubbed, because a stubbed estimate
# answers the preview and the launch from two different figures, and the two
# paths must agree on the placement.
CORPUS_BYTES = 700_000
DONE_WHEN = (
    "the declared input corpus.txt is read; pytest -q "
    "tests/test_a_request_too_large_for_local_is_never_placed_there.py reports "
    "four placement cases"
)

WINDOWS = {
    "alpha": 200_000,
    "gamma": 180_000,
    "unusable": 150_000,
}


@pytest.fixture()
def dispatch_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = backend_tests.dispatch_repo.__wrapped__(tmp_path, monkeypatch)
    (repo / "corpus.txt").write_text("x" * CORPUS_BYTES, encoding="utf-8")
    return repo


@pytest.fixture()
def scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """A candidate scan whose lanes are measured on their declared windows.

    The scan's own structure — the per-backend fit, the exclusion reasons and
    the context block — runs for real, and so do routing's context-fit and
    competence verdicts: the node carries a genuinely oversized input, so every
    lane is weighed on the figure the request itself measures. Only the facts
    that need a live fleet (serving probes, the lane gate, the account view) are
    fixed.
    """
    monkeypatch.setattr(
        snapshot, "_dispatch_lane_gate", lambda backend: {"state": "open"}
    )
    monkeypatch.setattr(
        snapshot, "_lane", lambda *a: (3, {"waiting": 0}, {"held": False})
    )
    monkeypatch.setattr(
        snapshot, "budget_view", lambda *a, **k: {"backends": [], "groups": []}
    )
    monkeypatch.setattr(
        snapshot.resumption,
        "probe_lane_availability",
        lambda *a, **k: {"status": "served"},
    )
    # The routed pick withdraws a lane nothing spawns, which would remove every
    # lane this fixture's dispatch does run in-process. The withdrawal is a fact
    # about which lanes a spawned dispatch can reach, not about their windows,
    # so it is lifted here and every lane is weighed on its window alone.
    monkeypatch.setattr(snapshot.recovery, "IN_HARNESS_LAUNCH", "test-no-lane")


def _lane_document(path: Path, *, slots: float) -> Path:
    path.write_text(
        json.dumps(
            {
                "headroom": 4,
                "admission": {
                    "worker_slots": slots,
                    "observed_seconds": 900,
                    "sessions": {SESSION: {"worker_slots": slots}},
                    "verdict": "open" if slots else "congested",
                    "headroom": 4,
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _configure(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    local_window: int,
    extra_windows: dict[str, int] | None = None,
    local_slots: float = 4,
) -> dict:
    windows = {LOCAL: local_window, **WINDOWS, **(extra_windows or {})}
    config = deepcopy(backend_tests.CONFIG)
    config["local_backend"] = LOCAL
    config["default_backend"] = LOCAL
    config["backends"][LOCAL]["lane_document"] = str(
        _lane_document(repo.parent / "local-lane.json", slots=local_slots)
    )
    for name, window in windows.items():
        config["backends"][name]["usable_input_window"] = window
    monkeypatch.setattr(
        crew_dispatch_commands, "_resolved_flight", lambda *_a, **_k: config
    )
    monkeypatch.setattr(
        crew_dispatch_commands, "_model_availability_refusal", lambda *_a, **_k: None
    )
    return config


def _node(name: str) -> TaskNode:
    return TaskNode(
        id=name,
        goal="place the request on a lane whose window holds it",
        plan="plan-a",
        section="dispatch",
        role="implement",
        spec_level="exact",
        done_when=DONE_WHEN,
        write_paths=["result.json"],
        time_budget="20m",
    )


def _fallback_selection() -> dict:
    """The selection a pick records when it ran past its bound."""
    return {
        "action": "fallback",
        "backend": None,
        "family": None,
        "model": None,
        "effort": None,
        "probabilities": {},
        "confidence": None,
        "jev_model": "jev-test",
        "fallback_reason": "timeout",
        "offered": [],
        "excluded": [],
        "comment": "",
    }


def _stub_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    selection = _fallback_selection()

    def stub(**_kwargs):
        return dict(selection)

    monkeypatch.setattr(dispatch_module, "dispatch_picker_selection", stub)


def _invoke(repo: Path, *, node: str, dry_run: bool = False):
    arguments = backend_tests._arguments(repo, node=node, dry_run=dry_run)
    # The shared argument list carries a brief that declares no input; this
    # file's nodes are oversized because their brief loads one.
    arguments[arguments.index("--done-when") + 1] = DONE_WHEN
    arguments = [*arguments, "--route", "picker"]
    if not dry_run:
        arguments.append("--no-watch")
    return CliRunner().invoke(cli.main, arguments)


def _payload(result) -> dict:
    return backend_tests._payload(result)


def test_oversized_fallback_resolves_the_unbounded_lane_with_a_context_fit_route(
    dispatch_repo: Path, scan: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(dispatch_repo, monkeypatch, local_window=230_144)
    _stub_fallback(monkeypatch)

    result = _invoke(dispatch_repo, node="oversized-fallback")
    payload = _payload(result)

    assert result.exit_code == 0, result.output
    assert payload["backend"] == CODEX
    assert payload["route_fallback"] == {
        "from_backend": LOCAL,
        "to_backend": CODEX,
        "reason": "context-fit",
    }
    # The picker's own answer still rides the record, so a reader sees that the
    # placement came from the fallback rather than from Jev.
    assert payload["picker_selection"]["action"] == "fallback"


def test_the_oversized_fallback_is_placed_on_the_fitting_lane_when_local_is_full(
    dispatch_repo: Path, scan: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A full local lane is the case the check exists for: the fallback would
    # otherwise queue the oversized request there, and the context refusal would
    # surface only when admission launched it.
    config = _configure(dispatch_repo, monkeypatch, local_window=230_144, local_slots=0)
    monkeypatch.setattr(
        queue_admission, "_dispatch_fleet_gate", lambda: {"state": "open"}
    )
    record = dispatch_module.dispatch(
        node=_node("oversized-queued"),
        project=PROJECT,
        repo=dispatch_repo,
        config=config,
        session=SESSION,
        route="picker",
        picker_selection=_fallback_selection(),
        watch_required=False,
        watch_override=True,
    )

    # The request is placed on the lane that can run it, so nothing of it waits
    # on the lane whose window it exceeds.
    assert record["backend"] == CODEX
    assert record["route_fallback"] == {
        "from_backend": LOCAL,
        "to_backend": CODEX,
        "reason": "context-fit",
    }
    assert runs.list_live(project=PROJECT, phase="queued") == []


def test_a_request_that_fits_the_local_lane_still_resolves_it(
    dispatch_repo: Path, scan: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(dispatch_repo, monkeypatch, local_window=400_000)
    _stub_fallback(monkeypatch)

    result = _invoke(dispatch_repo, node="fitting-fallback")
    payload = _payload(result)

    assert result.exit_code == 0, result.output
    assert payload["backend"] == LOCAL
    assert not payload.get("route_fallback")


def test_a_request_no_permitted_lane_holds_refuses_with_its_shortfall(
    dispatch_repo: Path, scan: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(
        dispatch_repo,
        monkeypatch,
        local_window=230_144,
        extra_windows={CODEX: 190_000},
    )
    _stub_fallback(monkeypatch)

    result = _invoke(dispatch_repo, node="unfittable")
    payload = _payload(result)

    assert result.exit_code != 0
    competence = payload["competence"]
    # The refusal names the distance to the lane's own window, so the reader is
    # told how much the node has to shrink by rather than which lane happened to
    # be tried first.
    assert competence["estimated_tokens"] > competence["window_tokens"]
    assert str(competence["shortfall_tokens"]) in payload["detail"]
    assert str(competence["window_tokens"]) in payload["detail"]
    assert runs.list_live(project=PROJECT, phase="queued") == []

    # The preview measures the same node against the same lane, so it refuses
    # with the same payload rather than a second description of the shortfall.
    dry_payload = _payload(_invoke(dispatch_repo, node="unfittable", dry_run=True))
    assert dry_payload["error"] == payload["error"]
    # The preview answers from the resolution's competence verdict, which carries
    # the same fit check as its context block; the live dispatch refuses from
    # that fit check itself. The two must name the same estimate, window and
    # shortfall, or the preview would be answering a different question from the
    # dispatch it previews.
    dry_competence = dry_payload["competence"]
    for field in ("estimated_tokens", "window_tokens", "shortfall_tokens"):
        assert dry_competence[field] == payload["competence"][field]
    assert (
        dry_competence["context"]["shortfall_tokens"]
        == (  # type: ignore[index]
            payload["competence"]["shortfall_tokens"]
        )
    )
