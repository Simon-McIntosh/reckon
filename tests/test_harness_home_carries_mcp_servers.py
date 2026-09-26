"""A fenced claude run home carries the operator's user-scope MCP servers.

The operator's user-scope MCP server declarations live in the top-level
``mcpServers`` key of ``~/.claude.json``, which sits in the home directory
rather than under ``~/.claude``, so the ``harness_home_files`` carry — whose
sources are relative to the harness config dir — never reads it. A fenced run
that starts without it loses every ``mcp__`` tool. The home-root carry
(``harness_home_adjacent_files``) names that file with its key filter, the same
seeder copies it, and because the harness writes the run's own copy itself the
seed merges the declared keys in rather than replacing the file.

Everything here runs against a synthetic operator home and run directory in
``tmp_path`` and never touches the real ``~/.claude`` or ``~/.claude.json``.

The declared negative control drops the ``~/.claude.json`` declaration from the
flight defaults; the mcpServers assertion then fails with no ``.claude.json``
under the run harness home. Running this file with ``HARNESS_HOME_MCP_MUTATION=1``
reproduces that red log.

The merged copy is private from creation: written to a sibling temporary opened
``0o600`` and moved onto the destination, never created at a umask mode and
narrowed afterwards. Its final mode is the narrower of ``0o600`` and any
existing destination's own mode, so a wider source never widens the run's copy.
The second declared negative control restores the chmod-after-write path;
running the file with ``HARNESS_HOME_MERGE_MUTATION=1`` reproduces its red log,
in which the private-from-creation case fails because no temporary is ever
observed.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path

import pytest

from reckon import _backends, flight

NEGATIVE_CONTROL_MUTATION = (
    "drop the ~/.claude.json declaration from the flight defaults; the "
    "mcpServers assertion must fail"
)

# The second declared negative control: restore the chmod-after-write path, so
# the seeded file exists at the umask mode until a later chmod and the spy on
# os.replace never fires. The private-from-creation case must then fail.
MERGE_NEGATIVE_CONTROL = (
    "restore the chmod-after-write path for the merged home file; the "
    "private-from-creation case must fail"
)

OPERATOR_SERVERS = {
    "reckon": {"command": "reckon-mcp"},
    "imas-dd": {"command": "imas-dd", "args": ["--serve"]},
}
OPERATOR_PROJECTS = {"/home/operator/Code/reckon": {"history": ["do a thing"]}}
OPERATOR_OAUTH = {"accessToken": "operator-secret", "userId": "operator-uid"}

CLAUDE_BACKEND = {"launch": "cli", "command": "claude"}
CLIVE_BACKEND = {"launch": "cli", "command": "clive"}


def _operator_home(root: Path) -> Path:
    """Build a synthetic operator home holding the MCP declarations."""
    home = root / "operator"
    claude = home / ".claude"
    claude.mkdir(parents=True)
    (claude / "settings.json").write_text(
        json.dumps({"hooks": {"Stop": [{"command": "operator-stop"}]}})
    )
    (claude / "CLAUDE.md").write_text("# operator guidance\n")
    operator_config = home / ".claude.json"
    operator_config.write_text(
        json.dumps(
            {
                "mcpServers": OPERATOR_SERVERS,
                "projects": OPERATOR_PROJECTS,
                "oauthAccount": OPERATOR_OAUTH,
                "numStartups": 42,
            }
        )
    )
    # The operator's file is private; the run's copy must be too.
    operator_config.chmod(0o600)
    return home


def _launch(home: Path, run: Path, backend: dict, *, fence: bool = True):
    return _backends.launch_plan(
        backend_name=str(backend["command"]),
        backend=backend,
        prompt="do the node",
        worktree=str(run / "worktree"),
        manifest_path=str(run / "manifest.md"),
        fence=fence,
        fence_home=home,
    )


@pytest.fixture(autouse=True)
def _apply_declared_mutation(monkeypatch):
    """The declared negative control: the home-root declaration is dropped."""
    if not os.environ.get("HARNESS_HOME_MCP_MUTATION"):
        return
    monkeypatch.setattr(flight, "shipped_harness_home_adjacent_files", dict)


@pytest.fixture(autouse=True)
def _apply_merge_mutation(monkeypatch):
    """The declared negative control: the chmod-after-write path is restored."""
    if not os.environ.get("HARNESS_HOME_MERGE_MUTATION"):
        return

    def _legacy_write(destination, payload, _mode=0o600):
        destination.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        destination.chmod(0o644)

    monkeypatch.setattr(_backends, "_write_private_json", _legacy_write)


def _metadata_snapshot(root: Path) -> list[tuple]:
    """Relative path, size and mtime for every entry — contents never read."""
    if not root.exists():
        return []
    entries: list[tuple] = []
    for path in sorted(root.rglob("*")):
        try:
            stat_result = path.stat()
        except OSError:
            continue
        entries.append(
            (
                str(path.relative_to(root)),
                path.is_dir(),
                stat_result.st_size,
                stat_result.st_mtime_ns,
            )
        )
    return entries


def test_the_shipped_defaults_declare_the_home_root_file():
    """The shipped home-root table names ~/.claude.json filtered to mcpServers."""
    shipped = flight.shipped_harness_home_adjacent_files()
    assert shipped["claude"] == [{"path": ".claude.json", "keys": ["mcpServers"]}]
    assert flight.harness_home_adjacent_files("claude") == [
        {"path": ".claude.json", "keys": ["mcpServers"]}
    ]
    # The config-dir table is untouched by the addition.
    assert flight.harness_home_files("claude") == [
        {"path": "settings.json", "keys": ["hooks"]},
        {"path": "CLAUDE.md"},
    ]


def test_a_claude_run_home_carries_the_operator_mcp_servers(tmp_path: Path):
    """The run's .claude.json holds only mcpServers, at mode 0600."""
    home = _operator_home(tmp_path)
    run = tmp_path / "run"
    run.mkdir()
    plan = _launch(home, run, CLAUDE_BACKEND)
    harness = Path(plan.environment["CLAUDE_CONFIG_DIR"])
    assert harness == run / "harness"

    seeded_path = harness / ".claude.json"
    seeded = json.loads(seeded_path.read_text())
    assert seeded["mcpServers"] == OPERATOR_SERVERS
    # No other operator key crosses: no projects, no oauth, no counters.
    assert "projects" not in seeded
    assert "oauthAccount" not in seeded
    assert "numStartups" not in seeded
    assert set(seeded) == {"mcpServers"}
    assert stat.S_IMODE(seeded_path.stat().st_mode) == 0o600


