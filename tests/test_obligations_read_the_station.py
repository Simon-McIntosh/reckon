"""The obligations hook and the exported snapshot read the station.

The station computes each live session's duties once per change and keeps them
in its node-local store. Three properties are measured here, each driven
through the surface that consumes it rather than asserted about the writer:

* the checklist the hook injects from the station equals the one the derivation
  itself formats over the same fixture fleet;
* a station whose heartbeat is older than the stale window is reported stale,
  with its age, rather than served as a current answer;
* an unchanged fleet leaves the exported snapshot file untouched across a
  minute, while a changed fleet rewrites it -- the second half is what keeps a
  writer that never writes from passing the first half.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import obligation_snapshot, runs, station_obligations, station_store
from reckon.crew.obligations import obligations as obligations_view
from reckon.hooks.coordinator_obligations import format_checklist
from reckon.mcp_views import serve_crew_view
from tests.helpers.git import git as _git

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "reckon" / "hooks" / "coordinator_obligations.py"
PROJECT = "station-obligations-fixture"
SESSION = "station-obligations-session"
BLOCKED_RUN_ID = "run-station-blocked"
BLOCKED_NODE_ID = "station-blocked-node"
STATION_ONLY_RUN_ID = "run-station-only"
# An age the checklist renders from wall-clock distances, such as "2s" or "50m0s".
AGE = re.compile(r"\d+d\d+h|\d+h\d+m|\d+m\d+s|\d+s")
_ABSENT_HARNESS_PID = 2_147_483_647


@pytest.fixture()
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(home / "mounts.json"))
    return home


@pytest.fixture()
def runtime_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The node-local root the hook and the station both resolve."""
    root = tmp_path / "runtime"
    root.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(root))
    return root


@pytest.fixture()
def repository(tmp_path: Path, config_home: Path) -> Path:
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the station fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


@pytest.fixture()
def follower(config_home: Path):
    """Register the session exactly as a real follower arm would.

    The registration is armed with a registration the liveness reader counts --
    a delivering one beside a held producer seat -- because the station derives
    only for sessions something is actually delivering to.
    """
    with (
        runs.follower_registration(PROJECT, SESSION, delivery="stream"),
        runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat),
    ):
        assert acquired is True
        yield


def _write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def _live_blocked_run(repository: Path, tmp_path: Path) -> Path:
    """Record one live run whose manifest says it is blocked."""
    manifest = tmp_path / "manifests" / f"{BLOCKED_RUN_ID}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(f"node: {BLOCKED_NODE_ID}\nstatus: blocked\n", encoding="utf-8")
    head = _git(repository, "rev-parse", "HEAD")
    _write_json(
        runs.pointer_path(BLOCKED_RUN_ID),
        {
            "run_id": BLOCKED_RUN_ID,
            "project": PROJECT,
            "session": SESSION,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": head,
            "process_alive": False,
            "role": "implement",
            "manifest_path": str(manifest),
            "node": {
                "id": BLOCKED_NODE_ID,
                "plan": "fixture-plan",
                "section": "fixture-section",
                "time_budget": "20m",
                "write_paths": ["seed.txt"],
            },
        },
    )
    return manifest


def _station(runtime_root: Path, *, now: datetime) -> station_store.StationStore:
    """A station store holding the fixture fleet's computed obligations."""
    store = station_store.StationStore(runtime_root)
    store.connect()
    station_obligations.refresh(store, project=PROJECT, changed=True, now=now)
    store.heartbeat(now=now)
    return store


