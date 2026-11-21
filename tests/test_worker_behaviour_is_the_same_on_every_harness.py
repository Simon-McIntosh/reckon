"""One lane, several harnesses, one treatment for the worker launched on it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import _backends
from reckon.crew import worker_behaviour as behaviour
from reckon.crew.admission.selection import gate_admits_request

LANE_DOCUMENT = "/srv/lanes/local.json"

# The local lane is served by three harnesses in front of one model server:
# a claude-shaped wrapper, a claude backend, and oh-my-pi. Each publishes the
# same lane document, so each serves the one lane.
LOCAL_BACKENDS = {
    "clive": {"command": "clive", "launch": "cli", "lane_document": LANE_DOCUMENT},
    "claude-local": {
        "command": "claude",
        "launch": "cli",
        "lane_document": LANE_DOCUMENT,
    },
    "omp-local": {"command": "omp", "launch": "cli", "lane_document": LANE_DOCUMENT},
}


def _node(tmp_path: Path) -> dict[str, Path]:
    """A node's run directory, worktree and delivery roots, laid out once."""
    run_directory = tmp_path / "run"
    worktree = tmp_path / "worktree"
    delivery = tmp_path / "delivery"
    for directory in (run_directory, worktree, delivery):
        directory.mkdir()
    return {"run": run_directory, "worktree": worktree, "delivery": delivery}


def _compose(node: dict[str, Path], name: str, backend) -> _backends.LaunchPlan:
    return _backends.launch_plan(
        backend_name=name,
        backend=backend,
        prompt="one node, one brief",
        worktree=node["worktree"],
        manifest_path=node["run"] / "manifest.md",
        writable_directories=(node["delivery"],),
        fence=False,
        behaviour_role=behaviour.IMPLEMENT_ROLE,
        flight_config={"local_backend": name, "backends": dict(LOCAL_BACKENDS)},
    )


def _treatments(node: dict[str, Path], names) -> dict[str, dict]:
    return {
        name: _compose(node, name, LOCAL_BACKENDS[name]).worker_behaviour
        for name in names
    }


def test_every_local_backend_gives_one_node_the_same_treatment(tmp_path: Path) -> None:
    node = _node(tmp_path)
    treatments = _treatments(node, tuple(LOCAL_BACKENDS))
    reference_name, reference = next(iter(treatments.items()))
    for name, treatment in treatments.items():
        assert treatment == reference, (
            f"backend {name!r} treats one node differently from {reference_name!r}"
        )
    # The facts the lane owns, stated rather than implied: one inbox, one
    # allowlist, one duty, one landing route, and one lane identity.
    assert reference["lane"] == LANE_DOCUMENT
    assert reference["inbox_directory"] == str(node["run"] / "inbox")
    assert reference["mcp_server_allowlist"] is True
    assert reference["review_duty"] is True
    assert reference["landing_treatment"] == "promoted-under-its-lane"


def test_a_launch_that_loses_its_inbox_is_not_the_same_treatment(
    tmp_path: Path,
) -> None:
    """Negative control for the first case: drop one backend's inbox delivery."""
    node = _node(tmp_path)
    first = behaviour.compose_worker_behaviour(
        backend_name="clive",
        backend=LOCAL_BACKENDS["clive"],
        run_directory=node["run"],
        role=behaviour.IMPLEMENT_ROLE,
    )
    without_inbox = behaviour.compose_worker_behaviour(
        backend_name="omp-local",
        backend=LOCAL_BACKENDS["omp-local"],
        run_directory=None,
        role=behaviour.IMPLEMENT_ROLE,
    )
    assert behaviour.behave_identically(first, without_inbox) == ["inbox_directory"]


def _one_lane(config: dict) -> bool:
    """Whether every backend in the queue resolves to the drainer's one lane."""
    identities = {
        behaviour.lane_identity(name, None, config) for name in LOCAL_BACKENDS
    }
    return len(identities) == 1


def _gated_identically(config: dict, lane: str) -> bool:
    verdicts = {
        name: gate_admits_request(name, lane, config) for name in LOCAL_BACKENDS
    }
    return len(set(verdicts.values())) == 1 and all(verdicts.values())


@pytest.mark.parametrize("configured", ["clive", "omp-local"])
def test_the_gate_admits_either_backend_of_one_lane_identically(
    configured: str,
) -> None:
    """A queue holding one request per backend is gated the same either way."""
    config = {"local_backend": configured, "backends": dict(LOCAL_BACKENDS)}
    assert _one_lane(config)
    assert _gated_identically(config, configured)


def test_substituting_a_backend_name_for_the_lane_splits_it() -> None:
    """Negative control for the second case: the gate then holds by name."""
    name_only = {
        "clive": {"command": "clive", "launch": "cli"},
        "claude-local": {"command": "claude", "launch": "cli"},
        "omp-local": {"command": "omp", "launch": "cli"},
    }
    config = {"local_backend": "clive", "backends": name_only}
    assert not _one_lane(config)


def test_a_shared_lane_document_is_one_lane_identity(tmp_path: Path) -> None:
    resolved = str((tmp_path / "local.json").resolve())
    declared = {**LOCAL_BACKENDS}
    for settings in declared.values():
        settings["lane_document"] = resolved
    identities = {
        behaviour.lane_identity(name, settings, {"backends": declared})
        for name, settings in declared.items()
    }
    assert identities == {resolved}
    assert json.dumps(sorted(identities)) == json.dumps([resolved])


def test_a_wrapped_harness_gets_the_home_and_hook_it_wraps(tmp_path: Path) -> None:
    """A claude-shaped wrapper is seeded claude's home, hook and files."""
    from reckon._worker_fence import harness_home, seed_harness_home
    from reckon.hooks import install

    homes = {}
    for dialect_name in ("clive", "claude"):
        run_directory = tmp_path / dialect_name
        run_directory.mkdir()
        home = harness_home(dialect_name, run_directory)
        assert home is not None
        homes[dialect_name] = home.relative_to(run_directory)
        seed_harness_home(
            home,
            dialect_name=dialect_name,
            operator_home=tmp_path / "operator",
            settings_file="settings.json",
            mcp_config_file=".claude.json",
        )
        assert install.settings_carry_worker_inbox(home / "settings.json")
    assert homes["clive"] == homes["claude"]
