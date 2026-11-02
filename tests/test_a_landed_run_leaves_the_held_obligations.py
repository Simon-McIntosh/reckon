"""A held answer drops the duties of runs that no longer hold a live pointer.

A station computes a session's duties once per tick and keeps them until the
next one, so the answer a coordinator reads can be minutes old. A run that
lands in that window releases its live pointer, and the duty derived from that
pointer -- an automatic landing refusal above all -- is settled. The answer is
therefore re-read against the fleet as it stands now, through the snapshot
module the hook already loads by path.

Four facts are measured, each through the surface that consumes the answer:
the duties of a departed run are gone from both the crew view and the hook's
rendered list; the same duty survives while its pointer is there; a duty that
names no run is kept; and the hook still reaches no derivation module.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import obligation_snapshot, runs, station_store
from reckon.mcp_views import serve_crew_view
from tests.helpers.git import git as _git

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "reckon" / "hooks" / "coordinator_obligations.py"
PROJECT = "departed-duty-fixture"
SESSION = "departed-duty-session"
DEPARTED_RUN_ID = "run-departed-landing"
DEPARTED_NODE_ID = "departed-landing-node"
RUNLESS_NODE_ID = "runless-plan-node"
# The derivation modules the prompt path must never load; the snapshot exists
# so a turn's opening costs no fleet-wide derivation.
DERIVATION_MODULES = ("reckon._plan_html", "reckon._backends", "reckon.ledger")
# A pid no process can hold, so the drive's session resolves through the
# registration's own name rather than the runner's ambient harness identity.
_ABSENT_HARNESS_PID = 2_147_483_647

_IMPORT_SURFACE_DRIVER = """\
import importlib.util
import json
import sys
from pathlib import Path

