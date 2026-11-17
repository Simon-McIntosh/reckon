"""A replay fixes its cohort before comparing the promotion drain."""

import json
import threading

import pytest

from reckon.crew import promotion_requests, runs
from scripts.replay_promotion_drain import (
    collect,
    protect_sources,
    replay,
    validate_dataset,
)

START = "2026-11-16T21:00:00Z"
END = "2026-11-16T22:30:00Z"


def test_locked_promotion_reads_its_target_in_the_operational_scope(
    tmp_path, monkeypatch
):
    from reckon.crew import open_run_index, promotion

    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    record = {"run_id": "target", "project": "fixture", "repo": str(tmp_path)}
    monkeypatch.setattr(promotion, "read_dispatch_detail", lambda record: {})

    def checked(checkout, run_id):
        assert open_run_index.pointers() == (record,)
        raise RuntimeError("scope checked")

    monkeypatch.setattr(promotion, "_require_committable_checkout", checked)
    with pytest.raises(RuntimeError, match="scope checked"):
        promotion._complete_locked("target", record=record, gate="passed")


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def _request(run_id, stamp):
    return {"run_id": run_id, "request_id": run_id, "requested_at": stamp}


def test_selection_uses_request_time_and_keeps_retained_settlements(tmp_path):
    crew = tmp_path / "crew"
    for name, stamp in (
        ("before", "2026-11-16T20:59:59Z"),
        ("lower", START),
        ("middle", "2026-11-16T22:00:00+00:00"),
        ("upper", END),
    ):
        _write(
            crew / "promotion-requests" / f"{name}.json.settled", _request(name, stamp)
        )
    result = collect(crew, START, END)
    assert result["header"]["count"] == 2
    assert [r["request"]["run_id"] for r in validate_dataset(result)] == [
        "lower",
        "middle",
    ]


def test_retained_request_supersedes_the_same_historical_request(tmp_path):
    crew, census = tmp_path / "crew", tmp_path / "census"
    request = _request("run", START)
    _write(census / "sources.json", {"requests": {"run": request}})
    _write(crew / "promotion-requests" / "run.json", {**request, "phase": "promoted"})
    result = collect(crew, START, END, census)
    assert result["header"]["count"] == 1
    assert validate_dataset(result)[0]["request"]["phase"] == "promoted"


def test_distinct_attempts_of_one_run_remain_distinct(tmp_path):
    crew, census = tmp_path / "crew", tmp_path / "census"
    request = _request("run", START)
    _write(census / "sources.json", {"requests": {"run": request}})
    _write(crew / "promotion-requests" / "run.json", {**request, "request_id": "retry"})
    assert collect(crew, START, END, census)["header"]["count"] == 2


