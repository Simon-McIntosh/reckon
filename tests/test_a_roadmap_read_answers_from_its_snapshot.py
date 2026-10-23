"""A roadmap read answers from a snapshot keyed on the project's write generation.

A warm read composes one project's plans and sprint state plus the mounted
projects those plans name, then re-establishes on the next call that none of it
moved by walking the covered tree. The snapshot makes that re-establishment
cheap: the composed result is stored under a key of the few facts it is a
function of — each mounted project's staging index, its HEAD, and the store's
plan-write counter, with a live run's classification carried by the crew
watcher's own per-run reduction.

The driver runs out of process so the counts are taken in a process whose
imports, environment and cwd are the ones under test, and so a second process
can be asked what it answers with nothing warmed at all.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from textwrap import dedent

SOURCE_TREE = Path(__file__).resolve().parents[1]
DOCUMENT_COUNT = 200
DIRECTORY_COUNT = 10
LIVE_RUN = "r-live"
# Wide enough that nothing in the new cases below recomputes because a
# revalidation window expired: every recomposition is caused by the fact under
# test, never by the clock's ordinary advance past the window.
WIDE_WINDOW = "3600"

DRIVER = dedent(
    """
    import io
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    source = Path(os.environ["RECKON_TREE"]).resolve()
    sys.path.insert(0, str(source))
    from reckon import mcp, roadmap_snapshot, serve

    root = Path(sys.argv[1]).resolve()
    mode = sys.argv[2]
    home = root / "home"
    serve._resolve_paths(home / "mounts.json")

    if mode == "nocounter":
        # The declared negative control: the write counter is dropped from the
        # key, so a key match survives a plan-write and the call answers with
        # the composition made before it. The key is built in the snapshot
        # module, so the name is rebound there, where the builder looks the
        # name up, rather than on the module that re-exports it.
        real = roadmap_snapshot.roadmap_project_generation
        roadmap_snapshot.roadmap_project_generation = (
            lambda project, docs_dir: real(project, docs_dir)[:2] + ["stable"]
        )

    real_stat = os.stat
    real_open = __import__("builtins").open
    counts = {"covered": 0, "keys": {}}

    p1_docs = (root / "p1" / "docs").resolve()
    p2_docs = (root / "p2" / "docs").resolve()
    p1_root = (root / "p1").resolve()
    p2_root = (root / "p2").resolve()

    def _note(path, kind):
        try:
            resolved = Path(str(path)).resolve()
        except OSError:
            return
        if kind == "stat" or kind == "open":
            if str(resolved).startswith(str(p1_docs)) or str(resolved).startswith(
                str(p2_docs)
            ):
                counts["covered"] += 1
            for name, base in (("p1", p1_root), ("p2", p2_root)):
                if str(resolved).startswith(str(base)):
                    counts["keys"][name] = counts["keys"].get(name, 0) + 1
                    break

    def counting_stat(path, *args, **kwargs):
        _note(path, "stat")
        return real_stat(path, *args, **kwargs)

    def counting_open(file, *args, **kwargs):
        _note(file, "open")
        return real_open(file, *args, **kwargs)

    os.stat = counting_stat
    __import__("builtins").open = counting_open
    # Path.read_text and Path.open resolve io.open, so a wrapper on builtins
    # alone leaves every read the key makes through a Path uncounted, and the
    # bound would then measure stats only.
    io.open = counting_open

    def call():
        return mcp._roadmap("p1", view="raw")

    def reconsidered(action):
        counts["covered"] = 0
        counts["keys"] = {}
        action()
        stats = counts["covered"]
        per_project = max(counts["keys"].values(), default=0)
        return stats, per_project

    result = {"mode": mode}
    first = call()

    # Case: a second call with nothing changed walks no covered file, and its
    # key stays inside the per-project binding.
    counts["covered"] = 0
    counts["keys"] = {}
    second = call()
    result["covered_stats_second_call"] = counts["covered"]
    result["key_calls_per_project"] = max(counts["keys"].values(), default=0)
    result["equal"] = first == second

    # Case: a plan-write moves the key, so the next call recomposes.
    from reckon._store import advance_write_generation

    advance_write_generation("p1")
    recomposed, _ = reconsidered(call)
    result["write_counter_recomposed"] = recomposed
    # A counter that moved without changing the plan state still recomposes to
    # the same answer: the snapshot is keyed on the write, not on the content.
    result["unchanged_after_counter_write"] = call() == first

    # Case: a git commit in either project moves the key.
    def commit(project):
        subprocess.run(
            ["git", "-C", str(root / project), "add", "-A"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(root / project),
                "-c",
                "user.email=t@example.invalid",
                "-c",
                "user.name=t",
                "commit",
                "-q",
                "-m",
                "change",
                "--allow-empty",
            ],
            check=True,
            capture_output=True,
        )

    commit("p2")
    recomposed, _ = reconsidered(call)
    result["sibling_commit_recomposed"] = recomposed

    # Case: an in-place edit that neither reckon nor git made is seen once the
    # revalidation window has passed.
    edited = root / "p1" / "docs" / "plans" / "plan-001.html"
    text = edited.read_text(encoding="utf-8")
    edited.write_text(
        text.replace("<p>plan-001</p>", "<p>edited</p>"), encoding="utf-8"
    )
    seen_inside, _ = reconsidered(call)
    result["edited_inside_window"] = seen_inside
    os.environ["RECKON_ROADMAP_REVALIDATE_S"] = "0"
    seen_after, _ = reconsidered(call)
    result["edited_after_window"] = seen_after

    # Case: a live pointer of another project leaves this project's call alone.
    os.environ["RECKON_ROADMAP_REVALIDATE_S"] = "3600"
    call()
    live = home / "crew" / "live"
    live.mkdir(parents=True, exist_ok=True)
    (live / "r-other.json").write_text(
        json.dumps(
            {
                "run_id": "r-other",
                "project": "p2",
                "phase": "working",
                "manifest_path": str(root / "p2" / "crew" / "manifest.md"),
            }
        ),
        encoding="utf-8",
    )
    counts["covered"] = 0
    call()
    result["sibling_pointer_covered_stats"] = counts["covered"]

    print(json.dumps(result))
    """
)


def _document(slug: str, project: str) -> str:
    return dedent(
        f"""\
        <!doctype html><html><head>
        <meta name="docs-project" content="{project}">
        <meta name="reckon-type" content="plan">
        <meta name="plan-slug" content="{slug}">
        <meta name="plan-title" content="{slug}">
        <meta name="plan-status" content="active">
        <meta name="plan-impl" content="0.25">
        <meta name="plan-version" content="1">
        </head><body><p>{slug}</p></body></html>
        """
    )


def _build_project(root: Path, project: str, *, depends_on: str | None) -> Path:
    docs = root / "docs"
    plans = docs / "plans"
    plans.mkdir(parents=True)
    for number in range(DOCUMENT_COUNT):
        slug = f"plan-{number:03d}"
        name = "plan-00" if number == 0 else slug
        (plans / f"{slug}.html").write_text(_document(name, project), encoding="utf-8")
    for directory in range(DIRECTORY_COUNT):
        branch = docs / f"area-{directory:02d}"
        branch.mkdir()
        for number in range(DOCUMENT_COUNT // DIRECTORY_COUNT):
            (branch / f"work-{number:02d}.html").write_text(
                _document(f"work-{number:02d}", project), encoding="utf-8"
            )
    if depends_on is not None:
        # The link goes into the document that already carries the slug: a
        # second file claiming it would be two resources under one identity,
        # which no reader of this fixture should have to represent.
        (plans / "plan-000.html").write_text(
            _document("plan-00", project).replace(
                "<body>",
                f'<body data-links="{depends_on}:plan-000">',
            ),
            encoding="utf-8",
        )
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "base",
        ],
        check=True,
        capture_output=True,
    )
    return root


def _build(tmp_path: Path) -> Path:
    root = tmp_path / "fleet"
    root.mkdir()
    _build_project(root / "p1", "p1", depends_on="p2")
    _build_project(root / "p2", "p2", depends_on=None)
    home = root / "home"
    home.mkdir()
    (home / "mounts.json").write_text(
        json.dumps(
            {
                "p1": str((root / "p1" / "docs").resolve()),
                "p2": str((root / "p2" / "docs").resolve()),
            }
        ),
        encoding="utf-8",
    )
    return root


def _env(root: Path, window: str) -> dict:
    """The environment one driver process runs under.

    ``XDG_RUNTIME_DIR`` is pointed inside the fixture: the snapshot is
    persisted node-locally, and a node-local path shared with the machine would
    make one test's answer another's, and would let a test read or write state
    outside the repository under test.
    """
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(root),
        "XDG_RUNTIME_DIR": str(root / "runtime"),
        "RECKON_HOME": str(root / "home"),
        "RECKON_TREE": os.environ.get("RECKON_TREE", str(SOURCE_TREE)),
        "RECKON_ROADMAP_REVALIDATE_S": window,
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _run(root: Path, mode: str, driver: Path, window: str = WIDE_WINDOW) -> dict:
    completed = subprocess.run(
        [sys.executable, str(driver), str(root), mode],
        cwd=root,
        env=_env(root, window),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _spawn(root: Path, mode: str, driver: Path, window: str = WIDE_WINDOW):
    return subprocess.Popen(
        [sys.executable, str(driver), str(root), mode],
        cwd=root,
        env=_env(root, window),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _collect(process, timeout: float = 240.0) -> dict:
    out, err = process.communicate(timeout=timeout)
    assert process.returncode == 0, err[-4000:]
    return json.loads(out.strip().splitlines()[-1])


def _wait_for(path: Path, timeout: float = 120.0) -> None:
    deadline = time.time() + timeout
    while not path.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert path.exists(), f"{path} never appeared"


def _driver(tmp_path: Path) -> Path:
    path = tmp_path / "driver.py"
    path.write_text(DRIVER, encoding="utf-8")
    return path


LIVE_DRIVER = dedent(
    """
    import io
    import json
    import os
    import sys
    import time
    import types
    from pathlib import Path

    source = Path(os.environ["RECKON_TREE"]).resolve()
    sys.path.insert(0, str(source))
    sys.path.insert(0, str(source))
    from reckon import mcp, roadmap_snapshot, serve

    root = Path(sys.argv[1]).resolve()
    mode = sys.argv[2]
    home = root / "home"
    serve._resolve_paths(home / "mounts.json")

    docs = {"p1": (root / "p1" / "docs").resolve(), "p2": (root / "p2" / "docs").resolve()}
    bases = {"p1": (root / "p1").resolve(), "p2": (root / "p2").resolve()}
    real_stat = os.stat
    real_open = __import__("builtins").open
    counts = {"covered": 0, "keys": {}}

    def note(path):
        try:
            resolved = Path(str(path)).resolve()
        except OSError:
            return
        if str(resolved).startswith(str(docs["p1"])) or str(resolved).startswith(str(docs["p2"])):
            counts["covered"] += 1
        for name, base in bases.items():
            if str(resolved).startswith(str(base)):
                counts["keys"][name] = counts["keys"].get(name, 0) + 1
                break

    def counting_stat(path, *args, **kwargs):
        note(path)
        return real_stat(path, *args, **kwargs)

    def counting_open(file, *args, **kwargs):
        note(file)
        return real_open(file, *args, **kwargs)

    os.stat = counting_stat
    __import__("builtins").open = counting_open
    # Path.read_text and Path.open reach io.open directly, so a wrapper on
    # builtins alone leaves every read the key makes through a Path uncounted —
    # .git/HEAD, the ref it names, the write counter and the snapshot file.
    io.open = counting_open

    def call():
        return mcp._roadmap("p1", view="raw")

    def covered():
        counts["covered"] = 0
        call()
        return counts["covered"]

    def in_flight(report):
        body = report.get("data") if isinstance(report.get("data"), dict) else report
        found = {}
        for item in body.get("pending_work") or []:
            if isinstance(item, dict):
                found[str(item.get("slug") or "")] = [
                    str(r.get("run_id") or "") for r in (item.get("in_flight") or [])
                ]
        return found

    def pointer(alive=True):
        now = time.time()
        return {
            "run_id": "r-live",
            "project": "p1",
            "member": "worker",
            "attempt": 1,
            "phase": "working",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 60)),
            "launcher_host": "another-login-node",
            "process_alive": alive,
            "node": {"plan": "plan-00", "section": "delivery"},
            "log_path": str(home / "crew" / "runs" / "r-live" / "stream.jsonl"),
        }

    def put_pointer(alive=True):
        directory = home / "crew" / "live"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "r-live.json").write_text(json.dumps(pointer(alive)))

    def put_exit_record():
        directory = home / "crew" / "runs" / "r-live"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "exit.json").write_text(
            json.dumps(
                {
                    "run_id": "r-live",
                    "signal": 15,
                    "signal_name": "SIGTERM",
                    "exit_code": None,
                }
            )
        )

    def put_worker_record(attempt):
        # A run-directory record the classification's own key is taken over. It
        # says nothing the pointer does not already say about the run's state,
        # so writing it moves the composition a classification is a function of
        # while leaving the state word and the liveness reading where they were.
        directory = home / "crew" / "runs" / "r-live"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "worker.json").write_text(
            json.dumps({"run_id": "r-live", "attempt": attempt, "pid": 4242})
        )

    result = {"mode": mode}

    if mode == "fresh":
        result["first_call_covered"] = covered()
    elif mode == "await":
        result["first_call_covered"] = covered()
        (root / "warm").write_text("1")
        deadline = time.time() + 120
        while not (root / "go").exists() and time.time() < deadline:
            time.sleep(0.05)
        result["recomposed"] = covered()
    elif mode == "write":
        from reckon import _store

        state, version = _store.read_plan("p1", "plan-00", root / "p1", artifact_type="plan")
        state["impl"] = 0.5
        result["written_version"] = _store.write_plan(
            "p1", "plan-00", state, version, root / "p1", artifact_type="plan"
        )
    elif mode == "live_exit":
        put_pointer()
        result["in_flight_before"] = in_flight(call())
        put_pointer(alive=False)
        put_exit_record()
        result["recomposed"] = covered()
        result["in_flight_after"] = in_flight(call())
    elif mode == "liveness":
        put_pointer()
        call()
        from reckon.crew import recovery_watch as watch

        watch._fresh_liveness = lambda ptr: (False, True, None)
        result["recomposed"] = covered()
    elif mode == "clock":
        run_dir = home / "crew" / "runs" / "r-live"
        run_dir.mkdir(parents=True, exist_ok=True)
        awaited = root / "awaited-output.txt"
        newline = chr(10)
        (run_dir / "manifest.md").write_text(
            newline.join(
                [
                    "status: waiting",
                    "wait_condition: the job has finished",
                    "wait_file: " + json.dumps([str(awaited)]),
                    "resume_brief: read the job's output and continue",
                ]
            )
            + newline
        )
        pointer_record = pointer()
        pointer_record["phase"] = "waiting"
        pointer_record["manifest_path"] = str(run_dir / "manifest.md")
        directory = home / "crew" / "live"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "r-live.json").write_text(json.dumps(pointer_record))
        (run_dir / "stream.jsonl").write_text("{}" + chr(10))

        def classification_of(report):
            body = report.get("data") if isinstance(report.get("data"), dict) else report
            for item in body.get("pending_work") or []:
                for row in item.get("in_flight") or []:
                    if row.get("run_id") == "r-live":
                        return str(row.get("classification") or "")
            return ""

        result["classification_before"] = classification_of(call())
        moment = time.time() + 3000
        # The key, the snapshot's own freshness check and the reduction behind
        # both read the clock from the snapshot module, so the moment is moved
        # there; serve keeps its own name for the request path around it.
        roadmap_snapshot.time = types.SimpleNamespace(time=lambda: moment)
        serve.time = types.SimpleNamespace(time=lambda: moment)
        import reckon.crew.recovery_classification as clocked
        clocked._utc_seconds = lambda: moment
        counts["covered"] = 0
        result["classification_after"] = classification_of(call())
        result["recomposed"] = counts["covered"]
    elif mode == "reuse":
        put_pointer()
        put_worker_record(1)
        call()
        before = serve._roadmap_live_readings("p1")[0]
        result["state_before"] = before[0][1] if before else ""
        result["liveness_before"] = before[0][2:5] if before else []
        put_worker_record(2)
        after = serve._roadmap_live_readings("p1")[0]
        result["state_after"] = after[0][1] if after else ""
        result["liveness_after"] = after[0][2:5] if after else []
        result["row_before"] = before[0][6] if before else ""
        result["row_after"] = after[0][6] if after else ""
        result["recomposed"] = covered()
    else:
        raise SystemExit("unknown mode: " + mode)

    print(json.dumps(result))
    """
)


def _live_driver(tmp_path: Path) -> Path:
    path = tmp_path / "live_driver.py"
    path.write_text(LIVE_DRIVER, encoding="utf-8")
    return path


def _snapshot_file(root: Path) -> Path:
    return root / "runtime" / "reckon" / "p1" / "roadmap-snapshot.json"


def _listed(by_plan: dict) -> list[str]:
    """Every run id a report lists as in flight, whichever plan holds it."""
    return sorted(run for runs in (by_plan or {}).values() for run in runs)


def test_a_second_read_walks_no_covered_file(tmp_path: Path) -> None:
    root = _build(tmp_path)
    measured = _run(root, "measure", _driver(tmp_path))
    assert measured["covered_stats_second_call"] == 0, measured
    assert measured["key_calls_per_project"] <= 8, measured
    assert measured["equal"] is True, measured


def test_a_store_write_makes_the_next_read_recompose(tmp_path: Path) -> None:
    root = _build(tmp_path)
    measured = _run(root, "measure", _driver(tmp_path))
    assert measured["write_counter_recomposed"] > 0, measured
    assert measured["unchanged_after_counter_write"] is True, measured


def test_the_write_counter_is_what_carries_the_store_write(tmp_path: Path) -> None:
    """The declared negative control: drop the counter and the case goes stale."""
    root = _build(tmp_path)
    measured = _run(root, "nocounter", _driver(tmp_path))
    assert measured["write_counter_recomposed"] == 0, measured


def test_a_sibling_commit_and_an_in_place_edit_are_seen(tmp_path: Path) -> None:
    root = _build(tmp_path)
    measured = _run(root, "measure", _driver(tmp_path))
    assert measured["sibling_commit_recomposed"] > 0, measured
    assert measured["edited_inside_window"] == 0, measured
    assert measured["edited_after_window"] > 0, measured


def test_a_sibling_projects_live_pointer_does_not_disturb_this_read(
    tmp_path: Path,
) -> None:
    root = _build(tmp_path)
    measured = _run(root, "measure", _driver(tmp_path))
    assert measured["sibling_pointer_covered_stats"] == 0, measured


def test_a_fresh_process_answers_from_the_persisted_snapshot(tmp_path: Path) -> None:
    """A process that warmed nothing answers its first call from the file."""
    root = _build(tmp_path)
    driver = _live_driver(tmp_path)

    cold = _run(root, "fresh", driver)
    assert cold["first_call_covered"] > DOCUMENT_COUNT, cold
    assert _snapshot_file(root).exists(), "the snapshot was never persisted"

    warm = _run(root, "fresh", driver)
    # A process that started cold pays one existence check per mount while it
    # resolves the mounts — two for the two mounted projects — and not one
    # visit to a covered file. The cold arm above walks the tree; this one does
    # not, inside the section's per-project budget of eight.
    assert warm["first_call_covered"] <= 8, warm


def test_a_store_write_in_one_process_forces_another_process_to_recompute(
    tmp_path: Path,
) -> None:
    """A plan write in one process reaches a reader in another, unwindowed."""
    root = _build(tmp_path)
    driver = _live_driver(tmp_path)

    # A first process warms the snapshot, so the second starts warm.
    _run(root, "fresh", driver)
    waiter = _spawn(root, "await", driver)
    _wait_for(root / "warm")
    written = _run(root, "write", driver)
    assert written["written_version"] > 1, written
    (root / "go").write_text("1")
    measured = _collect(waiter)

    assert measured["first_call_covered"] <= 8, measured
    assert measured["recomposed"] > DOCUMENT_COUNT, measured


def test_a_live_runs_exit_record_moves_the_next_read_out_of_flight(
    tmp_path: Path,
) -> None:
    """The run's ending is seen at once, and a dead reading with it."""
    root = _build(tmp_path)
    driver = _live_driver(tmp_path)

    measured = _run(root, "live_exit", driver)
    assert _listed(measured["in_flight_before"]) == [LIVE_RUN], measured
    assert _listed(measured["in_flight_after"]) == [], measured
    assert measured["recomposed"] > DOCUMENT_COUNT, measured

    # The liveness arm: every file untouched, only the process-table reading
    # turns false, so nothing but the reading can have moved the key.
    liveness = _run(root, "liveness", driver)
    assert liveness["recomposed"] > DOCUMENT_COUNT, liveness