def test_a_clive_launch_through_the_production_path_seeds_it(tmp_path: Path):
    """clive runs the claude-shaped harness, so a clive launch carries it too."""
    home = _operator_home(tmp_path)
    run = tmp_path / "run"
    run.mkdir()
    plan = _launch(home, run, CLIVE_BACKEND)
    assert plan.dialect == "claude"
    assert plan.argv[0] == _backends.FENCE_BINARY
    harness = Path(plan.environment["CLAUDE_CONFIG_DIR"])
    seeded = json.loads((harness / ".claude.json").read_text())
    assert seeded["mcpServers"] == OPERATOR_SERVERS


def test_an_existing_run_home_file_keeps_its_keys_and_gains_the_servers(
    tmp_path: Path,
):
    """A run's own .claude.json is authoritative for its keys; yours merge in."""
    home = _operator_home(tmp_path)
    run = tmp_path / "run"
    run.mkdir()
    harness = run / "harness"
    harness.mkdir(parents=True)
    own = json.dumps(
        {
            "mcpServers": {"run-local": {"command": "run-mcp"}},
            "projects": {"/home/operator/Code/reckon": {"history": ["run's own"]}},
        }
    )
    (harness / ".claude.json").write_text(own)

    _launch(home, run, CLAUDE_BACKEND)

    seeded = json.loads((harness / ".claude.json").read_text())
    # The run's own keys survive, and the operator's servers are merged in.
    assert seeded["projects"] == {
        "/home/operator/Code/reckon": {"history": ["run's own"]}
    }
    assert seeded["mcpServers"]["run-local"] == {"command": "run-mcp"}
    assert seeded["mcpServers"]["reckon"] == OPERATOR_SERVERS["reckon"]
    assert seeded["mcpServers"]["imas-dd"] == OPERATOR_SERVERS["imas-dd"]