def test_archived_run_records_and_historical_pointer_are_copied(tmp_path):
    crew, census = tmp_path / "crew", tmp_path / "census"
    _write(
        census / "sources.json",
        {
            "requests": {"run": _request("run", START)},
            "pointers": {"run": {"run_id": "run", "repo": "/fixture"}},
        },
    )
    manifest = crew / "archive" / "runs" / "run" / "manifest.md"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("status: complete\n")
    before = {
        p.relative_to(tmp_path): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    [row] = validate_dataset(collect(crew, START, END, census))
    assert row["pointer"]["repo"] == "/fixture"
    assert row["run_files"]["manifest.md"] == "status: complete\n"
    after = {
        p.relative_to(tmp_path): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    assert after == before


def test_empty_request_directory_reports_no_rate(tmp_path):
    data = collect(tmp_path / "empty", START, END)
    assert data["header"]["count"] == 0
    with pytest.raises(ValueError, match="no rate"):
        validate_dataset(data)


def test_changed_copied_set_is_refused(tmp_path):
    crew = tmp_path / "crew"
    _write(crew / "promotion-requests" / "run.json", _request("run", START))
    data = collect(crew, START, END)
    data["rows"][0]["request"]["run_id"] = "changed"
    with pytest.raises(ValueError, match="digest"):
        validate_dataset(data)


def test_relocation_uses_the_longest_path_and_clears_process_handles():
    from scripts.replay_promotion_drain import _relocate

    assert _relocate(
        {"paths": ["/source/worker/file", "unchanged"], "pid": 123},
        {"/source": "/private", "/source/worker": "/private/worker"},
    ) == {"paths": ["/private/worker/file", "unchanged"], "pid": None}
    assert _relocate({"pid": 123, "note": "unchanged"}, {}) == {
        "pid": None,
        "note": "unchanged",
    }


def test_private_repository_copies_packs_without_changing_the_source(tmp_path):
    from scripts.replay_promotion_drain import _repository, git

    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-q", "-b", "main")
    git(source, "config", "user.name", "Fixture")
    git(source, "config", "user.email", "fixture@example.invalid")
    (source / "present.txt").write_text("known content\n")
    git(source, "add", "present.txt")
    git(source, "-c", "commit.gpgsign=false", "commit", "-qm", "Seed fixture")
    git(source, "repack", "-ad")
    (source / "loose.txt").write_text("loose content\n")
    git(source, "add", "loose.txt")
    git(source, "-c", "commit.gpgsign=false", "commit", "-qm", "Extend fixture")
    packs = source / ".git/objects/pack"
    before = {path.name: path.read_bytes() for path in packs.iterdir()}
    assert any(name.endswith(".pack") for name in before)
    revision = git(source, "rev-parse", "HEAD")
    private = tmp_path / "private"
    _repository(
        private,
        {
            "root": str(source),
            "objects": str(source / ".git/objects"),
            "revision": revision,
        },
    )
    assert (private / "present.txt").read_text() == "known content\n"
    assert git(private, "rev-parse", "HEAD") == revision
    (private / ".git/objects/info/alternates").unlink()
    assert git(private, "show", "HEAD:loose.txt") == "loose content"
    assert {path.name: path.read_bytes() for path in packs.iterdir()} == before
    for name, content in before.items():
        if name.endswith((".pack", ".idx")):
            assert (private / ".git/objects/pack" / name).read_bytes() == content


def test_real_replay_exercises_guards_and_a_positive_control(tmp_path, monkeypatch):
    import os
    from pathlib import Path

    from reckon import ledger
    from reckon.crew import promotion_scope
    from reckon.crew.dispatch_launch import worker_scratch_root

    appended = []
    append = ledger.append_run

    def observed_append(*args, **kwargs):
        assert worker_scratch_root().is_relative_to(Path(os.environ["TMPDIR"]))
        assert Path(os.environ["XDG_RUNTIME_DIR"]).is_relative_to(
            Path(os.environ["TMPDIR"])
        )
        scope = promotion_scope._BRANCH_WRITES.get()
        assert scope and scope.get("lease"), "real ledger write has no branch lease"
        appended.append(True)
        return append(*args, **kwargs)

    monkeypatch.setattr(ledger, "append_run", observed_append)
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(tmp_path / "foreign-scratch"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "foreign-runtime"))
    crew = tmp_path / "source-crew"
    request = {
        **_request("r-unavailable", START),
        "project": "missing",
        "kind": "complete",
        "options": {"gate": "passed"},
        "priority_override": None,
    }
    _write(crew / "promotion-requests" / "run.json", request)
    data = collect(crew, START, END)
    before = {
        p.relative_to(crew): p.read_bytes() for p in crew.rglob("*") if p.is_file()
    }
    result = replay(
        data,
        sources={"projects": {}, "heads": {}, "sha256": "fixture"},
        assets=tmp_path,
        scratch_root=tmp_path,
    )
    assert result["positive_control"]["phase"] == "promoted"
    assert appended, "the positive control did not reach the real ledger writer"
    assert result["requests"] == 1
    assert result["verdicts"] == {"promotion-refused": 1}
    assert result["successful_promotions_per_hour"] == 0
    assert {
        p.relative_to(crew): p.read_bytes() for p in crew.rglob("*") if p.is_file()
    } == before


def test_replay_store_is_seeded_before_it_becomes_authoritative(tmp_path, monkeypatch):
    from reckon import ledger, run_store
    from scripts.replay_promotion_drain import _seed_store

    monkeypatch.setenv("RECKON_RUN_STORE", str(tmp_path / "store.db"))
    repository = tmp_path / "repository"
    target = ledger.run_path("fixture", "known", repository)
    target.parent.mkdir(parents=True)
    target.write_text(
        ledger.serialize_run({"run_id": "known", "worktree": "/copied/tree"})
    )
    assert run_store.stored_run_rows("fixture") is None
    assert _seed_store(
        {"fixture": repository}, {"/copied/tree": str(tmp_path / "worker")}
    ) == {"fixture": 1}
    [row] = run_store.stored_run_rows("fixture")
    assert row["run_id"] == "known"
    assert row["worktree"] == str(tmp_path / "worker")
    with run_store.RunStore() as store:
        assert store._conn.execute("PRAGMA synchronous").fetchone() == (2,)


def test_empty_real_replay_reports_no_rate(tmp_path):
    with pytest.raises(ValueError, match="no rate"):
        replay(collect(tmp_path / "empty", START, END), sources={}, assets=tmp_path)


def test_replay_refuses_a_write_to_its_original_inputs(tmp_path):
    crew = tmp_path / "source"
    crew.mkdir()
    source = crew / "known-present.json"
    source.write_text("original")
    data = {"header": {"source_crew": str(crew)}}
    protection = protect_sources(data, {"projects": {}}, tmp_path / "output.json")
    try:
        with pytest.raises(PermissionError, match="original source"):
            source.write_text("changed")
        assert source.read_text() == "original"
        assert protection["refusals"] == 1
    finally:
        protection["active"] = False


def _queue_for_repository(run_id, repository, *, role="implement"):
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "repo": str(repository),
            "role": role,
            "project": "burst-fixture",
            "session": "burst-session",
            "node": {"id": run_id, "role": role},
            "phase": "complete",
        },
    )
    return promotion_requests.request_promotion(run_id)


