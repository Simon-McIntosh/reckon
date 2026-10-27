"""A worker's harness home starts only the MCP servers its project names.

A harness that starts in a run home with no settings inherits every server the
checkout's ``.mcp.json`` registers — about twenty on one project measured
2026-10-08 — each paying a process and, on a launch whose directory does not
resolve, a connection that fails. So the seeder writes into the run home's
``settings.json`` the two Claude Code keys that govern the project's servers:
``enableAllProjectMcpServers`` set false, which disables the project's
``.mcp.json`` servers as a group, and ``enabledMcpjsonServers`` naming the ones
the project's flight configuration names under ``worker_mcp_servers``. The user
scope servers — reckon among them — are carried separately in the run home's
``.claude.json`` and stay enabled, because the project key does not govern them.

Everything here runs against a synthetic operator home, a synthetic project
checkout and a synthetic run directory in ``tmp_path`` and never touches the
real ``~/.claude`` or ``~/.claude.json``.

Two mutations each reproduce a red log. ``WORKER_MCP_OWNED_KEY_MUTATION=1``
takes ``worker_mcp_servers`` out of the reckon-owned keys, so the flight
resolution refuses the declaration as an unknown key and the resolve test
fails. ``WORKER_MCP_SETTINGS_MUTATION=1`` removes the seeded settings write, so
the default project's server reads enabled and the settings tests fail.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from reckon import _backends, _worker_fence, flight

NEGATIVE_CONTROL_MUTATION = (
    "take worker_mcp_servers out of the reckon-owned keys; the resolve raises "
    "the schema refusal and the test fails"
)
SETTINGS_MUTATION = (
    "remove the seeded settings write that disables project MCP servers; the "
    "no-worker_mcp_servers case then reads the project server enabled and fails"
)
OWNED_KEY_MUTATION_ENV = "WORKER_MCP_OWNED_KEY_MUTATION"
SETTINGS_MUTATION_ENV = "WORKER_MCP_SETTINGS_MUTATION"


def _unowned_schema_view(real):
    """The declared mutation: the schema view keeps ``worker_mcp_servers``."""

    def view(data):
        cleaned = real(data)
        if flight.WORKER_MCP_SERVERS_KEY in data:
            cleaned[flight.WORKER_MCP_SERVERS_KEY] = data[flight.WORKER_MCP_SERVERS_KEY]
        return cleaned

    return view


# The servers a fixture project registers in its ``.mcp.json``, none of which
# a worker should start unless the project names it.
PROJECT_SERVERS = {
    "imas-cx": {"command": "uv", "args": ["run", "imas-codex", "serve"]},
    "imas-dd": {"command": "uv", "args": ["run", "imas-dd", "serve"]},
}
# The user-scope servers the operator carries in ``~/.claude.json``. reckon is
# the one every worker needs; it must stay enabled.
OPERATOR_SERVERS = {"reckon": {"command": "reckon", "args": ["mcp"]}}

CLAUDE_BACKEND = {"launch": "cli", "command": "claude"}
CLIVE_BACKEND = {"launch": "cli", "command": "clive"}


def _operator_home(root: Path) -> Path:
    """Build a synthetic operator home holding settings and user servers."""
    home = root / "operator"
    claude = home / ".claude"
    claude.mkdir(parents=True)
    (claude / "settings.json").write_text(
        json.dumps({"hooks": {"Stop": [{"command": "operator-stop"}]}})
    )
    (claude / "CLAUDE.md").write_text("# operator guidance\n")
    (home / ".claude.json").write_text(json.dumps({"mcpServers": OPERATOR_SERVERS}))
    return home


def _project_checkout(root: Path) -> Path:
    """Build a synthetic project checkout registering its own MCP servers."""
    checkout = root / "checkout"
    checkout.mkdir(parents=True, exist_ok=True)
    (checkout / ".mcp.json").write_text(json.dumps({"mcpServers": PROJECT_SERVERS}))
    return checkout


# Declarations that are not a list of server names: a bare string, a mapping,
# a tuple, and a list holding a value that is not a string. Each must be
# refused by name rather than seeded as the servers the project asked for.
MALFORMED_DECLARATIONS = [
    pytest.param("imas-cx", id="bare-string"),
    pytest.param({"imas-cx": None}, id="mapping"),
    pytest.param(("imas-cx",), id="tuple"),
    pytest.param(["imas-cx", 3], id="list-holding-a-non-string"),
]


def _resolve(root: Path, declaration: dict):
    """Resolve a flight configuration carrying ``declaration`` as its project layer.

    The declaration is resolved the way a project's own layer is, through the
    same layer validation a launch reads, and with the strict authoring read so
    a key the running schema does not declare is refused by name rather than
    silently reported.
    """
    return flight.resolve(
        overrides=declaration,
        host_path=root / "host" / "flight.yaml",
        project_path=root / "project" / "flight.yaml",
        catalogue_path=root / "absent-catalogue.yaml",
        strict=True,
    )


def _launch(home: Path, run: Path, backend: dict, *, fence_config: dict | None = None):
    return _backends.launch_plan(
        backend_name=str(backend["command"]),
        backend=backend,
        prompt="do the node",
        worktree=str(run / "worktree"),
        manifest_path=str(run / "manifest.md"),
        fence=True,
        fence_home=home,
        fence_config=fence_config,
    )


def _harness(plan) -> Path:
    return Path(plan.environment["CLAUDE_CONFIG_DIR"])


def _settings(plan) -> dict:
    return json.loads((_harness(plan) / "settings.json").read_text())


def _server_starts(settings: dict, name: str) -> bool:
    """Whether the harness would start project server ``name``.

    A server named in the per-server allow list starts; otherwise it starts
    only when the group switch has not been turned off.
    """
    if name in settings.get(_backends.ENABLED_PROJECT_MCP_SERVERS_KEY, []):
        return True
    return settings.get(_backends.PROJECT_MCP_SERVERS_ENABLED_KEY) is not False


@pytest.fixture(autouse=True)
def _apply_declared_mutation(monkeypatch):
    """The declared negative controls, each behind its own switch."""
    if os.environ.get(OWNED_KEY_MUTATION_ENV):
        monkeypatch.setattr(
            flight, "_schema_view", _unowned_schema_view(flight._schema_view)
        )
    if os.environ.get(SETTINGS_MUTATION_ENV):
        monkeypatch.setattr(
            _worker_fence, "_seed_worker_mcp_settings", lambda home, servers: None
        )


def test_the_default_project_disables_its_servers_and_keeps_reckon(tmp_path: Path):
    """No worker_mcp_servers: the project's servers do not start; reckon does."""
    home = _operator_home(tmp_path)
    _project_checkout(tmp_path)
    run = tmp_path / "run"
    run.mkdir()
    plan = _launch(home, run, CLAUDE_BACKEND, fence_config={})

    settings = _settings(plan)
    assert settings[_backends.PROJECT_MCP_SERVERS_ENABLED_KEY] is False
    for name in PROJECT_SERVERS:
        assert not _server_starts(settings, name), name
    # reckon is a user-scope server, carried in the run home's .claude.json and
    # not governed by the project key, so it stays enabled.
    carried = json.loads((_harness(plan) / ".claude.json").read_text())
    assert carried["mcpServers"] == OPERATOR_SERVERS
    assert "reckon" in carried["mcpServers"]