def test_a_run_home_without_the_operator_file_seeds_nothing(tmp_path: Path):
    """No ~/.claude.json beside the operator home invents no run copy."""
    home = tmp_path / "operator"
    (home / ".claude").mkdir(parents=True)
    run = tmp_path / "run"
    run.mkdir()
    plan = _launch(home, run, CLAUDE_BACKEND)
    harness = Path(plan.environment["CLAUDE_CONFIG_DIR"])
    assert not (harness / ".claude.json").exists()


def test_the_operator_home_is_never_written(tmp_path: Path):
    """Seeding reads the given operator home and never mutates it."""
    home = _operator_home(tmp_path)
    before = _metadata_snapshot(home)
    run = tmp_path / "run"
    run.mkdir()
    _launch(home, run, CLAUDE_BACKEND)
    assert _metadata_snapshot(home) == before


def test_a_wider_source_never_widens_an_existing_destination(tmp_path: Path):
    """A 0o644 source and a 0o600 destination leave the destination 0o600."""
    home = _operator_home(tmp_path)
    (home / ".claude.json").chmod(0o644)
    run = tmp_path / "run"
    run.mkdir()
    harness = run / "harness"
    harness.mkdir(parents=True)
    destination = harness / ".claude.json"
    destination.write_text(json.dumps({"projects": {"/home/operator/x": {}}}))
    destination.chmod(0o600)

    _launch(home, run, CLAUDE_BACKEND)

    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    seeded = json.loads(destination.read_text())
    assert seeded["mcpServers"] == OPERATOR_SERVERS
    assert "projects" in seeded


def test_a_wider_destination_is_narrowed_to_the_ceiling(tmp_path: Path):
    """An existing 0o644 destination is never left wider than 0o600."""
    home = _operator_home(tmp_path)
    run = tmp_path / "run"
    run.mkdir()
    harness = run / "harness"
    harness.mkdir(parents=True)
    destination = harness / ".claude.json"
    destination.write_text(json.dumps({"projects": {}}))
    destination.chmod(0o644)

    _launch(home, run, CLAUDE_BACKEND)

    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_the_seeded_file_is_private_from_creation(tmp_path: Path, monkeypatch):
    """The file is only ever observed at 0o600, never after a widening chmod."""
    home = _operator_home(tmp_path)
    (home / ".claude.json").chmod(0o644)
    run = tmp_path / "run"
    run.mkdir()
    destination = run / "harness" / ".claude.json"

    observed: list[int] = []
    real_replace = os.replace

    def _spy(source, target, *args, **kwargs):
        if str(target) == str(destination):
            observed.append(stat.S_IMODE(os.stat(source).st_mode))
        return real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(os, "replace", _spy)
    _launch(home, run, CLAUDE_BACKEND)

    # The private write moves a temporary onto the destination exactly once,
    # and that temporary is 0o600 at the instant it is observed.
    assert observed == [0o600]
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


if __name__ == "__main__":  # pragma: no cover - reproduces the red log
    print(NEGATIVE_CONTROL_MUTATION)
    with tempfile.TemporaryDirectory() as directory:
        home = _operator_home(Path(directory))
        run = Path(directory) / "run"
        run.mkdir()
        plan = _launch(home, run, CLAUDE_BACKEND)
        harness = Path(plan.environment["CLAUDE_CONFIG_DIR"])
        path = harness / ".claude.json"
        print(f"run harness home: {harness}")
        print(f".claude.json present: {path.is_file()}")
        if path.is_file():
            print(f"keys: {sorted(json.loads(path.read_text()))}")
    sys.exit(0)