def test_a_runs_own_record_moves_the_key_with_its_state_unchanged(
    tmp_path: Path,
) -> None:
    """A run detail that is not its state still moves the next read.

    The state word and the liveness reading are coarser than the composition a
    classification is a function of. A record in the run's own directory moves
    while both of them stand still, so a key holding only those two matches and
    the read answers from a composition one of whose inputs has moved.
    """
    root = _build(tmp_path)
    measured = _run(root, "reuse", _live_driver(tmp_path))

    assert measured["state_before"] == measured["state_after"], measured
    assert measured["liveness_before"] == measured["liveness_after"], measured
    assert measured["row_before"] != measured["row_after"], measured
    assert measured["recomposed"] > DOCUMENT_COUNT, measured


def test_an_advanced_clock_recomputes_a_run_whose_files_did_not_move(
    tmp_path: Path,
) -> None:
    """The classification follows the clock, so the key must follow it too.

    The run declares a wait on a file that never appears. Nothing is written
    and nothing ends: the only thing that moves is the clock, and at a moment
    past the declared horizon the same run is overdue rather than waiting. The
    assertion is taken from the read's own result, so a read that matched its
    key and answered from the composition taken before the horizon is what the
    case rejects.
    """
    root = _build(tmp_path)
    measured = _run(root, "clock", _live_driver(tmp_path))

    assert measured["classification_before"] == "waiting", measured
    assert measured["classification_after"] == "wait-aged", measured
    assert measured["recomposed"] > DOCUMENT_COUNT, measured


