"""A wallet that cannot serve a review withholds no bookend reserve.

The bookend reserve keeps a fraction of every wallet's window for the review and
verify roles. On a declared budget group whose every member the configuration
removes from review routing, no review can ever run, so the reserved fraction
would be spent by nobody and would only hold the implementation work that wallet
does serve below its own ceiling. The reserve is therefore lifted there and
keeps holding everywhere a review can run.

The measure is a pair, not a single figure. A reserve that was lifted for every
wallet would report zero everywhere the test looks, so each all-excluded case is
asserted beside the same wallet with one review-capable member, where the
configured reserve is still withheld — and the configured figure is read from
the configuration at assertion time rather than written into the test, so a
reserve declared at another value moves both the wallet's report and this test's
expectation together.

Two surfaces are driven. The wallet's own figures are read through
``budget_group.group_figures``, and the boundary is driven through the dispatch
entry point itself, because a correct reserve lift refuses nothing if the
dispatch path never consults it. The dispatch cases run on the harness that
drives a real dispatch: a temporary crew home, a real worktree, and the
assertion that nothing landed outside it.

Every case here points the configuration home at a throwaway tree and asserts
afterwards that this workstation's own configuration is untouched.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import crew, flight
from reckon._store import _config_home
from reckon.crew import budget_group as bg
from reckon.crew import reserve
from tests.conftest import test_temp_config_home as _temp_config_home
from tests.test_dispatch_records_the_pace_row import (
    _Host,
    _prescribed_node,
    _spent_lane,
)
from tests.test_dispatch_records_the_pace_row import (
    host as _host_fixture,
)

# pytest injects a fixture by the name a case's parameter asks for, so the
# harness's fixture is bound under that name rather than the alias it arrives by.
host = _host_fixture

NOW = datetime(2026, 10, 8, 12, 0, 0, tzinfo=UTC)

# The wallet two lanes share, and the configured reserve this file's cases are
# measured against. The figure lives in the synthesised layer, never in a
# literal the test compares itself to: a wallet's report and the expectation are
# read from the same declaration.
WALLET = "sol"
CONFIGURED_RESERVE_PCT = 25.0

# The two members of the shared wallet, declared so a case can exclude one or
# both and observe which way the reserve moves.
WALLET_MEMBERS = ("alpha", "beta")

# A host layer declaring one wallet with two members and a configured reserve.
# Which members are removed from review routing is the axis under test, so the
# exclusion list is substituted rather than written in.
HOST_LAYER = """
version: 1

backends:
  alpha:
    command: codex
    budget_group: {wallet}
  beta:
    command: codex
    budget_group: {wallet}

budget:
  bookend_reserve_pct: {reserve}

review_excluded_backends: {exclusions}
"""


def _home_manifest(home: Path) -> dict[str, str]:
    """The home's own files, by name and content, with its file census."""
    files = {"<census>": json.dumps(sorted(p.name for p in home.iterdir()))}
    for entry in sorted(home.iterdir()):
        if entry.is_file():
            files[entry.name] = hashlib.sha256(entry.read_bytes()).hexdigest()
    return files


@pytest.fixture()
def guarded_home(monkeypatch, tmp_path):
    """Point the configuration home at a throwaway tree, watching the real one."""
    real = _config_home()
    before = _home_manifest(real)
    home = _temp_config_home("reckon-bookend-reserve-", directory=tmp_path)
    monkeypatch.setenv("RECKON_HOME", str(home))
    yield home
    assert _home_manifest(real) == before, (
        "the run changed the workstation's own configuration home"
    )


def _resolved_config(tmp_path: Path, *, excluded: tuple[str, ...]) -> dict:
    """Resolve a flight config whose wallet carries the given review exclusions."""
    layer = tmp_path / "host-flight.yaml"
    layer.write_text(
        HOST_LAYER.format(
            wallet=WALLET,
            reserve=CONFIGURED_RESERVE_PCT,
            exclusions=(
                "\n" + "".join(f"  - {name}\n" for name in excluded)
                if excluded
                else "[]"
            ),
        ),
        encoding="utf-8",
    )
    resolved = flight.resolve(
        host_path=layer,
        project_path=tmp_path / "project-flight.yaml",
    )
    return resolved.config


# ── The wallet's own figures ────────────────────────────────────────────────


def test_a_wallet_with_no_review_capable_member_withholds_no_reserve(
    guarded_home, tmp_path
) -> None:
    """Every member excluded: the reserve the wallet was sized for is gone.

    The wallet's report is the surface a reader consults, so a reserve that was
    lifted only inside the dispatch path would still read here as withheld.
    """
    config = _resolved_config(tmp_path, excluded=WALLET_MEMBERS)

    figures = bg.group_figures(WALLET, config, {}, now=NOW)

    assert sorted(figures.members) == sorted(WALLET_MEMBERS)
    assert figures.reserve_pct == 0.0


