"""A review refused for unchanged inputs is not composed again.

A sweep composes a review for every delivered run awaiting one, and that
composition asks questions whose answers move only when the run, its records or
its lane do: a budget verdict over the lane's whole run history, a roster
snapshot, a scope read. A lane that refuses every dispatch therefore costs one
composition per awaiting run per sweep, for a refusal whose reason has not
changed since the previous pass.

The property is that the composition is spent once per set of inputs:

- with fifty delivered runs awaiting review and a lane that refuses every
  dispatch, the count keeps climbing on a first sweep and stops on the second;
- admitting the lane moves the admission the refusal rested on, so the third
  sweep composes all fifty and dispatches them;
- filing one run's review record moves that run's records and no other's, so
  the next sweep recomposes exactly that run.

The negative control is the base revision, where the second sweep composes all
fifty: nothing is remembered there, so every sweep pays for every refusal.
"""

from __future__ import annotations

import copy
import importlib
import json
import os
import time
from pathlib import Path

import pytest

from reckon.crew import recovery, review, runs
from reckon.crew import recovery_review_dispatch as reflex_dispatch
from reckon.crew.node import CrewError
from tests.conftest import real_store_is_not_a_fixture_target
from tests.helpers.git import git
from tests.test_a_queued_run_is_admitted import (
    CONFIG,
    PROJECT,
    SESSION,
    _lane_document,
    _launcher,
    admit_repo,  # noqa: F401 - shared temporary repository fixture
)

dispatch_module = importlib.import_module("reckon.crew.dispatch")
preparation_module = importlib.import_module("reckon.crew.dispatching.preparation")

RUN_COUNT = 50
RUNS = [f"r-refused-composition-{index:02d}" for index in range(RUN_COUNT)]
REFUSAL = "the review lane refused the composed review"
BUDGET_REFUSAL = "the lane's budget verdict holds no room for the review"
CLAIM_REFUSAL = "the review is held by another run's claim on the same path"
HOLDER = "r-holder-of-the-reviewed-path"


@pytest.fixture(autouse=True)
def no_test_reads_another_tests_refusal() -> None:
    """The memory of refused compositions is process-wide, so it is emptied.

    The seam is named directly rather than probed for: a revision where the
    memory is missing under a different name must fail here, because a fixture
    that quietly did nothing would let every test below pass over a broken
    exclusion.
    """
    with reflex_dispatch._REFUSED_COMPOSITIONS_LOCK:
        reflex_dispatch._REFUSED_COMPOSITIONS.clear()


def _age_remembered_refusals(seconds: float) -> None:
    """Move every remembered refusal's composition back by ``seconds``."""
    with reflex_dispatch._REFUSED_COMPOSITIONS_LOCK:
        for run_id, (identity, report, composed_at) in tuple(
            reflex_dispatch._REFUSED_COMPOSITIONS.items()
        ):
            reflex_dispatch._REFUSED_COMPOSITIONS[run_id] = (
                identity,
                report,
                composed_at - seconds,
            )


