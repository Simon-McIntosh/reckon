"""The one-time split of an aggregate runs list into per-run ledger files.

Every test here drives the entry point against a synthesised project in a
throwaway git repository whose state is reached through a throwaway config home
and mount registry. The split is a write operation over other repositories'
durable state, so each test's teardown watches the workstation's own mounted
projects: a migration that resolved to a real project would deposit the case's
own run ids there, so the check asserts that none of them appear.

The watch is by the case's own rows rather than by a stat fingerprint of the
live trees, because a real promotion lands into a mounted project at any moment
and an mtime comparison reads that unrelated write as a leak. The teardown
asserts it can see at least one real project's state, because an instrument
that sees nothing cannot support a claim that nothing leaked.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import _store, flight, ledger
from reckon.cli import main as cli_main

PROJECT = "alpha"
SIBLING = "beta"
MEMBERS = [{"id": "impl-one", "backend": "local", "launch": "cli"}]
HOLDS = [{"backend": "local", "until": "2027-01-01T00:00:00Z"}]


# ── The workstation's own state, watched across every test ──────────────────


# The run ids the current case seeded through the fixture ledger, recorded so
# the teardown can look for exactly these in the workstation's own trees.
_seeded_run_ids: set[str] = set()


def _run_id_values(node: object) -> set[str]:
    """Every ``run_id`` string a parsed ledger structure carries, at any depth."""
    found: set[str] = set()
    if isinstance(node, list):
        for item in node:
            found |= _run_id_values(item)
    elif isinstance(node, dict):
        value = node.get("run_id")
        if isinstance(value, str):
            found.add(value)
        for item in node.values():
            found |= _run_id_values(item)
    return found


def _run_ids_under(project: str, docs: Path) -> set[str]:
    """Every run id a mounted project's own state carries, by name or by row."""
    state = Path(docs) / "state" / project
    found = {path.stem for path in (state / "runs").glob("*.json")}
    ledger_file = state / "crew.json"
    if ledger_file.is_file():
        try:
            payload = json.loads(ledger_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return found
        found |= _run_id_values(payload)
    return found


@pytest.fixture(scope="session")
def workstation_state():
    """The workstation's own mounted projects, read before any override.

    Session-scoped because the suite replaces the configuration home for every
    test, and a wider scope is instantiated first: read at function scope this
    would resolve the throwaway home's absent registry and watch nothing. The
    at-least-one-project assertion is the control for that — read too late it
    fails loudly rather than watching an empty set in silence.
    """
    mounts = flight.mounted_project_docs()
    assert any(
        (Path(docs) / "state" / project).exists() for project, docs in mounts.items()
    ), (
        "the leak scan sees no state under the workstation's mounts, so its "
        "untouched assertion would be vacuous"
    )
    return mounts


@pytest.fixture()
def home(tmp_path, monkeypatch, workstation_state):
    """A throwaway config home, with the real mounted projects watched."""
    mounts = workstation_state
    _seeded_run_ids.clear()
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(config_home / "mounts.json"))
    yield config_home
    # The registry in force here is the case's; a case that re-registered a
    # mounted project's name has pointed the split at that project's tree, so
    # watch wherever the name resolves now, falling back to the live path.
    registered = flight.mounted_project_docs()
    leaked: list[str] = []
    for project, live in sorted(mounts.items()):
        watched = registered.get(project, live)
        leaked.extend(
            f"{run_id} under {project} at {watched}"
            for run_id in sorted(_run_ids_under(project, watched) & _seeded_run_ids)
        )
    assert not leaked, (
        "the split wrote the case's own rows into the workstation's own "
        f"mounted state: {leaked}"
    )


# ── The synthesised project ────────────────────────────────────────────────


def _state_dir(repo: Path, project: str) -> Path:
    return repo / "docs" / "state" / project


def _write_ledger(repo: Path, project: str, data: dict) -> Path:
    """Seed an aggregate through the ledger's own envelope writer."""
    path = _state_dir(repo, project) / "crew.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    _store._write_json_envelope(path, project, ledger.LEDGER_SLUG, data, 0)
    for row in data.get("runs") or ():
        if isinstance(row, dict) and isinstance(row.get("run_id"), str):
            _seeded_run_ids.add(row["run_id"])
    return path


def _rows(*run_ids: str) -> list[dict]:
    return [
        ledger.build_record(
            run_id=run_id,
            plan="plan-a",
            gate="passed",
            node=f"node-{index}",
            completed_at=f"2027-01-0{index + 1}T00:10:00Z",
        )
        for index, run_id in enumerate(run_ids)
    ]


def _seed_repo(repo: Path) -> None:
    """Commit the seeded state so a later commit is visibly separate."""
    (repo / "docs").mkdir(parents=True, exist_ok=True)
    (repo / "other.txt").write_text("outside the state tree\n", encoding="utf-8")
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "docs", "other.txt"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _mount(home: Path, mounts: dict[str, Path]) -> None:
    (home / "mounts.json").write_text(
        json.dumps({project: str(docs) for project, docs in mounts.items()}),
        encoding="utf-8",
    )