def test_a_wallet_with_one_review_capable_member_keeps_the_reserve(
    guarded_home, tmp_path
) -> None:
    """One member left review-capable: the configured reserve is withheld.

    The configured figure is read from the resolved configuration at assertion
    time, so the expectation and the report move together if the declaration is
    retuned. This is the half that separates a real lift from a reserve that was
    simply switched off.
    """
    config = _resolved_config(tmp_path, excluded=("alpha",))

    figures = bg.group_figures(WALLET, config, {}, now=NOW)
    configured = float(config["budget"][reserve.RESERVE_KEY])

    assert configured == CONFIGURED_RESERVE_PCT
    assert figures.reserve_pct == configured


# ── The boundary at the dispatch entry point ────────────────────────────────


def _dispatch_config(*, excluded: tuple[str, ...]) -> dict:
    """A dispatch config whose two lanes share one declared wallet."""
    return {
        "default_backend": "alpha",
        "backends": {
            "alpha": {
                "launch": "cli",
                "command": "codex",
                "model": "some-model",
                "effort": "high",
                "sandbox": "worktree-full",
                "session_reuse": True,
                "time_budget": "25m",
                "budget_group": WALLET,
            },
            "beta": {
                "launch": "cli",
                "command": "codex",
                "model": "some-model",
                "effort": "high",
                "sandbox": "worktree-full",
                "session_reuse": True,
                "time_budget": "25m",
                "budget_group": WALLET,
            },
        },
        "roles": {"implement": {}, "review": {}, "verify": {}, "investigate": {}},
        "budget": {
            "utilisation_ceiling_pct": 100,
            "resume_reserve_pct": 5,
            "coordinator_reserve_pct": 3,
            "bookend_reserve_pct": CONFIGURED_RESERVE_PCT,
            "drain_lead_hours": 12.0,
            "pace_multiple": 1.25,
            "exhausted_statuses": [],
        },
        "review_excluded_backends": list(excluded),
        "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    }


def _node(config_home: Path, name: str) -> crew.TaskNode:
    """The harness's prescribed node, carrying the implementation role."""
    node = _prescribed_node(config_home, name)
    node.role = "implement"
    return node


def _dispatch_with(host: _Host, name: str, node: crew.TaskNode, config: dict) -> dict:
    """Drive one node through the dispatch entry point under a chosen config."""
    record = crew.dispatch(
        node=node,
        project="sample",
        repo=host.repo,
        config=config,
        session=f"session-{name}",
        launcher=lambda *arguments, **options: 4242,
        watch_required=False,
    )
    host.spawned.append(str(record["run_id"]))
    return record


def test_an_implement_dispatch_reaches_the_full_ceiling_on_such_a_wallet(
    host: _Host,
) -> None:
    """The pair through the entry point: the same window, opposite verdicts.

    The window is filled past the reserve boundary and left below the budget
    gate's own ceiling, so the reserve is the only thing that could refuse the
    implementation dispatch. On the all-excluded wallet it is admitted; on the
    wallet with a review-capable member, meeting the same reading, it is
    refused against the reserved fraction. The refused half is what shows the
    admission is the reserve's lift rather than a window with room.
    """
    _spent_lane(host, "lane-at-the-lifted-boundary", backend="alpha", utilisation=85.0)

    admitted = _dispatch_with(
        host,
        "lifted-implement",
        _node(host.config_home, "lifted-implement"),
        _dispatch_config(excluded=WALLET_MEMBERS),
    )

    assert admitted["role"] == "implement"
    assert admitted["pace"]["group"] == WALLET
    assert admitted["pace"]["clocks"]["five_hour"]["utilisation"] == pytest.approx(0.85)

    _spent_lane(host, "lane-at-the-held-boundary", backend="alpha", utilisation=85.0)

    with pytest.raises(crew.CrewError) as refusal:
        _dispatch_with(
            host,
            "held-implement",
            _node(host.config_home, "held-implement"),
            _dispatch_config(excluded=("alpha",)),
        )

    message = str(refusal.value)
    assert (
        f"the window keeps {CONFIGURED_RESERVE_PCT:g}% for review and verify roles"
        in message
    ), message
    assert "implement" in message, message


# ── The key is a declared slot a flight layer can set ───────────────────────


def test_a_flight_layer_writes_the_key_and_moves_the_boundary(
    guarded_home, tmp_path
) -> None:
    """A layer may declare the reserve, and the declared value moves the ceiling.

    The key was read by the reserve and refused by the schema, so no layer could
    set it. Resolving a layer that writes it proves the slot is declared, and
    the implementation ceiling it produces proves the value reached the
    arithmetic rather than stopping at the parser.
    """
    layer = tmp_path / "host-flight.yaml"
    layer.write_text(
        "version: 1\nbudget:\n  bookend_reserve_pct: 35\n", encoding="utf-8"
    )

    resolved = flight.resolve(
        host_path=layer,
        project_path=tmp_path / "project-flight.yaml",
    )

    assert resolved.config["budget"][reserve.RESERVE_KEY] == 35.0
    assert resolved.origin(f"budget.{reserve.RESERVE_KEY}") == "host"
    assert reserve.role_ceiling_pct(resolved.config["budget"], "implement") == 65.0
    assert reserve.role_ceiling_pct(resolved.config["budget"], "review") == 65.0 + 35.0
