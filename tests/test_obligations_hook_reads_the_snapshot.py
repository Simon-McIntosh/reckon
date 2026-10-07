"""The prompt hook answers from the session's published snapshot, never deriving.

Every case here drives the hook as the harness does: the prompt path runs
against a synthesised configuration home with hook JSON on stdin. The import
surface is driven through a fresh interpreter that runs the prompt path and
then lists ``sys.modules``, because the property under test is what the process
loaded -- a derivation imported anywhere on the path defeats the snapshot,
however fast it looks.

The mutating direction is covered too: a snapshot that is not fresh is answered
by one line naming the reason and the remedy, and the snapshot file is
byte-identical afterwards, because the hook reads that file and never writes
it. A not-fresh snapshot whose producer is mid-reload is the exception: the
last snapshot's list is injected under the reloading heading with no remedy
when a live producer runs older code, when a producer has just gone and the
publication is within the reload window, or when the seat recorded a reload
intent within it; only an absence older than that window earns the remedy. A fresh snapshot is injected exactly as the derived checklist was, and a
fresh snapshot naming a duty the live fleet does not have is injected as it
stands, which is what shows the list comes from the file rather than a
derivation.
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

from reckon.crew import obligation_snapshot, runs
from reckon.crew.obligations import obligations as obligations_view
from reckon.hooks.coordinator_obligations import format_checklist

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "reckon" / "hooks" / "coordinator_obligations.py"
PROJECT = "snapshot-hook-fixture"
SESSION = "snapshot-hook-session"
BLOCKED_RUN_ID = "run-snapshot-blocked"
BLOCKED_NODE_ID = "snapshot-fixture-node"
SCORING_RUN_ID = "run-snapshot-scoring"
SCORING_NODE_ID = "snapshot-scoring-node"
CONTINUATION_RUN_ID = "run-snapshot-continuation"
FOREIGN_BACKEND = "codex"
REMEDY = f"reckon crew watch --ensure --project {PROJECT}"

# The derivation modules the prompt path must never load. Their presence in a
# fresh prompt process is the defect the snapshot exists to remove.
DERIVATION_MODULES = ("reckon._plan_html", "reckon._backends", "reckon.ledger")

# An age the checklist renders from wall-clock distances, such as "2s" or "50m0s".
AGE = re.compile(r"\d+d\d+h|\d+h\d+m|\d+m\d+s|\d+s")

# A pid no process can hold, so the drive's session resolves through the
# registration's own name rather than the runner's ambient harness identity.
_ABSENT_HARNESS_PID = 2_147_483_647

_IMPORT_SURFACE_DRIVER = """\
import json
import sys
from pathlib import Path

from reckon.hooks.coordinator_obligations import main

exit_code = main(["--hook", "prompt"])
Path(sys.argv[1]).write_text(json.dumps(sorted(sys.modules)), encoding="utf-8")
raise SystemExit(exit_code)
"""


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments], cwd=repository, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


@pytest.fixture()
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(home / "mounts.json"))
    return home


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
        ("commit", "-q", "-m", "test: seed snapshot hook fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


@pytest.fixture()
def follower(config_home: Path):
    """Register the session exactly as a real follower arm would.

    The registration is written through ``runs`` rather than by hand, so the
    hook's own resolution of the registration directory and of the session name
    is exercised against the writer's, not against a second copy of the rule.
    """
    with runs.follower_claim(PROJECT, SESSION) as claim:
        assert claim[0] is True, claim[1]
        yield


def _live_run(
    repository: Path, tmp_path: Path, *, run_id: str, node_id: str, backend: str = ""
) -> Path:
    """Record one live run, scoring when it names a head no review has read."""
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    head = _git(repository, "rev-parse", "HEAD")
    manifest.write_text(
        f"node: {node_id}\nstatus: complete\ncommits: [{head}]\n"
        if backend
        else f"node: {node_id}\nstatus: blocked\n",
        encoding="utf-8",
    )
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(repository),
        "worktree": str(repository),
        "base_sha": head,
        "process_alive": False,
        "role": "implement",
        "manifest_path": str(manifest),
        "node": {
            "id": node_id,
            "plan": "fixture-plan",
            "section": "fixture-section",
            "time_budget": "20m",
            "write_paths": ["seed.txt"],
        },
    }
    if backend:
        pointer["backend"] = backend
    _write_json(runs.pointer_path(run_id), pointer)
    return manifest


def _producer(**overrides: object) -> dict[str, object]:
    producer = {
        "pid": os.getpid(),
        "pid_start_time": obligation_snapshot.process_start_time(os.getpid()),
        "started_at": datetime.now(tz=UTC).isoformat(),
        "code_stamp": obligation_snapshot.source_code_stamp(),
    }
    producer.update(overrides)
    return producer


def _publish(payload: dict[str, object], **producer_overrides: object) -> Path:
    document = obligation_snapshot.document_for(
        payload,
        computed_at=datetime.now(tz=UTC),
        stream_offset=0,
        producer=_producer(**producer_overrides),
    )
    return obligation_snapshot.write_snapshot(PROJECT, SESSION, document)


def _publish_derived(**producer_overrides: object) -> Path:
    """Publish the fixture's own derived payload as a fresh snapshot."""
    return _publish(obligations_view(PROJECT, SESSION), **producer_overrides)