def test_a_named_worker_server_is_enabled_in_the_seeded_settings(tmp_path: Path):
    """A project listing imas-cx finds it enabled in the seeded settings file."""
    home = _operator_home(tmp_path)
    run = tmp_path / "run"
    run.mkdir()
    plan = _launch(
        home, run, CLAUDE_BACKEND, fence_config={"worker_mcp_servers": ["imas-cx"]}
    )

    settings = _settings(plan)
    assert settings[_backends.ENABLED_PROJECT_MCP_SERVERS_KEY] == ["imas-cx"]
    assert _server_starts(settings, "imas-cx")
    # A server the project did not name still does not start.
    assert not _server_starts(settings, "imas-dd")


def test_a_clive_launch_seeds_it_through_the_lifecycle_path(tmp_path: Path):
    """clive runs the claude-shaped harness, so a clive launch seeds it too."""
    home = _operator_home(tmp_path)
    run = tmp_path / "run"
    run.mkdir()
    plan = _launch(home, run, CLIVE_BACKEND, fence_config={})
    assert plan.dialect == "claude"
    assert _settings(plan)[_backends.PROJECT_MCP_SERVERS_ENABLED_KEY] is False


def test_an_existing_run_settings_file_is_left_untouched(tmp_path: Path):
    """A run's own settings file is left untouched, not rewritten by the seed."""
    home = _operator_home(tmp_path)
    run = tmp_path / "run"
    run.mkdir()
    harness = run / "harness"
    harness.mkdir(parents=True)
    own = json.dumps({"hooks": {"Stop": []}, "model": "a-run-choice"})
    (harness / "settings.json").write_text(own)
    _launch(home, run, CLAUDE_BACKEND, fence_config={})

    # The run home was seeded once, carrying the MCP record; a launch into it
    # again leaves the run's own file exactly as it is.
    assert (harness / "settings.json").read_text() == own


