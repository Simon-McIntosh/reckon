"""A run whose reviewed head is already on the primary branch is not landed again."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon.crew import (
    landing_measure,
    promotion_gate,
    promotion_requests,
    queue_landing,
    review_landing,
    runs,
)
from tests.test_the_landing_pass_measures_and_lands import PROJECT, RUN, _accept
from tests.test_the_landing_pass_measures_and_lands import delivery as imported_delivery

delivery_case = imported_delivery


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


class _Tree:
    """A prepared base checkout, for the base-arm reader the pass calls."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def __enter__(self) -> Path:
        return self._path

    def __exit__(self, *_exc) -> None:
        return None


class _Counters:
    """The two things a settled delivery must never cost."""

    def __init__(self) -> None:
        self.promotion_requests = 0
        self.gate_replays = 0


@pytest.fixture()
def settled_delivery(delivery_case, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The delivered run, checked out in its own worktree and reviewed."""
    _accept(base=str(delivery_case["base"]), head=str(delivery_case["head"]))
    repo = Path(delivery_case["repo"])
    # A landing of the run's head has already happened and the branch moved on
    # past it, which is the shape a re-request comes from.
    worktree = tmp_path / "worker-tree"
    _git(repo, "commit", "--allow-empty", "-q", "-m", "chore: a later landing")
    _git(repo, "worktree", "add", "--detach", str(worktree), str(delivery_case["head"]))
    pointer = runs.read_pointer(RUN)
    runs._write_json(runs.pointer_path(RUN), {**pointer, "worktree": str(worktree)})
    # The landing declaration and its push are the settlement's own business
    # and are exercised elsewhere; a checkpoint settles in this repository
    # without an origin to publish to.
    monkeypatch.setattr(review_landing, "finish_landing", lambda *a, **k: "")
    # The ordinary landing path reads the base checkout through git; the
    # fixture's own repository stands in for it so a pass that does not settle
    # reaches its measurement instead of failing on a missing repository.
    monkeypatch.setattr(
        landing_measure, "base_checkout", lambda repo, sha, timeout=0.0: _Tree(repo)
    )
    counters = _Counters()

    def counting_promotion(*_args, **_kwargs):
        counters.promotion_requests += 1
        return {"phase": "pending", "kind": "command"}

    def counting_measure(*_args, head_sha: str = "", **_kwargs):
        counters.gate_replays += 1
        return landing_measure.Measurement(
            command="true",
            suite={},
            population={},
            head=head_sha,
            project=PROJECT,
        )

    monkeypatch.setattr(promotion_requests, "request_promotion", counting_promotion)
    monkeypatch.setattr(landing_measure, "measure_run", counting_measure)
    return {**delivery_case, "worktree": worktree, "counters": counters}


def test_a_delivered_head_on_the_primary_branch_is_settled_without_a_landing(
    settled_delivery,
) -> None:
    counters = settled_delivery["counters"]
    passes = [queue_landing.land_once() for _ in range(3)]

    assert counters.promotion_requests == 0
    assert counters.gate_replays == 0
    assert runs.pointer_path(RUN).exists() is False
    assert passes[0]["skipped"] == [{"run_id": RUN, "status": "landed-elsewhere"}]
    for later in passes[1:]:
        assert RUN not in later["landed"]


def test_a_head_carried_but_a_change_dropped_is_reported_and_not_settled(
    settled_delivery,
) -> None:
    repo = Path(settled_delivery["repo"])
    path = "reckon/crew/queue_landing.py"
    _git(repo, "rm", "-q", path)
    _git(repo, "commit", "-q", path, "-m", "chore: drop the delivered file")
    # The reviewed head is still an ancestor, and the path its delivery wrote
    # is gone from the branch that carries it.
    result = queue_landing.land_once()

    assert runs.pointer_path(RUN).exists()
    assert settled_delivery["counters"].promotion_requests == 0
    assert all(
        entry.get("status") != "landed-elsewhere" for entry in result.get("skipped", [])
    )
    reported = [entry for entry in result["obligations"] if entry["run_id"] == RUN]
    assert reported, result
    assert path in reported[0]["guard"]


def test_a_head_that_added_nothing_is_not_settled(
    settled_delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A head that is an ancestor because the run added nothing is not a landed
    # delivery: its base is its head, and the ordinary path owns it.
    head = str(settled_delivery["head"])
    repo = Path(settled_delivery["repo"])
    pointer = runs.read_pointer(RUN)
    verdict, _detail = queue_landing._primary_carries_the_delivery(
        {**pointer, "base_sha": head}, head, _git(repo, "rev-parse", "HEAD")
    )

    assert verdict == "not-carried"


def test_a_replay_that_exits_with_no_log_records_its_own_failure(
    settled_delivery,
) -> None:
    repo = Path(settled_delivery["repo"])

    def replay_exits_nonzero(argv, **_kwargs):
        return SimpleNamespace(returncode=3, stdout="", stderr="")

    report = promotion_gate.rerun_gate_at_integrated_revision(
        repository=repo,
        gate_check={"command": "exit 3"},
        integrated_revision="HEAD",
        run=replay_exits_nonzero,
    )

    # The run's own verdict is its base gate, which the replay cannot rewrite.
    assert report["base_verdict"] == "passed"
    assert report["integrated_verdict"] != "failed"
    assert report["log_path"] is None
    assert "3" in str(report.get("replay_failure") or "")
    assert report["reason"]
    assert report["ok"] is False