name = "reckon.crew.obligation_snapshot"
spec = importlib.util.spec_from_file_location(name, sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[name] = module
spec.loader.exec_module(module)

payload = {
    "obligations": [{"kind": "landing-refused", "run_id": "run-departed-landing"}],
    "summary": {"count": 1, "oldest_age_seconds": 3},
}
settled = module.without_departed_run_duties(payload)
Path(sys.argv[2]).write_text(
    json.dumps({"modules": sorted(sys.modules), "settled": settled}), encoding="utf-8"
)
"""


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
        ("commit", "-q", "-m", "test: seed the departed-duty fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


@pytest.fixture()
def follower(config_home: Path):
    """Register the session exactly as a real follower arm would."""
    with (
        runs.follower_registration(PROJECT, SESSION, delivery="stream"),
        runs._project_watch_claim(PROJECT, "1h") as (acquired, _seat),
    ):
        assert acquired is True
        yield


def _duty(*, run_id: str, node: str) -> dict[str, object]:
    return {
        "kind": "landing-refused" if run_id else "plan-review",
        "run_id": run_id,
        "node": node,
        "plan": "fixture-plan",
        "age_seconds": 120,
        "next_command": "reckon crew promote --run " + (run_id or ""),
        "reason": "automatic landing refused: the merged-head gate is red",
    }


def _held_answer(*, run_id: str, node: str, runless: bool) -> dict[str, object]:
    obligations = [_duty(run_id=run_id, node=node)]
    if runless:
        obligations.append(_duty(run_id="", node=RUNLESS_NODE_ID))
    return {
        "project": PROJECT,
        "session": SESSION,
        "obligations": obligations,
        "acknowledged": [],
        "summary": {
            "count": len(obligations),
            "oldest_age_seconds": 120,
            "unreconciled_runs": 1,
        },
    }


def _publish_answer(
    runtime_root: Path,
    *,
    run_id: str,
    node: str,
    runless: bool = True,
    now: datetime | None = None,
) -> None:
    """Store one held answer, as a station tick would."""
    moment = now or datetime.now(tz=UTC)
    held = _held_answer(run_id=run_id, node=node, runless=runless)
    store = station_store.StationStore(runtime_root)
    store.connect()
    try:
        store.write_obligation(
            SESSION,
            {
                "identity": "departed-duty",
                "document": obligation_snapshot.document_for(
                    held, computed_at=moment, stream_offset=0, producer={}
                ),
            },
            changed_at=moment,
        )
        store.heartbeat(now=moment)
    finally:
        store.close()


def _record_pointer(run_id: str) -> Path:
    """Write the live pointer whose absence is the departure under test."""
    path = obligation_snapshot.live_pointer_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"run_id": run_id, "project": PROJECT}), encoding="utf-8"
    )
    return path


def _drive(repository: Path, *, runtime: Path) -> dict[str, object]:
    """Run the prompt hook as the harness does, returning its answer."""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT)
    environment["CLAUDE_PID"] = str(_ABSENT_HARNESS_PID)
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
    output = answer.get("hookSpecificOutput")
    assert isinstance(output, dict), answer
    return str(output["additionalContext"])


def _view_rows() -> list[dict[str, object]]:
    answer = serve_crew_view("obligations", project=PROJECT, session=SESSION)
    assert answer["ok"] is True, answer
    return list(answer["obligations"])


def test_a_departed_runs_duty_is_gone_from_the_view_and_the_hooks_list(
    repository: Path, runtime_root: Path, follower: None
) -> None:
    """The landing refusal of a run whose pointer is gone is not owed.

    The answer still carries it as the station stored it, so the fixture's own
    pointer count is what changes between this case and the next: nothing here
    re-derives the answer.
    """
    _publish_answer(runtime_root, run_id=DEPARTED_RUN_ID, node=DEPARTED_NODE_ID)
    assert not obligation_snapshot.live_pointer_path(DEPARTED_RUN_ID).exists()

    rows = _view_rows()
    assert [row["run_id"] for row in rows] == [""], rows
    assert DEPARTED_RUN_ID not in str(rows), "the view still serves a settled duty"
    assert RUNLESS_NODE_ID in str(rows), "a duty naming no run is not a departure"

    checklist = _injected(_drive(repository, runtime=runtime_root))
    assert DEPARTED_RUN_ID not in checklist, checklist
    assert RUNLESS_NODE_ID in checklist, checklist
    assert "1 outstanding" in checklist, checklist


def test_the_same_duty_is_kept_while_its_run_holds_a_live_pointer(
    repository: Path, runtime_root: Path, follower: None
) -> None:
    """The drop is the pointer's absence, not the row's kind or its age."""
    _publish_answer(runtime_root, run_id=DEPARTED_RUN_ID, node=DEPARTED_NODE_ID)
    _record_pointer(DEPARTED_RUN_ID)

    rows = _view_rows()
    assert [row["run_id"] for row in rows] == [DEPARTED_RUN_ID, ""], rows
    assert rows[0]["kind"] == "landing-refused", rows

    checklist = _injected(_drive(repository, runtime=runtime_root))
    assert DEPARTED_RUN_ID in checklist, checklist
    assert "2 outstanding" in checklist, checklist


def test_a_duty_that_names_no_run_is_kept(
    repository: Path, runtime_root: Path, follower: None
) -> None:
    """A row with no run id has no pointer to be checked against."""
    _publish_answer(runtime_root, run_id="", node=RUNLESS_NODE_ID, runless=False)

    rows = _view_rows()
    assert [row["run_id"] for row in rows] == [""], rows
    assert rows[0]["kind"] == "plan-review", rows

    checklist = _injected(_drive(repository, runtime=runtime_root))
    assert RUNLESS_NODE_ID in checklist, checklist


def test_settling_an_answer_loads_no_derivation_module(
    config_home: Path, tmp_path: Path
) -> None:
    """The helper the hook gains reaches no plan, backend or ledger derivation.

    The drive loads the snapshot module the way the hook does -- by file path,
    under its registered name -- applies the helper, and reports what the
    process holds. The snapshot module's own presence is the positive control:
    the absence below cannot be an absence of work.
    """
    driver = tmp_path / "settle_driver.py"
    driver.write_text(_IMPORT_SURFACE_DRIVER, encoding="utf-8")
    result_path = tmp_path / "settle.json"
    environment = dict(os.environ)
    environment["RECKON_HOME"] = str(config_home)
    completed = subprocess.run(
        [
            sys.executable,
            str(driver),
            str(REPO_ROOT / "reckon" / "crew" / "obligation_snapshot.py"),
            str(result_path),
        ],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == "", completed.stderr
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["settled"]["obligations"] == [], result["settled"]
    loaded = set(result["modules"])
    assert loaded & set(DERIVATION_MODULES) == set(), sorted(
        loaded & set(DERIVATION_MODULES)
    )
    assert "reckon.crew" not in loaded, (
        "the crew facade imports every concern module and must not be reached"
    )
    assert "reckon.crew.obligation_snapshot" in loaded, (
        "the snapshot reader is loaded by file path, so its registered name is "
        "the positive control for this absence"
    )