class _InterleavingMemo:
    """A mounts memo whose every store is followed by a reader's observation.

    The module stores the pair of a stamp and the mounts resolved under it as a
    single value. A reader running on another thread could otherwise land
    between two stores and take the new stamp beside the mounts resolved under
    the old one; this stands in for that reader, and it reads the same way a
    concurrent caller would: the pair first, and a memo written as two separate
    attributes leaves it no pair to read, so it takes the two halves one at a
    time and records whatever combination it lands on.
    """

    # The two fields a memo written as separate stores would carry, so a reader
    # handed that shape sees a real pair of values rather than an attribute it
    # never had.
    stamp: int | None = None
    mounts: dict[str, Path] | None = None

    def __init__(self, observe) -> None:
        object.__setattr__(self, "_observe", observe)
        object.__setattr__(self, "pair", None)

    def __setattr__(self, name: str, value: object) -> None:
        object.__setattr__(self, name, value)
        observe = object.__getattribute__(self, "_observe")
        observe(self)


def test_a_second_call_with_the_same_stamp_resolves_nothing_again(
    monkeypatch, tmp_path: Path
) -> None:
    """A mounts file that has not moved is not resolved a second time.

    The resolution stats every mounted docs tree, so repeating it per request
    is the cost the memo exists to remove. The count is the measurement: the
    resolver runs once for the first call and not at all for the second.
    """
    from reckon import roadmap_snapshot

    monkeypatch.setattr(roadmap_snapshot._MOUNTS_MEMO, "pair", None)
    resolutions: list[int] = []

    def resolve() -> dict[str, Path]:
        resolutions.append(1)
        return {"p1": tmp_path / "p1" / "docs"}

    first = roadmap_snapshot.remembered_mounts(7, resolve)
    second = roadmap_snapshot.remembered_mounts(7, resolve)

    assert first == second == {"p1": tmp_path / "p1" / "docs"}
    assert len(resolutions) == 1, resolutions


