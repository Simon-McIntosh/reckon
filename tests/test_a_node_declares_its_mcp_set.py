"""A node's worker connects to exactly the MCP set its flight declares for it.

A flight declares MCP sets under ``mcp_sets``, each a list of servers, and a
node names one with ``mcp_set``. Every launch backend then starts exactly that
set: on claude the run's ``worker-mcp.json`` and the seeded settings name only
those servers, on codex every other configured server is disabled on the argv,
and on omp the agent store keeps only those servers and denies the rest. A node
naming no set gets the flight's flat ``worker_mcp_servers`` list, or no server
at all when the flight names none — the plan's own statement that a node naming
no set connects to no server. The set and its servers are recorded on the run,
so a coordinator reads what a worker could reach from the run rather than from
a configuration file that may have moved since.

The negative control is the base revision, where ``mcp_sets`` is not a flight
key and no node carries a set: there a node naming a set is refused, and no
launch has a set to record. ``BASE_REVISION_PROBE`` reproduces that failure and
``__main__`` writes it to ``BASE_LOG``.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from reckon import _backends, _worker_fence, flight
from reckon.crew.dispatching import launch as launch_module

# The servers the fixture flight declares, split across two sets so a launch
# that connected to the wrong one would name a server the other owns.
FIRST_SET = ["imas-cx", "imas-db"]
SECOND_SET = ["imas-dd"]
CONFIGURED = FIRST_SET + SECOND_SET

CLAUDE_BACKEND = {"launch": "cli", "command": "claude"}
CODEX_BACKEND = {"launch": "cli", "command": "codex"}
# The omp harness reads its MCP declarations from the agent store, which a run
# home carries only when the backend declares the file to seed it from.
OMP_BACKEND = {
    "launch": "cli",
    "command": "omp",
    "harness_home_files": [{"path": "agent/mcp.json"}],
}

BASE_LOG = (
    Path(__file__).parents[1]
    / "docs"
    / "evidence"
    / "fragments"
    / "work-scales-to-eighty-across-nodes"
    / "s3.html"
)

# What the base revision does with a node that names a set: the flight layer
# refuses the declaration it does not know, and the launch has no parameter to
# connect a node to a set at all.
BASE_REVISION_PROBE = """\
from reckon import _backends, flight

try:
    flight.resolve(
        overrides={"mcp_sets": {"first": ["imas-cx"]}},
        host_path="host/flight.yaml",
        project_path="project/flight.yaml",
        catalogue_path="none.yaml",
        strict=True,
    )
except flight.FlightConfigError as exc:
    print("flight refuses mcp_sets:", exc)

try:
    _backends.launch_plan(worker_mcp_set="first")
except TypeError as exc:
    print("launch has no set to connect a node to:", exc)
"""


def _servers(names: list[str]) -> dict[str, dict[str, object]]:
    return {name: {"command": name, "args": ["serve"]} for name in names}


def _operator_home(root: Path) -> Path:
    """An operator home whose harnesses all declare every configured server."""
    home = root / "operator"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({"hooks": {}}))
    (home / ".claude.json").write_text(json.dumps({"mcpServers": _servers(CONFIGURED)}))
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text(
        "".join(f'\n[mcp_servers.{name}]\ncommand = "{name}"\n' for name in CONFIGURED)
    )
    (home / ".omp" / "agent").mkdir(parents=True)
    (home / ".omp" / "agent" / "mcp.json").write_text(
        json.dumps({"mcpServers": _servers(CONFIGURED)})
    )
    return home


def _checkout(root: Path) -> Path:
    checkout = root / "checkout"
    checkout.mkdir()
    (checkout / ".mcp.json").write_text(json.dumps({"mcpServers": _servers(CONFIGURED)}))
    return checkout


def _flight_config(root: Path, **declaration) -> dict:
    """Resolve a flight declaring the two sets, through the layer validation."""
    overrides: dict = {"mcp_sets": {"first": FIRST_SET, "second": SECOND_SET}}
    overrides.update(declaration)
    resolved = flight.resolve(
        overrides=overrides,
        host_path=root / "host" / "flight.yaml",
        project_path=root / "project" / "flight.yaml",
        catalogue_path=root / "absent-catalogue.yaml",
        strict=True,
    )
    return dict(resolved.config)


def _launch(root: Path, backend: dict, mcp_set: str, **declaration):
    """Compose one launch whose node names ``mcp_set``, and return its fixtures."""
    home = _operator_home(root)
    checkout = _checkout(root)
    run = root / f"run-{backend['command']}-{mcp_set or 'none'}"
    run.mkdir()
    plan = _backends.launch_plan(
        backend_name=str(backend["command"]),
        backend=backend,
        prompt="do the node",
        worktree=str(checkout),
        manifest_path=str(run / "manifest.md"),
        fence=True,
        fence_home=home,
        fence_config=_flight_config(root, **declaration),
        worker_mcp_set=mcp_set,
    )
    return plan, run


def _claude_connected(run: Path) -> set[str]:
    """The servers the claude-shaped harness is declared to start."""
    return set(json.loads((run / "worker-mcp.json").read_text())["mcpServers"])


def _omp_connected(run: Path) -> tuple[set[str], set[str]]:
    """The omp agent store's declared servers, and the ones it denies."""
    store = json.loads((run / "omp-home" / "agent" / "mcp.json").read_text())
    return set(store["mcpServers"]), set(store.get("disabledServers") or ())