def _run_split(*extra: str) -> dict:
    result = CliRunner().invoke(cli_main, ["crew", "split-runs", *extra])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").strip()


def _commit_files(repo: Path, revision: str = "HEAD") -> set[str]:
    return set(_git(repo, "show", "--name-only", "--format=", revision).split())


# ── One project, row for row ────────────────────────────────────────────────


def test_the_split_writes_every_row_to_its_own_file_in_one_commit(
    home, tmp_path
) -> None:
    repo = tmp_path / "repo"
    rows = _rows("r-alpha", "r-beta", "r-gamma")
    rows[1]["store_write"] = {"status": "recorded"}
    _write_ledger(repo, PROJECT, {"members": MEMBERS, "runs": rows, "holds": HOLDS})
    _seed_repo(repo)
    _mount(home, {PROJECT: repo / "docs"})

    payload = _run_split()

    entry = payload["projects"][PROJECT]
    assert payload["dry_run"] is False
    assert entry["rows"] == 3
    assert entry["missing"] == 3
    assert entry["written"] == 3
    assert entry["commit"]
    for row in rows:
        target = _state_dir(repo, PROJECT) / "runs" / f"{row['run_id']}.json"
        assert target.read_text(encoding="utf-8") == ledger.serialize_run(row)
    # Every field of the row survives, including the keys a promotion does not
    # carry onto the aggregate.
    carried = json.loads(
        (_state_dir(repo, PROJECT) / "runs" / "r-beta.json").read_text(encoding="utf-8")
    )
    assert carried["store_write"] == {"status": "recorded"}
    data = json.loads((_state_dir(repo, PROJECT) / "crew.json").read_text())["data"]
    assert "runs" not in data
    assert data["members"] == MEMBERS
    assert data["holds"] == HOLDS
    expected = {f"docs/state/{PROJECT}/crew.json"} | {
        f"docs/state/{PROJECT}/runs/{row['run_id']}.json" for row in rows
    }
    assert _commit_files(repo) == expected
    assert _git(repo, "status", "--porcelain").strip() == ""


def test_an_empty_runs_list_migrates_to_the_key_s_removal_alone(home, tmp_path) -> None:
    repo = tmp_path / "repo"
    _write_ledger(repo, PROJECT, {"members": MEMBERS, "runs": [], "holds": HOLDS})
    _seed_repo(repo)
    _mount(home, {PROJECT: repo / "docs"})

    payload = _run_split()

    entry = payload["projects"][PROJECT]
    assert entry["rows"] == 0
    assert entry["written"] == 0
    assert entry["commit"]
    data = json.loads((_state_dir(repo, PROJECT) / "crew.json").read_text())["data"]
    assert "runs" not in data
    assert data["members"] == MEMBERS
    assert data["holds"] == HOLDS
    assert _commit_files(repo) == {f"docs/state/{PROJECT}/crew.json"}
    assert not (_state_dir(repo, PROJECT) / "runs").exists()


def test_a_second_invocation_writes_nothing_and_commits_nothing(home, tmp_path) -> None:
    repo = tmp_path / "repo"
    _write_ledger(
        repo,
        PROJECT,
        {"members": MEMBERS, "runs": _rows("r-alpha", "r-beta"), "holds": HOLDS},
    )
    _seed_repo(repo)
    _mount(home, {PROJECT: repo / "docs"})
    _run_split()
    head = _head(repo)
    aggregate = (_state_dir(repo, PROJECT) / "crew.json").read_bytes()

    payload = _run_split()

    entry = payload["projects"][PROJECT]
    assert entry["skipped"] == "the ledger holds no runs key"
    assert entry["written"] == 0
    assert entry["commit"] is None
    assert _head(repo) == head
    assert (_state_dir(repo, PROJECT) / "crew.json").read_bytes() == aggregate
    assert _git(repo, "status", "--porcelain").strip() == ""


# ── Refusals, siblings, and promotions that landed beside the list alone ────


def test_a_differing_file_stops_that_project_while_a_sibling_migrates(
    home, tmp_path
) -> None:
    repo = tmp_path / "repo"
    rows = _rows("r-one", "r-two")
    _write_ledger(repo, PROJECT, {"members": MEMBERS, "runs": rows, "holds": HOLDS})
    clashing = _state_dir(repo, PROJECT) / "runs" / "r-two.json"
    clashing.parent.mkdir(parents=True)
    stale = dict(rows[1], completed_at="2026-01-01T00:00:00Z")
    clashing.write_text(ledger.serialize_run(stale), encoding="utf-8")
    _write_ledger(repo, SIBLING, {"members": [], "runs": _rows("r-three"), "holds": []})
    _seed_repo(repo)
    _mount(home, {PROJECT: repo / "docs", SIBLING: repo / "docs"})

    payload = _run_split()

    entry = payload["projects"][PROJECT]
    assert entry["differing"] == 1
    assert entry["differing_files"] == [str(clashing.resolve())]
    assert entry["written"] == 0
    assert entry["commit"] is None
    assert entry["stopped"]
    # Nothing was written for the rows that would have migrated.
    assert not (_state_dir(repo, PROJECT) / "runs" / "r-one.json").exists()
    assert clashing.read_text(encoding="utf-8") == ledger.serialize_run(stale)
    data = json.loads((_state_dir(repo, PROJECT) / "crew.json").read_text())["data"]
    assert len(data["runs"]) == 2
    # The sibling migrated in the same run, and its commit is the only one.
    sibling = payload["projects"][SIBLING]
    assert sibling["written"] == 1
    assert sibling["commit"]
    assert _commit_files(repo) == {
        f"docs/state/{SIBLING}/crew.json",
        f"docs/state/{SIBLING}/runs/r-three.json",
    }
    assert _git(repo, "status", "--porcelain").strip() == ""