def test_a_missing_operator_settings_file_still_gains_the_record(tmp_path: Path):
    """No operator settings.json invents none, but the MCP record is written."""
    home = _operator_home(tmp_path)
    (home / ".claude" / "settings.json").unlink()
    run = tmp_path / "run"
    run.mkdir()
    plan = _launch(home, run, CLAUDE_BACKEND, fence_config={})
    settings = _settings(plan)
    assert settings[_backends.PROJECT_MCP_SERVERS_ENABLED_KEY] is False


def test_the_operator_home_is_never_written(tmp_path: Path):
    """Seeding reads the given operator home and never mutates it."""
    home = _operator_home(tmp_path)
    before = {p.relative_to(home): p.stat().st_mtime_ns for p in home.rglob("*")}
    run = tmp_path / "run"
    run.mkdir()
    _launch(home, run, CLAUDE_BACKEND, fence_config={})
    after = {p.relative_to(home): p.stat().st_mtime_ns for p in home.rglob("*")}
    assert after == before


def test_a_project_naming_its_servers_resolves_and_seeds_them(tmp_path: Path):
    """A flight config naming imas-cx resolves and seeds the run home with it."""
    home = _operator_home(tmp_path)
    run = tmp_path / "run"
    run.mkdir()
    resolved = _resolve(tmp_path, {"worker_mcp_servers": ["imas-cx"]})
    assert resolved.config["worker_mcp_servers"] == ["imas-cx"]

    plan = _launch(home, run, CLAUDE_BACKEND, fence_config=dict(resolved.config))

    settings = _settings(plan)
    assert settings[_backends.ENABLED_PROJECT_MCP_SERVERS_KEY] == ["imas-cx"]
    assert _server_starts(settings, "imas-cx")
    assert not _server_starts(settings, "imas-dd")


@pytest.mark.parametrize("declaration", MALFORMED_DECLARATIONS)
def test_a_declaration_that_is_not_a_list_is_refused_naming_the_key(
    tmp_path: Path, declaration
):
    """A value that is not a list of strings is refused, naming the key."""
    with pytest.raises(flight.FlightConfigError) as excinfo:
        _resolve(tmp_path, {"worker_mcp_servers": declaration})
    assert flight.WORKER_MCP_SERVERS_KEY in excinfo.value.key_path
    assert flight.WORKER_MCP_SERVERS_KEY in str(excinfo.value)


@pytest.mark.parametrize("declaration", MALFORMED_DECLARATIONS)
def test_the_reader_refuses_a_declaration_that_is_not_a_list_of_strings(
    declaration,
):
    """The one reader refuses the same values the layer refuses."""
    with pytest.raises(ValueError, match="worker_mcp_servers"):
        _worker_fence._declared_worker_mcp_servers({"worker_mcp_servers": declaration})


def test_an_absent_declaration_names_no_server():
    """An absent key names no server, and a well-formed list is read as given."""
    reader = _worker_fence._declared_worker_mcp_servers
    assert reader(None) == []
    assert reader({}) == []
    assert reader({"worker_mcp_servers": ["imas-cx", "imas-cx", ""]}) == ["imas-cx"]


if __name__ == "__main__":  # pragma: no cover - reproduces the red log
    import sys
    import tempfile

    if os.environ.get(SETTINGS_MUTATION_ENV):
        _worker_fence._seed_worker_mcp_settings = lambda home, servers: None
    if os.environ.get(OWNED_KEY_MUTATION_ENV):
        flight._schema_view = _unowned_schema_view(flight._schema_view)
        print(NEGATIVE_CONTROL_MUTATION)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            try:
                _resolve(root, {"worker_mcp_servers": ["imas-cx"]})
            except flight.FlightConfigError as exc:
                print("refused:", exc)
                sys.exit(1)
            print("resolved without refusal")
            sys.exit(0)
    print(SETTINGS_MUTATION)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        home = _operator_home(root)
        run = root / "run"
        run.mkdir()
        plan = _launch(home, run, CLAUDE_BACKEND, fence_config={})
        settings = _settings(plan)
        print(
            "enableAllProjectMcpServers:",
            settings.get(_backends.PROJECT_MCP_SERVERS_ENABLED_KEY),
        )
        print("imas-cx starts:", _server_starts(settings, "imas-cx"))

    sys.exit(1 if os.environ.get(SETTINGS_MUTATION_ENV) else 0)