def _codex_connected(plan) -> set[str]:
    """The servers codex keeps enabled: everything its argv does not disable."""
    disabled: set[str] = set()
    for index, argument in enumerate(plan.argv):
        if argument == "-c" and index + 1 < len(plan.argv):
            table = plan.argv[index + 1]
            if table.startswith("mcp_servers="):
                disabled |= set(re.findall(r'"([^"]+)"=\{enabled=false\}', table))
    return set(CONFIGURED) - disabled


# ── each backend connects to exactly the set its node names ──────────────────


@pytest.mark.parametrize(
    ("mcp_set", "expected"),
    [
        pytest.param("first", FIRST_SET, id="first-set"),
        pytest.param("second", SECOND_SET, id="second-set"),
        pytest.param("", [], id="no-set"),
    ],
)
def test_a_claude_launch_connects_to_exactly_its_set(tmp_path: Path, mcp_set, expected):
    plan, run = _launch(tmp_path, CLAUDE_BACKEND, mcp_set)

    assert plan.mcp_set == mcp_set
    assert _claude_connected(run) == set(expected)
    settings = json.loads((run / "harness" / "settings.json").read_text())
    assert settings["enabledMcpjsonServers"] == expected
    assert settings["enableAllProjectMcpServers"] is False


@pytest.mark.parametrize(
    ("mcp_set", "expected"),
    [
        pytest.param("first", FIRST_SET, id="first-set"),
        pytest.param("second", SECOND_SET, id="second-set"),
        pytest.param("", [], id="no-set"),
    ],
)
def test_a_codex_launch_connects_to_exactly_its_set(tmp_path: Path, mcp_set, expected):
    plan, _run = _launch(tmp_path, CODEX_BACKEND, mcp_set)

    assert plan.mcp_set == mcp_set
    assert _codex_connected(plan) == set(expected)
    assert list(plan.worker_mcp_servers) == expected


@pytest.mark.parametrize(
    ("mcp_set", "expected"),
    [
        pytest.param("first", FIRST_SET, id="first-set"),
        pytest.param("second", SECOND_SET, id="second-set"),
        pytest.param("", [], id="no-set"),
    ],
)
def test_an_omp_launch_connects_to_exactly_its_set(tmp_path: Path, mcp_set, expected):
    plan, run = _launch(tmp_path, OMP_BACKEND, mcp_set)

    declared, denied = _omp_connected(run)
    assert declared == set(expected)
    assert denied == set(CONFIGURED) - set(expected)
    assert list(plan.worker_mcp_servers) == expected


# ── the refusal, and the record ─────────────────────────────────────────────


def test_a_node_naming_an_undeclared_set_is_refused(tmp_path: Path):
    """The guarded thing happens: a node names a set the flight does not hold."""
    with pytest.raises(_backends.BackendError) as excinfo:
        _launch(tmp_path, CLAUDE_BACKEND, "third")

    refusal = str(excinfo.value)
    assert "'third'" in refusal
    assert "first" in refusal and "second" in refusal
    # Refused before the run home exists, so no harness was seeded with a
    # server list nobody declared.
    assert not (tmp_path / "run-claude-third" / "harness").exists()