def _drive(repository: Path, *, runtime: Path | None = None) -> dict[str, object]:
    """Run the prompt hook as the harness does, carrying hook JSON on stdin."""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT)
    environment["CLAUDE_PID"] = str(_ABSENT_HARNESS_PID)
    if runtime is not None:
        environment["XDG_RUNTIME_DIR"] = str(runtime)
    completed = subprocess.run(
        [sys.executable, str(HOOK), "--hook", "prompt"],
        input=json.dumps(
            {
                "session_id": SESSION,
                "cwd": str(repository),
                "hook_event_name": "UserPromptSubmit",
            }
        ),
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == "", completed.stderr
    return json.loads(completed.stdout) if completed.stdout else {}


def _injected(answer: dict[str, object]) -> str:
    return str(answer["hookSpecificOutput"]["additionalContext"])  # type: ignore[index]


def _normalised(checklist: str) -> str:
    return AGE.sub("<age>", checklist)


def test_the_station_root_is_the_session_hosts_root(tmp_path: Path) -> None:
    """The hook's resolver and the store's pick the same directory, always.

    The hook loads the snapshot module by file path and cannot import the crew
    package, so its candidate order is a copy. This pins the copy against the
    resolver the station and the session host actually use, for each candidate.
    ``run_user_root`` is a real directory here so the per-user candidate is the
    one under test rather than a path that happens not to exist.
    """
    per_user = tmp_path / "run-user"
    per_user.mkdir()
    xdg = tmp_path / "xdg-root"
    scratch = tmp_path / "scratch"
    for environment, run_user in (
        ({"XDG_RUNTIME_DIR": str(xdg), "TMPDIR": str(scratch)}, None),
        ({"TMPDIR": str(scratch)}, per_user),
        ({"TMPDIR": str(scratch)}, None),
    ):
        assert obligation_snapshot.station_runtime_candidate(
            environ=environment, run_user_root=run_user
        ) == station_store.runtime_root_source(
            environ=environment, run_user_root=run_user
        )


def test_the_station_database_is_the_stores_own_file() -> None:
    """One name for the store file, so the hook reads the file the station wrote."""
    assert obligation_snapshot.STATION_DATABASE_NAME == station_store.DATABASE_NAME


def test_the_hooks_answer_from_the_station_equals_the_derivation(
    repository: Path, tmp_path: Path, runtime_root: Path, follower: None
) -> None:
    """The injected checklist is the derivation's own, read from the station."""
    _live_blocked_run(repository, tmp_path)
    _station(runtime_root, now=datetime.now(tz=UTC))
    derived = obligations_view(PROJECT, SESSION)

    answer = _drive(repository, runtime=runtime_root)

    assert BLOCKED_RUN_ID in _injected(answer)
    assert _normalised(_injected(answer)) == _normalised(
        format_checklist(dict(derived))
    )


def test_a_stale_station_heartbeat_is_reported_with_its_age(
    repository: Path, tmp_path: Path, runtime_root: Path, follower: None
) -> None:
    """A stopped station speaks its heartbeat's age, not a duty list."""
    _live_blocked_run(repository, tmp_path)
    long_ago = datetime.now(tz=UTC) - timedelta(minutes=5)
    _station(runtime_root, now=long_ago)

    answer = _drive(repository, runtime=runtime_root)

    line = _injected(answer)
    assert "\n" not in line, line
    assert "not current" in line
    assert "heartbeat" in line and " old" in line
    assert "5m" in line, f"the age is not named in minutes: {line}"
    assert BLOCKED_RUN_ID not in line, "a stale station must not answer with duties"


def _exported(runtime_root: Path, config_home: Path, *, now: datetime) -> Path:
    """Store and export the fixture fleet, returning the exported file."""
    store = _station(runtime_root, now=now)
    path = station_obligations.export_snapshot(
        store,
        project=PROJECT,
        home=config_home / "crew",
        now=now,
    )
    assert path is not None, "the first export always writes"
    store.close()
    return path


def test_an_unchanged_fleet_writes_no_new_snapshot_over_sixty_seconds(
    repository: Path,
    tmp_path: Path,
    runtime_root: Path,
    config_home: Path,
    follower: None,
) -> None:
    """A fleet that does not move leaves the exported file exactly as it was."""
    _live_blocked_run(repository, tmp_path)
    moment = datetime.now(tz=UTC)
    path = _exported(runtime_root, config_home, now=moment)
    before = path.stat()
    body = path.read_bytes()

    store = station_store.StationStore(runtime_root)
    store.connect()
    for elapsed in (20.0, 60.0, 120.0):
        assert (
            station_obligations.export_snapshot(
                store,
                project=PROJECT,
                home=config_home / "crew",
                now=moment + timedelta(seconds=elapsed),
            )
            is None
        ), "an unchanged fleet rewrote the snapshot"
    store.close()

    after = path.stat()
    assert (after.st_mtime_ns, after.st_ino) == (before.st_mtime_ns, before.st_ino)
    assert path.read_bytes() == body


def test_a_changed_fleet_rewrites_the_export(
    repository: Path,
    tmp_path: Path,
    runtime_root: Path,
    config_home: Path,
    follower: None,
) -> None:
    """The no-write case above is not passed by a writer that never writes."""
    _live_blocked_run(repository, tmp_path)
    moment = datetime.now(tz=UTC)
    path = _exported(runtime_root, config_home, now=moment)
    before = path.read_bytes()

    runs.pointer_path(BLOCKED_RUN_ID).unlink()
    store = station_store.StationStore(runtime_root)
    store.connect()
    station_obligations.refresh(
        store, project=PROJECT, changed=True, now=moment + timedelta(seconds=20)
    )
    rewritten = station_obligations.export_snapshot(
        store,
        project=PROJECT,
        home=config_home / "crew",
        now=moment + timedelta(seconds=20),
    )
    store.close()

    assert rewritten == path
    assert path.read_bytes() != before, "a fleet that changed left the export stale"
    assert BLOCKED_RUN_ID not in path.read_text(encoding="utf-8")


def test_a_reader_off_the_node_reads_the_exported_snapshot(
    repository: Path,
    tmp_path: Path,
    runtime_root: Path,
    config_home: Path,
    follower: None,
) -> None:
    """With no store on this node, the exported snapshot answers the hook."""
    _live_blocked_run(repository, tmp_path)
    _exported(runtime_root, config_home, now=datetime.now(tz=UTC))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    derived = obligations_view(PROJECT, SESSION)

    answer = _drive(repository, runtime=elsewhere)

    assert _normalised(_injected(answer)) == _normalised(
        format_checklist(dict(derived))
    )


def test_an_exported_snapshot_with_a_stale_heartbeat_names_its_age(
    repository: Path,
    tmp_path: Path,
    runtime_root: Path,
    config_home: Path,
    follower: None,
) -> None:
    """An off-node reader is told how old the station's last word is."""
    _live_blocked_run(repository, tmp_path)
    long_ago = datetime.now(tz=UTC) - timedelta(minutes=5)
    store = _station(runtime_root, now=long_ago)
    station_obligations.export_snapshot(
        store, project=PROJECT, home=config_home / "crew", now=long_ago
    )
    store.close()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    line = _injected(_drive(repository, runtime=elsewhere))

    assert "not current" in line and "5m" in line, line
    assert BLOCKED_RUN_ID not in line


def test_the_obligations_view_answers_from_the_station(
    repository: Path,
    tmp_path: Path,
    runtime_root: Path,
    follower: None,
) -> None:
    """The crew view serves the station's duties, not a fresh derivation."""
    _live_blocked_run(repository, tmp_path)
    now = datetime.now(tz=UTC)
    store = _station(runtime_root, now=now)
    held = {
        "project": PROJECT,
        "session": SESSION,
        "obligations": [
            {
                "kind": "run-held",
                "run_id": STATION_ONLY_RUN_ID,
                "age_seconds": 120,
                "detail": "the station's own answer",
                "action": {"verb": "reconcile", "command": "reckon crew reconcile"},
            }
        ],
        "acknowledged": [],
        "summary": {"count": 1, "oldest_age_seconds": 120, "unreconciled_runs": 1},
    }
    store.write_obligation(
        SESSION,
        {
            "identity": "station-only",
            "document": obligation_snapshot.document_for(
                held, computed_at=now, stream_offset=0, producer={}
            ),
        },
        changed_at=now,
    )
    store.close()

    answer = serve_crew_view("obligations", project=PROJECT, session=SESSION)
    derived = obligations_view(PROJECT, SESSION)

    assert answer["ok"] is True
    assert [row["run_id"] for row in answer["obligations"]] == [STATION_ONLY_RUN_ID]
    assert STATION_ONLY_RUN_ID not in [
        row.get("run_id") for row in derived["obligations"]
    ], "the fixture's derivation must not hold the station's answer"
    narrowed = serve_crew_view(
        "obligations", project=PROJECT, session=SESSION, fields=["run_id"]
    )
    assert narrowed["obligations"][0]["run_id"] == STATION_ONLY_RUN_ID
    assert "detail" not in narrowed["obligations"][0], "the fields filter is shared"
