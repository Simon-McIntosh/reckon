"""A dispatch that loses a registration race outlives a winner that withdraws.

Two dispatches of overlapping paths can both publish a claim before either
reaches its admission check. The claim registered first owns the paths, and the
later one refuses on sight of it. That refusal strands the paths when the winner
then withdraws for an unrelated reason — a budget hold, a backend ceiling, a
crash, a refusal of its own — because the loser has already gone and nothing
else takes them.

So a dispatch that would refuse only because of an unlaunched racing winner
waits, bounded, re-reading that claim: if the winner launches it refuses naming
it, if the claim disappears or records a withdrawal it proceeds, and if the
bound expires with the winner still unlaunched it refuses saying so. A launched
peer and an established claim are never waited on.

Each case plants the peer claim the check reads and stubs the wait's clock and
re-read, so no real sleep runs and the winner's fate is decided in the test.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

import reckon.crew.dispatch_claims as dispatch_claims_module
from reckon import crew

dispatch_module = importlib.import_module("reckon.crew.dispatch")
runs_module = importlib.import_module("reckon.crew.runs")

PEER_RUN_ID = "r-20261001T04000000000000-peer-winner"
OWN_RUN_ID = "r-20261001T04000000001000-own-loser"
PEER_REGISTERED_AT = "2026-10-01T04:00:00Z"
# Ten seconds after the peer: orderable, well clear of the one-second
# resolution guard, and strictly the later arrival.
OWN_REGISTERED_AT = "2026-10-01T04:00:10Z"
CLAIMED_PATH = "src/claimed.py"


class _Clock:
    """A monotonic clock the wait measures against, advanced only by pause."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def pause(self, seconds: float) -> None:
        self.now += seconds


def _node() -> crew.TaskNode:
    return crew.TaskNode(
        id="node-loser",
        goal="keep the paths when a racing winner withdraws",
        plan="",
        section="s1",
        role="implement",
        spec_level="exact",
        done_when="the loser proceeds once the winner withdraws",
        write_paths=[CLAIMED_PATH],
        time_budget="20m",
        manifest_path="race-loser-manifest.md",
    )


def _peer_claim(
    repo: Path,
    *,
    launched: bool = False,
    binding: bool = True,
    registered_at: str = PEER_REGISTERED_AT,
) -> object:
    """The winner's published claim as the check that reads it sees it."""
    return dispatch_module._RepositoryScopeClaim(
        project="fixture",
        repository=repo,
        run_id=PEER_RUN_ID,
        node_id="node-winner",
        path=CLAIMED_PATH,
        absolute_path=repo / CLAIMED_PATH,
        declared_path=CLAIMED_PATH,
        binding=binding,
        registered_at=registered_at,
        launched=launched,
    )


@pytest.fixture(autouse=True)
def _no_mounted_projects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispatch_module, "mounted_repository_projects", dict)


def _check(repo: Path, claim: object) -> None:
    dispatch_module._raise_repository_scope_conflict(
        _node(),
        project="fixture",
        repo=repo,
        authority={"repositories": [repo]},
        claims=[claim],
        own_run_id=OWN_RUN_ID,
        own_registered_at=OWN_REGISTERED_AT,
    )


def _stub_wait(
    monkeypatch: pytest.MonkeyPatch,
    clock: _Clock,
    reread,
) -> None:
    monkeypatch.setattr(dispatch_claims_module, "_racing_clock", clock)
    monkeypatch.setattr(dispatch_claims_module, "_racing_pause", clock.pause)
    monkeypatch.setattr(dispatch_claims_module, "_racing_claim_current", reread)


# ── The loser waits on an unlaunched winner ─────────────────────────────────


def test_the_loser_proceeds_when_the_winner_withdraws(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The winner gives its claim back while the loser waits; the loser proceeds.

    The winner's claim disappearing — a refused dispatch unlinks its pointer —
    leaves the paths with the loser, which must not have withdrawn on sight of a
    claim that no longer exists.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _stub_wait(monkeypatch, _Clock(), lambda _run_id: None)

    # Nothing is raised: the winner has gone and the paths are this dispatch's.
    _check(repo, _peer_claim(repo))


def test_the_loser_refuses_naming_the_winner_that_launches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The winner reaches its admission while the loser waits; the loser refuses."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _stub_wait(
        monkeypatch,
        _Clock(),
        lambda _run_id: _peer_claim(repo, launched=True),
    )

    with pytest.raises(crew.ScopeConflict) as refusal:
        _check(repo, _peer_claim(repo))

    assert refusal.value.run_id == PEER_RUN_ID
    assert CLAIMED_PATH in str(refusal.value)


def test_the_loser_refuses_when_the_bound_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The winner neither launches nor withdraws; the loser refuses at the bound.

    The reason names the bound, so a reader can tell a wait that timed out from
    a refusal that never waited.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    clock = _Clock()
    _stub_wait(monkeypatch, clock, lambda _run_id: _peer_claim(repo))

    with pytest.raises(crew.ScopeConflict) as refusal:
        _check(repo, _peer_claim(repo))

    assert refusal.value.run_id == PEER_RUN_ID
    assert "not launched" in str(refusal.value)
    assert f"{dispatch_module.RACING_WINNER_WAIT_SECONDS:g}s" in str(refusal.value)
    # The wait really ran to its bound rather than refusing on sight.
    assert clock.now >= dispatch_module.RACING_WINNER_WAIT_SECONDS


def test_a_launched_peer_is_never_waited_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claim whose worker has already launched refuses without any wait."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def _must_not_be_called(_run_id: str):
        raise AssertionError("a launched peer must not be waited on")

    _stub_wait(monkeypatch, _Clock(), _must_not_be_called)

    with pytest.raises(crew.ScopeConflict) as refusal:
        _check(repo, _peer_claim(repo, launched=True))

    assert refusal.value.run_id == PEER_RUN_ID


def test_the_facade_outranks_a_later_racing_arrival(tmp_path: Path) -> None:
    """A dispatch routed through the facade applies the same ordering.

    The facade rebuilds a peer claim from the live read model before it reaches
    the check, so unless the registration time and launch state travel with it,
    the check reads the peer as established and refuses a later racing arrival
    that the dispatch registered first should outrank.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    peer = runs_module._LiveScopeClaim(
        run_id=PEER_RUN_ID,
        node_id="node-winner",
        path=CLAIMED_PATH,
        declared_path=CLAIMED_PATH,
        registered_at="2026-10-01T04:00:20Z",
        launched=False,
    )

    # The peer registered ten seconds after this dispatch and has not launched,
    # so this dispatch outranks it and no refusal is raised.
    runs_module._raise_live_scope_conflict(
        _node(),
        [peer],
        repo,
        project="fixture",
        own_run_id=OWN_RUN_ID,
        own_registered_at=OWN_REGISTERED_AT,
    )