def _write_branch(repository):
    from reckon.crew import promotion_scope

    acquire = getattr(promotion_scope, "_acquire_branch_write", None)
    if acquire is not None:
        acquire(repository)


def test_a_commitless_promotion_finishes_beside_a_landing(tmp_path):
    first, report = "r-held-landing", "r-report"
    _queue_for_repository(first, tmp_path / "repo")
    _queue_for_repository(report, tmp_path / "repo", role="review")
    landed, release, reported = threading.Event(), threading.Event(), threading.Event()

    def execute(request):
        if request["run_id"] == first:
            _write_branch(tmp_path / "repo")
            landed.set()
            assert release.wait(5), "the fixture did not release its landing"
        else:
            reported.set()
        return {"ok": True, "record": {"commits": []}}

    service = promotion_requests.PromotionService(width=1, execute=execute)
    try:
        service.tick()
        assert landed.wait(2)
        service.tick()
        assert reported.wait(0.5), "commit-less promotion waited behind a landing"
        assert not release.is_set()
    finally:
        release.set()
        service.begin_drain()
        assert service.wait_for_in_flight(10) == []


def test_two_landings_on_one_branch_never_overlap(tmp_path):
    first, second = "r-first-landing", "r-second-landing"
    _queue_for_repository(first, tmp_path / "repo")
    _queue_for_repository(second, tmp_path / "repo")
    began, release, overlapped = threading.Event(), threading.Event(), threading.Event()

    def execute(request):
        if request["run_id"] == first:
            _write_branch(tmp_path / "repo")
            began.set()
            assert release.wait(5), "the fixture did not release its landing"
        else:
            assert began.wait(2)
            _write_branch(tmp_path / "repo")
            if not release.is_set():
                overlapped.set()
        return {"ok": True, "record": {"commits": []}}

    service = promotion_requests.PromotionService(width=2, execute=execute)
    try:
        service.tick()
        assert began.wait(2)
        assert not overlapped.wait(0.5), (
            "two landing executors overlapped on one branch"
        )
    finally:
        release.set()
        service.begin_drain()
        assert service.wait_for_in_flight(10) == []