def test_a_moved_mounts_file_is_resolved_again(monkeypatch, tmp_path: Path) -> None:
    """A mounts file that moved is resolved afresh, and its answer replaces the old."""
    from reckon import roadmap_snapshot

    monkeypatch.setattr(roadmap_snapshot._MOUNTS_MEMO, "pair", None)
    resolutions: list[int] = []

    def resolve() -> dict[str, Path]:
        resolutions.append(1)
        name = f"p{len(resolutions)}"
        return {name: tmp_path / name / "docs"}

    first = roadmap_snapshot.remembered_mounts(7, resolve)
    second = roadmap_snapshot.remembered_mounts(8, resolve)
    third = roadmap_snapshot.remembered_mounts(7, resolve)

    assert first == {"p1": tmp_path / "p1" / "docs"}, first
    assert second == {"p2": tmp_path / "p2" / "docs"}, second
    assert third == {"p3": tmp_path / "p3" / "docs"}, third
    assert len(resolutions) == 3, resolutions


def test_a_reader_never_sees_a_stamp_beside_mounts_from_another_store(
    monkeypatch, tmp_path: Path
) -> None:
    """The stamp and the mounts a reader returns come from one store.

    Each store is followed by a reader, the way a second thread would land on
    it. Every observation must pair a stamp with the mounts the resolver
    returned for that stamp: an observation carrying a stamp beside the mounts
    of the previous resolution is a reader answering from a set the stamp does
    not describe, which is what a memo written as two attributes allows.
    """
    from reckon import roadmap_snapshot

    resolved_for: dict[int, dict[str, Path]] = {}
    observations: list[tuple[object, object]] = []

    def observe(memo) -> None:
        pair = getattr(memo, "pair", None)
        if pair is None:
            pair = (getattr(memo, "stamp", None), getattr(memo, "mounts", None))
        observations.append((pair[0], pair[1]))

    def resolver_for(stamp: int):
        def resolve() -> dict[str, Path]:
            mounts = {"p": tmp_path / f"p{stamp}" / "docs"}
            resolved_for[stamp] = mounts
            return mounts

        return resolve

    memo = _InterleavingMemo(observe)
    monkeypatch.setattr(roadmap_snapshot, "_MOUNTS_MEMO", memo)

    roadmap_snapshot.remembered_mounts(7, resolver_for(7))
    roadmap_snapshot.remembered_mounts(8, resolver_for(8))

    assert observations, "the probe observed no store, so this case checks nothing"
    mixed = [
        (stamp, mounts)
        for stamp, mounts in observations
        if mounts is not None and mounts is not resolved_for.get(stamp)
    ]
    assert mixed == [], observations