def _publish_with_continuation(command: str, **producer_overrides: object) -> Path:
    """Publish the derived payload plus one lane-bearing continuation duty.

    A remedy that continues an existing run keeps the lane that run was carried
    on, so a lane-bearing redispatch is the duty that exercises the hook's lane
    edit: a new-work remedy names no lane, so it cannot.
    """
    payload = obligations_view(PROJECT, SESSION)
    obligations = [
        *(payload.get("obligations") or ()),
        {
            "kind": "review",
            "run_id": CONTINUATION_RUN_ID,
            "node": CONTINUATION_RUN_ID,
            "plan": "fixture-plan",
            "age_seconds": 0,
            "next_command": command,
        },
    ]
    payload["obligations"] = obligations
    payload["summary"] = {**(payload.get("summary") or {}), "count": len(obligations)}
    return _publish(payload, **producer_overrides)


def _prompt_payload(repository: Path) -> dict[str, object]:
    return {
        "session_id": SESSION,
        "cwd": str(repository),
        "hook_event_name": "UserPromptSubmit",
    }


def _drive(
    payload: dict[str, object], *, tmp_path: Path, modules_path: Path | None = None
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT)
    environment["CLAUDE_PID"] = str(_ABSENT_HARNESS_PID)
    if modules_path is None:
        command = [sys.executable, str(HOOK), "--hook", "prompt"]
    else:
        driver = tmp_path / "prompt_import_surface_driver.py"
        driver.write_text(_IMPORT_SURFACE_DRIVER, encoding="utf-8")
        command = [sys.executable, str(driver), str(modules_path)]
    completed = subprocess.run(
        command,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert completed.stderr == "", completed.stderr
    return completed


def _injected(completed: subprocess.CompletedProcess[str]) -> str:
    return json.loads(completed.stdout)["hookSpecificOutput"]["additionalContext"]


def _line(completed: subprocess.CompletedProcess[str]) -> str:
    """The single not-fresh line one drive emitted."""
    text = _injected(completed)
    assert text.strip(), "a not-fresh drive must speak its line"
    assert "\n" not in text, text
    return text


def _normalised(checklist: str) -> str:
    return AGE.sub("<age>", checklist)


def test_the_prompt_path_loads_no_derivation_module(
    repository: Path, tmp_path: Path, follower: None
) -> None:
    """A fresh prompt process reaches no plan, backend or ledger derivation.

    The scoring run contributes a composed new-work remedy and the fixture adds
    a lane-bearing continuation remedy, so the path exercises the hook's lane
    edit on both branches: the lane a continuation keeps and the lane a new-work
    remedy leaves unnamed. The drive's stdout is the injected checklist, so the
    absence of the three modules cannot be an absence of work: the snapshot was
    read, formatted and injected.
    """
    _live_run(
        repository,
        tmp_path,
        run_id=SCORING_RUN_ID,
        node_id=SCORING_NODE_ID,
        backend=FOREIGN_BACKEND,
    )
    continuation = (
        f"reckon crew redispatch --run {CONTINUATION_RUN_ID} "
        f"--backend {FOREIGN_BACKEND} --reason continue"
    )
    _publish_with_continuation(continuation)
    modules_path = tmp_path / "sys-modules.json"

    completed = _drive(
        _prompt_payload(repository), tmp_path=tmp_path, modules_path=modules_path
    )

    assert completed.returncode == 0
    checklist = _injected(completed)
    assert checklist.startswith(f"reckon obligations for session {SESSION}"), checklist
    assert SCORING_RUN_ID in checklist
    assert f"--backend {FOREIGN_BACKEND}" in checklist, (
        "the lane-bearing continuation must keep its run's lane for the lane "
        "edit to be exercised"
    )
    loaded = set(json.loads(modules_path.read_text(encoding="utf-8")))
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


def test_each_not_fresh_reason_speaks_once_and_leaves_the_snapshot_alone(
    repository: Path, tmp_path: Path, follower: None
) -> None:
    """A remedy speaks once and changes nothing; a reload shows the list.

    The absent and aged cases each name their reason and the remedy. The
    stale-code case is no longer a remedy: a live producer on older code is
    reloading, so it answers with the last snapshot's list under the reloading
    heading and withholds the remedy instead.
    """
    absent = _drive(_prompt_payload(repository), tmp_path=tmp_path)
    path = obligation_snapshot.snapshot_path(PROJECT, SESSION)
    line = _line(absent)
    assert "no producer" in line
    assert REMEDY in line
    assert not path.exists(), "the hook must not create a snapshot"

    _publish(obligations_view(PROJECT, SESSION), code_stamp="0" * 64)
    before = path.read_bytes()
    stale_code = _drive(_prompt_payload(repository), tmp_path=tmp_path)
    text = _injected(stale_code)
    header = text.splitlines()[0]
    assert "producer reloading" in header, header
    assert re.search(r"last snapshot \d+s old", header), header
    assert REMEDY not in text, "a reloading producer earns no remedy"
    assert path.read_bytes() == before, "a read must not rewrite the snapshot"

    document = obligation_snapshot.document_for(
        obligations_view(PROJECT, SESSION),
        computed_at=datetime.now(tz=UTC) - timedelta(seconds=900),
        stream_offset=0,
        producer=_producer(),
    )
    obligation_snapshot.write_snapshot(PROJECT, SESSION, document)
    before = path.read_bytes()
    aged = _drive(_prompt_payload(repository), tmp_path=tmp_path)
    line = _line(aged)
    assert re.search(r"the snapshot is \d+s old", line), line
    assert REMEDY in line
    assert path.read_bytes() == before


def test_a_reused_pid_is_not_fresh(
    repository: Path, tmp_path: Path, follower: None
) -> None:
    """The recorded start time is what makes a pid evidence, so reuse fails it.

    The publication is aged past the reload window for the reuse case, so the
    two readings stay distinguishable: a pid accepted on its start time would
    leave the snapshot merely stale, while a pid whose start time does not
    match is no producer at all, and only the latter earns the no-producer
    line rather than the reloading list.
    """
    _live_run(repository, tmp_path, run_id=BLOCKED_RUN_ID, node_id=BLOCKED_NODE_ID)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        start = obligation_snapshot.process_start_time(child.pid)
        assert start, "the child's start time must be readable for this case to bite"

        _publish(obligations_view(PROJECT, SESSION), pid=child.pid, pid_start_time=start)
        accepted = _drive(_prompt_payload(repository), tmp_path=tmp_path)
        assert accepted.stdout, "the true start time must read as fresh"

        document = obligation_snapshot.document_for(
            obligations_view(PROJECT, SESSION),
            computed_at=datetime.now(tz=UTC) - timedelta(seconds=600),
            stream_offset=0,
            producer=_producer(pid=child.pid, pid_start_time=str(int(start) - 1)),
        )
        obligation_snapshot.write_snapshot(PROJECT, SESSION, document)
        reused = _drive(_prompt_payload(repository), tmp_path=tmp_path)
    finally:
        child.kill()
        child.wait()

    line = _line(reused)
    assert "no producer" in line, line
    assert REMEDY in line


def test_a_fresh_snapshot_is_formatted_as_todays_output(
    repository: Path, tmp_path: Path, follower: None
) -> None:
    """The injected checklist is the derived one, and a repeat stays silent."""
    _live_run(
        repository, tmp_path, run_id=BLOCKED_RUN_ID, node_id=BLOCKED_NODE_ID
    )
    _publish_derived()
    derived = obligations_view(PROJECT, SESSION)

    first = _drive(_prompt_payload(repository), tmp_path=tmp_path)
    repeated = _drive(_prompt_payload(repository), tmp_path=tmp_path)

    assert first.returncode == 0
    assert _normalised(_injected(first)) == _normalised(
        format_checklist(dict(derived))
    )
    assert repeated.returncode == 0
    assert repeated.stdout == "", "an unchanged list is not injected twice"


def test_the_injected_list_is_the_snapshot_not_the_live_fleet(
    repository: Path, tmp_path: Path, follower: None
) -> None:
    """A duty only the snapshot names is injected, so the file is the source."""
    _live_run(repository, tmp_path, run_id=BLOCKED_RUN_ID, node_id=BLOCKED_NODE_ID)
    only_in_the_snapshot = {
        "project": PROJECT,
        "session": SESSION,
        "obligations": [
            {
                "kind": "blocked",
                "run_id": "run-only-in-the-snapshot",
                "node": "snapshot-only-node",
                "age_seconds": 4,
                "next_command": "read /tmp/snapshot-only; resolve the blocker",
            }
        ],
        "acknowledged": [],
        "summary": {"count": 1, "oldest_age_seconds": 4, "unreconciled_runs": 0},
    }
    _publish(only_in_the_snapshot)

    completed = _drive(_prompt_payload(repository), tmp_path=tmp_path)

    checklist = _injected(completed)
    assert "run-only-in-the-snapshot" in checklist
    assert BLOCKED_RUN_ID not in checklist, (
        "the live blocked run must not appear: the hook read the snapshot, not "
        "the fleet"
    )


def test_a_live_producer_on_older_code_shows_the_last_snapshot(
    repository: Path, tmp_path: Path, follower: None
) -> None:
    """A reload in progress shows the list under ``producer reloading``.

    A live producer whose recorded stamp is older than the source has not
    caught up yet, which is a reload and not a reason to cycle the seat. The
    first drive shows the fresh list; flipping only the stamp must speak again
    -- the same duties under a different sentence -- and the heading then
    carries the snapshot's age and the reloading words with no remedy. A third
    drive inside the same reload is quiet again, because nothing changed.
    """
    _live_run(repository, tmp_path, run_id=BLOCKED_RUN_ID, node_id=BLOCKED_NODE_ID)
    path = _publish_derived()
    fresh = _drive(_prompt_payload(repository), tmp_path=tmp_path)
    assert BLOCKED_RUN_ID in _injected(fresh)
    assert REMEDY not in _injected(fresh)

    _publish(obligations_view(PROJECT, SESSION), code_stamp="0" * 64)
    before = path.read_bytes()
    reloading = _drive(_prompt_payload(repository), tmp_path=tmp_path)
    text = _injected(reloading)
    header = text.splitlines()[0]
    assert BLOCKED_RUN_ID in text, text
    assert "producer reloading" in header, header
    assert re.search(r"last snapshot \d+s old", header), header
    assert REMEDY not in text, "a reloading producer earns no remedy"
    assert path.read_bytes() == before, "a read must not rewrite the snapshot"

    repeated = _drive(_prompt_payload(repository), tmp_path=tmp_path)
    assert repeated.stdout == "", "an unchanged list and reload state is not repeated"


def test_a_restarting_producer_shows_the_last_snapshot(
    repository: Path, tmp_path: Path, follower: None
) -> None:
    """A producer gone for a moment is reloading, not absent.

    The recorded pid belongs to no process, but the last publication is
    seconds old -- the gap while a replacement image starts and publishes --
    so the last list is shown under ``producer reloading`` and the absence
    remedy is withheld until the reload window passes.
    """
    _live_run(repository, tmp_path, run_id=BLOCKED_RUN_ID, node_id=BLOCKED_NODE_ID)
    _publish(
        obligations_view(PROJECT, SESSION),
        pid=_ABSENT_HARNESS_PID,
        pid_start_time="1",
    )

    completed = _drive(_prompt_payload(repository), tmp_path=tmp_path)

    text = _injected(completed)
    header = text.splitlines()[0]
    assert BLOCKED_RUN_ID in text, text
    assert "producer reloading" in header, header
    assert re.search(r"last snapshot \d+s old", header), header
    assert REMEDY not in text, "a reloading producer earns no remedy"


def test_a_recorded_reload_intent_shows_the_last_snapshot(
    repository: Path, tmp_path: Path, follower: None
) -> None:
    """The seat's own reload declaration counts while it is fresh.

    The seat writes ``reload_started_at`` as an in-place replacement begins, so
    a reload declared moments ago over an older publication still shows that
    publication's list with its age and no remedy, exactly as a live stale
    producer does.
    """
    _live_run(repository, tmp_path, run_id=BLOCKED_RUN_ID, node_id=BLOCKED_NODE_ID)
    document = obligation_snapshot.document_for(
        obligations_view(PROJECT, SESSION),
        computed_at=datetime.now(tz=UTC) - timedelta(seconds=600),
        stream_offset=0,
        producer=_producer(pid=_ABSENT_HARNESS_PID, pid_start_time="1"),
    )
    obligation_snapshot.write_snapshot(PROJECT, SESSION, document)
    seat = runs.watch_lock_path(PROJECT)
    seat.parent.mkdir(parents=True, exist_ok=True)
    _write_json(seat, {"reload_started_at": datetime.now(tz=UTC).isoformat()})

    completed = _drive(_prompt_payload(repository), tmp_path=tmp_path)

    text = _injected(completed)
    header = text.splitlines()[0]
    assert BLOCKED_RUN_ID in text, text
    assert "producer reloading" in header, header
    assert re.search(r"last snapshot \d+[dhms]", header), header
    assert REMEDY not in text, "a reloading producer earns no remedy"


def test_an_absence_past_the_reload_window_names_the_remedy(
    repository: Path, tmp_path: Path, follower: None
) -> None:
    """Past the window, with no live producer, the remedy is the answer.

    No live producer has published for longer than the reload window, so the
    seat is absent rather than reloading: the no-producer line carries the
    remedy again. A reload intent older than the window does not hold it back,
    because a healthy reload does not take that long.
    """
    _live_run(repository, tmp_path, run_id=BLOCKED_RUN_ID, node_id=BLOCKED_NODE_ID)
    document = obligation_snapshot.document_for(
        obligations_view(PROJECT, SESSION),
        computed_at=datetime.now(tz=UTC) - timedelta(seconds=600),
        stream_offset=0,
        producer=_producer(pid=_ABSENT_HARNESS_PID, pid_start_time="1"),
    )
    obligation_snapshot.write_snapshot(PROJECT, SESSION, document)
    seat = runs.watch_lock_path(PROJECT)
    seat.parent.mkdir(parents=True, exist_ok=True)
    _write_json(
        seat,
        {
            "reload_started_at": (
                datetime.now(tz=UTC) - timedelta(seconds=600)
            ).isoformat()
        },
    )

    completed = _drive(_prompt_payload(repository), tmp_path=tmp_path)

    line = _line(completed)
    assert "no producer" in line, line
    assert REMEDY in line, line