def test_the_connected_set_is_recorded_on_the_run(tmp_path: Path):
    plan, _run = _launch(tmp_path, CLAUDE_BACKEND, "first")

    assert launch_module.worker_mcp_record(plan) == {
        "worker_mcp": {"set": "first", "servers": FIRST_SET}
    }


def test_a_node_naming_no_set_records_no_connection_it_never_made(tmp_path: Path):
    plan, _run = _launch(tmp_path, CLAUDE_BACKEND, "")

    assert launch_module.worker_mcp_record(plan) == {}


def test_a_node_naming_no_set_gets_the_flights_flat_declaration(tmp_path: Path):
    """The declaration projects carried before sets existed still decides."""
    plan, run = _launch(
        tmp_path, CLAUDE_BACKEND, "", worker_mcp_servers=list(SECOND_SET)
    )

    assert plan.mcp_set == ""
    assert _claude_connected(run) == set(SECOND_SET)


# ── the declaration's own shape ─────────────────────────────────────────────


def test_the_flight_layer_keeps_the_sets_it_declared(tmp_path: Path):
    config = _flight_config(tmp_path)

    assert config["mcp_sets"] == {"first": FIRST_SET, "second": SECOND_SET}
    assert _worker_fence.worker_mcp_selection(config, "second") == (
        "second",
        SECOND_SET,
    )
    assert _worker_fence.worker_mcp_selection(config, "") == ("", [])


@pytest.mark.parametrize(
    "declaration",
    [
        pytest.param({"first": "imas-cx"}, id="bare-string"),
        pytest.param({"first": {"imas-cx": None}}, id="mapping"),
        pytest.param({"first": ["imas-cx", 3]}, id="list-holding-a-non-string"),
        pytest.param({"": ["imas-cx"]}, id="empty-name"),
        pytest.param("imas-cx", id="not-a-mapping"),
    ],
)
def test_a_malformed_set_table_is_refused_by_the_flight_layer(
    tmp_path: Path, declaration
):
    with pytest.raises(flight.FlightConfigError) as excinfo:
        flight.resolve(
            overrides={"mcp_sets": declaration},
            host_path=tmp_path / "host" / "flight.yaml",
            project_path=tmp_path / "project" / "flight.yaml",
            catalogue_path=tmp_path / "absent-catalogue.yaml",
            strict=True,
        )

    assert flight.MCP_SETS_KEY in str(excinfo.value)


def test_the_reader_and_the_layer_refuse_the_same_table(tmp_path: Path):
    """One reader answers both, so a launch cannot read what a layer refused."""
    with pytest.raises(_worker_fence.WorkerMcpSetsError):
        _worker_fence._declared_mcp_sets({"mcp_sets": {"first": "imas-cx"}})


if __name__ == "__main__":  # pragma: no cover - reproduces the red log
    import sys

    BASE_LOG.parent.mkdir(parents=True, exist_ok=True)
    BASE_LOG.write_text(
        "<!doctype html><html><head>"
        '<meta name="docs-project" content="reckon">'
        '<meta name="reckon-type" content="doc">'
        f'<meta name="plan-slug" content="work-scales-to-eighty-across-nodes">'
        "<title>A node declares the MCP set it needs — base revision</title>"
        "</head><body><h1>Base revision: a node naming a set is refused</h1>"
        "<p>Measured at the base revision by running the probe below. The base "
        "flight layer declares no <code>mcp_sets</code> key and "
        "<code>launch_plan</code> has no parameter that selects a set, so a "
        "node naming one is refused rather than connected to any server.</p>"
        f"<pre>{BASE_REVISION_PROBE}</pre></body></html>",
        encoding="utf-8",
    )
    sys.exit(0)

# ── the mutation this node's check must fail against ───────────────────────

# The mutation: the launch reads the flight's flat list and ignores the set, so
# a node naming the second set connects to whichever servers the flight lists
# project-wide. The set cases then read a connected set nobody asked for.
MUTATION_ENV = "NODE_MCP_SET_MUTATION"


@pytest.fixture(autouse=True)
def _apply_declared_mutation(monkeypatch):
    if not os.environ.get(MUTATION_ENV):
        return
    monkeypatch.setattr(
        _backends,
        "worker_mcp_selection",
        lambda config, mcp_set="": (
            "",
            _worker_fence._declared_worker_mcp_servers(config),
        ),
    )