@pytest.fixture()
def delivered(
    admit_repo: Path,  # noqa: F811 - the shared repository fixture, as an argument
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Fifty delivered runs awaiting review, and a lane that refuses them."""
    base = git(admit_repo, "rev-parse", "HEAD")
    source = admit_repo / "reckon" / "signal.py"
    source.parent.mkdir()
    source.write_text("SIGNAL = 1\n", encoding="utf-8")
    git(admit_repo, "add", "reckon/signal.py")
    git(
        admit_repo,
        "commit",
        "-qm",
        "test: record signal",
        "-m",
        "Provide runtime source for review.",
    )
    head = git(admit_repo, "rev-parse", "HEAD")
    lane = _lane_document(tmp_path, worker_slots=0)
    for run_id in RUNS:
        manifest = runs.crew_home() / "manifests" / f"{run_id}.md"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            f"node: delivered, 0\nstatus: complete\ncommits: [{head}]\n",
            encoding="utf-8",
        )
        stamp = time.time() - 30
        os.utime(manifest, (stamp, stamp))
        runs._write_json(
            runs.pointer_path(run_id),
            {
                "run_id": run_id,
                "project": PROJECT,
                "session": SESSION,
                "repo": str(admit_repo),
                "worktree": str(admit_repo),
                "base_sha": base,
                "backend": "clive",
                "launch": "cli",
                "phase": "complete",
                "process_alive": False,
                "role": "implement",
                "manifest_path": str(manifest),
                "node": {
                    "id": f"delivered-{run_id}",
                    "plan": "plan-a",
                    "section": "dispatch",
                    "time_budget": "20m",
                    "write_paths": ["reckon/signal.py"],
                },
            },
        )
    config = copy.deepcopy(CONFIG)
    config["roles"]["review"] = {}
    config["fences"]["unreconciled_run_grace"] = "1s"
    config["backends"]["clive"]["lane_document"] = str(lane)
    monkeypatch.setattr(
        dispatch_module,
        "_create_worktree",
        lambda *a, **k: {"path": str(admit_repo), "base": head, "base_sha": head},
    )
    yield {"config": config, "repository": admit_repo, "head": head, "lane": lane}
    real_store_is_not_a_fixture_target(runs.crew_home())


def _compositions(monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], dict]:
    """Count the compositions the sweep spends, and refuse them until admitted.

    The count is taken at the call every composition ends in, so a pass that
    answers from what it remembers is counted as zero rather than as a cheap
    refusal. Admitting the lane is one flag, because this stand-in *is* the
    admission the sweep meets, reached through the facade the composition calls.
    """
    state = {"admits": False, "claim": None, "reason": REFUSAL}
    composed: list[str] = []
    real_dispatch = preparation_module.dispatch

    def stand_in(**kwargs):
        composed.append(str(kwargs["node"].id))
        if state["claim"]:
            refusal = CrewError(CLAIM_REFUSAL)
            refusal.run_id = state["claim"]["run_id"]
            refusal.claimed_path = state["claim"]["path"]
            raise refusal
        if not state["admits"]:
            raise CrewError(state["reason"])
        return real_dispatch(**kwargs)

    monkeypatch.setattr(dispatch_module, "dispatch", stand_in)
    assert preparation_module.dispatch is stand_in
    return composed, state


def _sweep(config: dict, recorded: list) -> dict:
    return recovery.dispatch_awaiting_reviews(
        project=PROJECT,
        session=SESSION,
        config=config,
        launcher=_launcher(recorded),
    )


def test_only_a_changed_input_recomposes_a_refused_review(delivered, monkeypatch):
    config = delivered["config"]
    composed, state = _compositions(monkeypatch)
    recorded: list = []

    first = _sweep(config, recorded)

    assert len(composed) == RUN_COUNT, composed
    assert len(first.get("refused") or ()) == RUN_COUNT, first
    assert not first.get("dispatched"), first

    second = _sweep(config, recorded)

    # Nothing about any run moved, so nothing is composed again — and the
    # refusals are still reported, because the run is still owed its review.
    assert len(composed) == RUN_COUNT, composed
    assert len(second.get("refused") or ()) == RUN_COUNT, second
    assert not second.get("dispatched"), second
    assert all(report.get("settled_refusal") for report in second["refused"]), second

    state["admits"] = True
    delivered["lane"].write_text(
        delivered["lane"]
        .read_text(encoding="utf-8")
        .replace('"worker_slots": 0', '"worker_slots": 10'),
        encoding="utf-8",
    )

    third = _sweep(config, recorded)

    assert len(composed) == 2 * RUN_COUNT, composed
    assert len(composed[-RUN_COUNT:]) == RUN_COUNT, composed
    assert len(third.get("dispatched") or ()) == RUN_COUNT, third
    assert len(recorded) == RUN_COUNT, recorded


def test_one_runs_review_record_recomposes_only_that_run(delivered, monkeypatch):
    config = delivered["config"]
    composed, _state = _compositions(monkeypatch)
    recorded: list = []

    assert len(_sweep(config, recorded)["refused"]) == RUN_COUNT
    assert len(composed) == RUN_COUNT

    changed = RUNS[7]
    record_path = review.review_path(PROJECT, changed, reviewed_head_sha="b" * 40)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(
        json.dumps(
            {
                "reviewed_run_id": changed,
                "reviewed_head_sha": "b" * 40,
                "status": "complete",
            }
        ),
        encoding="utf-8",
    )

    third = _sweep(config, recorded)

    recomposed = composed[RUN_COUNT:]
    assert len(recomposed) == 1, composed
    assert changed in recomposed[0], recomposed
    assert len(third.get("refused") or ()) == RUN_COUNT, third


def test_a_refusal_naming_another_runs_claim_is_composed_again(delivered, monkeypatch):
    """A claim is another run's state, so it outlives none of this run's inputs.

    The pointer, the records and the lanes' admission are unchanged across the
    release, so a refusal remembered against them would answer for ever and the
    run would never be reviewed. The sweep composes again and asks the holder's
    own pointer instead, which is what the retry sees when the claim clears.
    """
    config = delivered["config"]
    composed, state = _compositions(monkeypatch)
    recorded: list = []
    state["claim"] = {"run_id": HOLDER, "path": "reckon/signal.py"}

    first = _sweep(config, recorded)

    assert len(composed) == RUN_COUNT, composed
    assert len(first.get("refused") or ()) == RUN_COUNT, first

    state["claim"] = None

    second = _sweep(config, recorded)

    assert len(composed) == 2 * RUN_COUNT, composed
    assert all(
        not report.get("settled_refusal") for report in second.get("refused") or ()
    ), second

    state["admits"] = True
    delivered["lane"].write_text(
        delivered["lane"]
        .read_text(encoding="utf-8")
        .replace('"worker_slots": 0', '"worker_slots": 10'),
        encoding="utf-8",
    )

    third = _sweep(config, recorded)

    assert len(composed) == 3 * RUN_COUNT, composed
    assert len(third.get("dispatched") or ()) == RUN_COUNT, third


def test_a_budget_refused_review_is_recomposed_once_the_bound_passes(
    delivered, monkeypatch
):
    """A hold the memory cannot see costs one composition a minute, not none.

    The lane's budget verdict is a projection over history the identity does
    not watch, so a review refused on it would be answered from memory for
    ever. The refusal is trusted for the bound and no longer: a sweep in the
    same minute composes nothing, and the first sweep past the bound composes
    the run again, so the verdict the hold names is asked afresh.
    """
    config = delivered["config"]
    composed, state = _compositions(monkeypatch)
    recorded: list = []
    state["reason"] = BUDGET_REFUSAL

    first = _sweep(config, recorded)

    assert len(composed) == RUN_COUNT, composed
    assert len(first.get("refused") or ()) == RUN_COUNT, first

    second = _sweep(config, recorded)

    assert len(composed) == RUN_COUNT, composed
    assert all(report.get("settled_refusal") for report in second["refused"]), second

    _age_remembered_refusals(reflex_dispatch._REFUSED_COMPOSITION_RETRY_SECONDS + 1)

    third = _sweep(config, recorded)

    assert len(composed) == 2 * RUN_COUNT, composed
    assert all(
        not report.get("settled_refusal") for report in third.get("refused") or ()
    ), third
    assert len(third.get("refused") or ()) == RUN_COUNT, third