def test_a_promoted_run_beside_the_list_is_left_alone_and_not_counted(
    home, tmp_path
) -> None:
    repo = tmp_path / "repo"
    _write_ledger(
        repo, PROJECT, {"members": [], "runs": _rows("r-listed"), "holds": []}
    )
    promoted_row = ledger.build_record(
        run_id="r-window",
        plan="plan-a",
        gate="passed",
        node="node-window",
        completed_at="2027-02-01T00:00:00Z",
    )
    promoted = _state_dir(repo, PROJECT) / "runs" / "r-window.json"
    promoted.parent.mkdir(parents=True)
    promoted.write_text(ledger.serialize_run(promoted_row), encoding="utf-8")
    _seed_repo(repo)
    _mount(home, {PROJECT: repo / "docs"})

    payload = _run_split()

    entry = payload["projects"][PROJECT]
    assert entry["rows"] == 1
    assert entry["missing"] == 1
    assert entry["identical"] == 0
    assert entry["differing"] == 0
    assert promoted.read_text(encoding="utf-8") == ledger.serialize_run(promoted_row)
    assert _commit_files(repo) == {
        f"docs/state/{PROJECT}/crew.json",
        f"docs/state/{PROJECT}/runs/r-listed.json",
    }


# ── The dry run ─────────────────────────────────────────────────────────────


def test_a_dry_run_reports_the_counts_and_writes_nothing(home, tmp_path) -> None:
    repo = tmp_path / "repo"
    _write_ledger(
        repo,
        PROJECT,
        {"members": MEMBERS, "runs": _rows("r-alpha", "r-beta"), "holds": HOLDS},
    )
    _seed_repo(repo)
    _mount(home, {PROJECT: repo / "docs"})
    head = _head(repo)
    aggregate = (_state_dir(repo, PROJECT) / "crew.json").read_bytes()

    payload = _run_split("--dry-run")

    entry = payload["projects"][PROJECT]
    assert payload["dry_run"] is True
    assert entry["rows"] == 2
    assert entry["missing"] == 2
    assert entry["identical"] == 0
    assert entry["differing"] == 0
    assert entry["written"] == 0
    assert entry["commit"] is None
    assert not (_state_dir(repo, PROJECT) / "runs").exists()
    assert _head(repo) == head
    assert (_state_dir(repo, PROJECT) / "crew.json").read_bytes() == aggregate


def test_a_dry_run_names_the_differing_file_the_real_run_would_refuse(
    home, tmp_path
) -> None:
    repo = tmp_path / "repo"
    rows = _rows("r-alpha", "r-two")
    _write_ledger(repo, PROJECT, {"members": [], "runs": rows, "holds": []})
    same = _state_dir(repo, PROJECT) / "runs" / "r-alpha.json"
    same.parent.mkdir(parents=True)
    same.write_text(ledger.serialize_run(rows[0]), encoding="utf-8")
    clashing = _state_dir(repo, PROJECT) / "runs" / "r-two.json"
    stale = dict(rows[1], completed_at="2026-01-01T00:00:00Z")
    clashing.write_text(ledger.serialize_run(stale), encoding="utf-8")
    _seed_repo(repo)
    _mount(home, {PROJECT: repo / "docs"})
    head = _head(repo)

    payload = _run_split("--dry-run")

    entry = payload["projects"][PROJECT]
    assert payload["dry_run"] is True
    assert entry["rows"] == 2
    assert entry["missing"] == 0
    assert entry["identical"] == 1
    assert entry["differing"] == 1
    assert entry["differing_files"] == [str(clashing.resolve())]
    assert entry["stopped"]
    assert _head(repo) == head


def test_a_project_without_a_ledger_or_runs_key_is_reported_as_skipped(
    home, tmp_path
) -> None:
    repo = tmp_path / "repo"
    _state_dir(repo, PROJECT).mkdir(parents=True)
    _write_ledger(repo, SIBLING, {"members": MEMBERS, "holds": HOLDS})
    _seed_repo(repo)
    _mount(home, {PROJECT: repo / "docs", SIBLING: repo / "docs"})
    head = _head(repo)

    payload = _run_split()

    assert payload["projects"][PROJECT]["skipped"] is not None
    assert payload["projects"][SIBLING]["skipped"] is not None
    assert payload["projects"][PROJECT]["commit"] is None
    assert _head(repo) == head
    assert _git(repo, "status", "--porcelain").strip() == ""