def test_landings_for_different_repositories_do_not_wait(tmp_path):
    first, second = "r-first-repository", "r-other-repository"
    _queue_for_repository(first, tmp_path / "one")
    _queue_for_repository(second, tmp_path / "two")
    began, release, concurrent = threading.Event(), threading.Event(), threading.Event()

    def execute(request):
        if request["run_id"] == first:
            _write_branch(tmp_path / "one")
            began.set()
            assert release.wait(5), "the fixture did not release its landing"
        else:
            _write_branch(tmp_path / "two")
            concurrent.set()
        return {"ok": True, "record": {"commits": []}}

    service = promotion_requests.PromotionService(width=1, execute=execute)
    try:
        service.tick()
        assert began.wait(2)
        service.tick()
        assert concurrent.wait(0.5), "another repository waited behind a landing"
    finally:
        release.set()
        service.begin_drain()
        assert service.wait_for_in_flight(10) == []


def test_linked_checkouts_of_one_branch_share_a_lease(tmp_path):
    from reckon.crew.promotion_scope import _branch_write_key

    shared = tmp_path / "objects"
    keys = []
    for name in ("one", "two"):
        private = shared / "worktrees" / name
        private.mkdir(parents=True)
        (private / "commondir").write_text("../..")
        (private / "HEAD").write_text("ref: refs/heads/main\n")
        checkout = tmp_path / name
        checkout.mkdir()
        (checkout / ".git").write_text(f"gitdir: {private}\n")
        keys.append(_branch_write_key(checkout))
    assert keys[0] == keys[1]
    (private / "HEAD").write_text("ref: refs/heads/other\n")
    assert _branch_write_key(checkout) != keys[0]


def test_a_detached_checkout_keeps_its_lease_key_when_it_commits(tmp_path):
    from reckon.crew.promotion_scope import _branch_write_key

    directory = tmp_path / ".git"
    directory.mkdir()
    (directory / "HEAD").write_text("a" * 40)
    before = _branch_write_key(tmp_path)
    (directory / "HEAD").write_text("b" * 40)
    assert _branch_write_key(tmp_path) == before


def test_a_refusal_releases_the_branch_and_rates_count_only_successes(tmp_path):
    first, second = "r-refused-writer", "r-next-writer"
    _queue_for_repository(first, tmp_path / "repo")

    def execute(request):
        _write_branch(tmp_path / "repo")
        if request["run_id"] == first:
            raise promotion_requests.CrewError("controlled refusal")
        return {"ok": True, "record": {"commits": []}}

    service = promotion_requests.PromotionService(width=1, execute=execute)
    service.tick()
    assert service.wait_for_in_flight(10) == []
    _queue_for_repository(second, tmp_path / "repo")
    service.tick()
    assert service.wait_for_in_flight(10) == []
    assert promotion_requests.read_promotion(second)["phase"] == "promoted"
    report = promotion_requests.promotion_queue_report()
    assert report["promoted_last_window"] == 1
    assert report["refused_last_window"] == 1
    assert report["successful_promotions_per_hour"] == 1
    assert report["rate_bounded_by"] in report["stage_seconds_last_window"]


def test_release_record_is_written_under_the_branch_lease(tmp_path, monkeypatch):
    from reckon import ledger
    from reckon.crew import promotion, promotion_release, promotion_scope
    from scripts.replay_promotion_drain import git

    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RECKON_RUN_STORE", str(tmp_path / "store.db"))

    repo = tmp_path / "repository"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    path = ledger.run_path("fixture", "run", repo)
    path.parent.mkdir(parents=True)
    path.write_text(ledger.serialize_run({"run_id": "run"}))
    git(repo, "add", str(path.relative_to(repo)))
    git(repo, "commit", "-qm", "Seed release record")
    original = promotion_release._update_run_record

    def observed(*args, **kwargs):
        scope = promotion_scope._BRANCH_WRITES.get()
        assert scope and scope.get("lease"), (
            "release record writes before acquiring the branch lease"
        )
        return original(*args, **kwargs)

    monkeypatch.setattr(promotion_release, "_update_run_record", observed)
    with promotion_scope._branch_write_scope():
        promotion._record_release_on_ledger(
            project="fixture", root=repo, run_id="run",
            release={"worktree_released": True}, checkout=repo,
        )


def test_a_measurement_wait_holds_no_preparation_slot(tmp_path, monkeypatch):
    import subprocess

    from reckon.crew import landing_measure

    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(landing_measure.MEASUREMENT_WIDTH_ENV, "2")
    release, reported = threading.Event(), threading.Event()
    lock = threading.Lock()
    active = maximum = 0
    measuring = {"r-measure-a", "r-measure-b", "r-measure-c"}
    for run_id in [*sorted(measuring), "r-report"]:
        _queue_for_repository(run_id, tmp_path / "repo")

    def run(command, **kwargs):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            assert release.wait(5), "the measurement fixture was not released"
            return subprocess.CompletedProcess(command, 0, "1 passed in 0.01s\n", "")
        finally:
            with lock:
                active -= 1

    def execute(request):
        if request["run_id"] in measuring:
            landing_measure.armed_observations(
                command="python -c pass",
                tree=tmp_path,
                base_tree=tmp_path,
                base_sha="base",
                head_sha="head",
                logs_dir=tmp_path / request["run_id"],
                run=run,
                config={"landing": {}},
            )
        else:
            reported.set()
        return {"ok": True, "record": {"commits": []}}

    service = promotion_requests.PromotionService(width=1, execute=execute)
    service.start_refill()
    try:
        service.tick()
        assert reported.wait(2), "a measurement wait spent a preparation slot"
        with lock:
            assert active == 2
            assert maximum == 2
    finally:
        release.set()
        service.stop_refill()
        service.begin_drain()
        assert service.wait_for_in_flight(10) == []


def test_batch_relay_reads_immutable_objects_and_returns_remote_logs(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys

    from scripts.replay_promotion_drain import _batch_transport, _repository, git

    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-q", "-b", "main")
    git(source, "config", "user.name", "Fixture")
    git(source, "config", "user.email", "fixture@example.invalid")
    (source / "known.txt").write_text("known object\n")
    git(source, "add", "known.txt")
    git(source, "-c", "commit.gpgsign=false", "commit", "-qm", "Seed relay fixture")
    revision = git(source, "rev-parse", "HEAD")
    private = tmp_path / "private"
    binding = {
        "root": str(source),
        "objects": str(source / ".git/objects"),
        "revision": revision,
    }
    _repository(private, binding)
    binary = tmp_path / "scheduler"
    binary.mkdir()
    command = binary / "sbatch"
    command.write_text(
        f"#!{sys.executable}\nimport pathlib,subprocess,sys\n"
        f"private=pathlib.Path({str(private / '.git')!r})\n"
        "hidden=private.with_name('unavailable-git');private.rename(hidden)\n"
        "try:\n"
        " args=sys.argv[1:]\n"
        " cwd=next(a.split('=',1)[1] for a in args if a.startswith('--chdir='))\n"
        " result=subprocess.run(['bash','-c',args[args.index('--wrap')+1]],cwd=cwd)\n"
        "finally: hidden.rename(private)\n"
        "raise SystemExit(result.returncode)\n"
    )
    command.chmod(0o700)
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    environment = os.environ.copy()
    environment["PATH"] = _batch_transport(
        tmp_path,
        {"fixture": private},
        {"projects": {"fixture": binding}},
        tmp_path / "shared",
    )
    log = tmp_path / "local" / "gate.log"
    result = subprocess.run(
        [
            "sbatch",
            f"--chdir={private}",
            f"--output={log}.scheduler",
            "--wrap",
            (
                f"git --git-dir {private / '.git'} show {revision}:known.txt > {log}; "
                f"echo EXIT=0 >> {log}"
            ),
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert log.read_text() == "known object\nEXIT=0\n"
    assert (source / "known.txt").read_text() == "known object\n"
