"""A watcher armed from a session carries none of that session's identity.

The producer outlives the session that started it and sweeps every session's
runs, so an identity it inherits is read back as its own. A sweep then composes
a dispatch that resolves its coordinator from the inherited variables and hands
a follower for some other session's run to whichever host the inherited process
names. Measured on 2026-10-18: a watcher armed from a coordinator session drew a
burst of rows dated five days earlier, all belonging to a session idle since
2026-10-07.

Every launch route is covered. The process route is ``_start_watch_producer``,
which hands its launcher a scrubbed environment. The fleet route is the
supervisor script the batch step starts from its own environment. The in-place
reload is ``crew_watch``, which scrubs ``os.environ`` before its first sweep so a
producer armed before this sheds the identity without being restarted.

The negative control passes the inherited environment through, and case (a)
then fails.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from reckon.crew import recovery, runs
from reckon.crew.dispatch import WATCH_ARMING_ENV

# The variables a watcher must never carry. CLAUDE_CODE_MESSAGING_SOCKET and
# CLAUDE_CODE_MESSAGING_TOKEN are reached by the CLAUDE_CODE_ prefix in the
# scrubber rather than named; CODEX_SESSION_ID and CODEX_THREAD_ID cover a
# producer armed from a Codex session.
IDENTITY_VARIABLES = (
    "CLAUDE_PID",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CODEX_SESSION_ID",
    "CODEX_THREAD_ID",
)


CONFIG: dict = {
    "default_backend": "alpha",
    "local_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}, "review": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


def _module(name: str):
    return importlib.import_module(name)


def _arm_session_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set every identity variable a dispatching session would carry."""
    monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "f653840f-identity")
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_SOCKET", "/run/user/0/host.sock")
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_TOKEN", "messaging-token")
    monkeypatch.setenv("CODEX_SESSION_ID", "codex-session-id")
    monkeypatch.setenv("CODEX_THREAD_ID", "codex-thread-id")


def test_start_watch_producer_hands_over_a_scrubbed_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The process route passes its launcher no session identity, keeping PATH
    and RECKON_HOME."""
    dispatch_watch = _module("reckon.crew.dispatch_watch")
    _arm_session_identity(monkeypatch)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(WATCH_ARMING_ENV, "on")

    captured: dict = {}

    class _Supervisor:
        def poll(self):
            return None

    def record_spawn(argv, **kwargs):
        captured["argv"] = [str(item) for item in argv]
        captured["env"] = kwargs.get("env")
        return _Supervisor()

    monkeypatch.setattr(dispatch_watch.subprocess, "Popen", record_spawn)
    monkeypatch.setattr(dispatch_watch, "_watch_executable", lambda: "reckon")
    monkeypatch.setattr(dispatch_watch, "_read_fleet_record", lambda: None)

    dispatch_watch._start_watch_producer("sample")

    assert captured["argv"]
    environment = captured["env"]
    assert environment is not None, "the launcher was handed no environment"
    for name in IDENTITY_VARIABLES:
        assert name not in environment, name
    assert not [key for key in environment if key.startswith("CLAUDE_CODE_")]
    assert environment["PATH"] == "/usr/bin:/bin"
    assert environment["RECKON_HOME"] == str(tmp_path / "home")


def test_each_identity_variable_alone_leaves_the_readers_inert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Set one identity variable at a time, scrub, and the readers name no
    session.

    The scrub is the module's own, so a variable the list fails to cover would
    survive it and the reader would name a session and the check fails.
    """
    dispatch_watch = _module("reckon.crew.dispatch_watch")
    dispatch_accounting = _module("reckon.crew.dispatch_accounting")
    original = dict(os.environ)

    for name in IDENTITY_VARIABLES:
        caller = {
            key: value
            for key, value in original.items()
            if key not in IDENTITY_VARIABLES
        }
        caller[name] = "identity-value"
        scrubbed = dispatch_watch._without_session_identity(caller)
        monkeypatch.setattr(os, "environ", scrubbed)

        harness, runtime_session, _transcript = (
            dispatch_accounting._coordinator_runtime()
        )
        assert harness is None, name
        assert runtime_session is None, name
        assert dispatch_watch._session_host_owner() is None, name

    monkeypatch.setattr(os, "environ", original)


class _StubReloader:
    def __init__(self, *args, **kwargs) -> None:
        pass


class _StubPoll:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


def test_crew_watch_sheds_identity_before_its_first_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The in-place reload route removes them from the live environment, keeping
    the process's pid and seat."""
    follow_module = _module("reckon.crew_follow_commands")
    dispatch_watch = _module("reckon.crew.dispatch_watch")
    dispatch_accounting = _module("reckon.crew.dispatch_accounting")

    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(WATCH_ARMING_ENV, raising=False)
    _arm_session_identity(monkeypatch)

    observed: dict = {}

    def stub_watch_follow(project, **kwargs):
        observed["owner"] = dispatch_watch._session_host_owner()
        observed["runtime"] = dispatch_accounting._coordinator_runtime()
        if False:
            yield {}

    monkeypatch.setattr(recovery, "watch_follow", stub_watch_follow)
    monkeypatch.setattr(runs, "watch_lease_renewed_at", lambda project: None)
    monkeypatch.setattr(follow_module, "_FollowerReloader", _StubReloader)
    monkeypatch.setattr(follow_module, "_StampPoll", _StubPoll)
    monkeypatch.setattr(follow_module, "_ticker_grid", lambda *a, **k: object())
    monkeypatch.setattr(follow_module, "_emit_crew_result", lambda *a, **k: None)

    follow_module.crew_watch.callback(
        project="sample",
        stall_window="1h",
        exit_on_empty=False,
        once=False,
        ensure_service=False,
        follow=True,
        json_output=True,
        pretty=False,
        width=None,
        theme=None,
        no_color=True,
    )

    assert "owner" in observed, "the first sweep did not run"
    assert observed["owner"] is None
    assert observed["runtime"][1] is None
    assert observed["runtime"][0] is None


def test_the_supervisor_sheds_identity_before_it_starts_the_producer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fleet route: the supervisor script, run directly from an environment
    carrying the identity, starts a producer without them."""
    dispatch_watch = _module("reckon.crew.dispatch_watch")
    _arm_session_identity(monkeypatch)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))

    environment_file = tmp_path / "producer-environment.json"
    producer = tmp_path / "producer_writes_environment.py"
    producer.write_text(
        "import json, os, sys\n"
        "with open(sys.argv[1], 'w', encoding='utf-8') as handle:\n"
        "    json.dump(dict(os.environ), handle)\n",
        encoding="utf-8",
    )
    supervisor_argv = [
        sys.executable,
        "-c",
        dispatch_watch._WATCH_PRODUCER_SUPERVISOR,
        "RECKON_WATCH_LOG",
        str(tmp_path / "watch.log"),
        "0.0",
        "0.0",
        "1000.0",
        sys.executable,
        str(producer),
        str(environment_file),
    ]

    completed = subprocess.run(
        supervisor_argv,
        env=dict(os.environ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert environment_file.is_file(), "the stub producer never ran"

    environment = json.loads(environment_file.read_text(encoding="utf-8"))
    for name in IDENTITY_VARIABLES:
        assert name not in environment, name
    assert not [key for key in environment if key.startswith("CLAUDE_CODE_")]
    assert environment["PATH"] == "/usr/bin:/bin"


@pytest.fixture()
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A temporary crew home and a repository that looks like a reckon mount."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repo = tmp_path / "repo"
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "fixture.html").write_text(
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<h2 id="s10">A review dispatched from a windowless producer</h2>',
        encoding="utf-8",
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "docs/plans/fixture.html"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (config_home / "mounts.json").write_text(
        json.dumps({"sample": str(repo / "docs")}), encoding="utf-8"
    )

    dispatch_module = _module("reckon.crew.dispatch")
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    def prepare_worktree(_repo: Path, session: str, node: str, base: str) -> dict:
        path = tmp_path / "worktrees" / f"{session}-{node}"
        path.mkdir(parents=True, exist_ok=True)
        return {"path": str(path), "base": base, "base_sha": base_sha}

    monkeypatch.setattr(dispatch_module, "_create_worktree", prepare_worktree)
    monkeypatch.setenv(WATCH_ARMING_ENV, "off")
    return config_home, repo


def test_a_review_dispatched_inside_a_producer_records_no_session(
    host: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A review dispatched from such a producer records no coordinator runtime
    session and writes no session-host request.

    A host FIFO named for the arming session is held open and live, so a leaked
    identity would write a request the reader would see; the producer's
    environment names no host, so the reader stays empty.
    """
    config_home, repo = host
    dispatch_watch = _module("reckon.crew.dispatch_watch")
    dispatch_module = _module("reckon.crew.dispatch")

    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    _arm_session_identity(monkeypatch)
    # Only the Claude identity resolves a session host; a Codex id beside it
    # makes the harness read as Codex, which has no host FIFO.
    monkeypatch.delenv("CODEX_SESSION_ID", raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)

    # The host the identity would name, and a reader that would see a request.
    owner = dispatch_watch._session_host_owner()
    assert owner is not None, "the identity did not resolve to a session host"
    fifo = dispatch_watch._session_host_fifo_path(owner)
    assert fifo is not None
    fifo.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(fifo)
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)

    producer_environment = dispatch_watch._without_session_identity(os.environ)

    # The watch requirement is answered without arming a producer: the fact
    # under measurement is the session-host request and the recorded
    # coordinator, not admission.
    watch_state = {
        "watcher_live": True,
        "session_attached": False,
        "session_follower_released": False,
        "attach_line": "reckon crew follow --project sample",
        "ensure_line": "reckon crew watch --project sample",
    }
    monkeypatch.setattr(
        dispatch_module, "_ensure_watch_producer", lambda *a, **k: dict(watch_state)
    )
    monkeypatch.setattr(
        dispatch_module, "_watcher_delivery_admission", lambda *a, **k: None
    )

    manifest = config_home / "manifests" / "r-scoring.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("status: complete\ncommits: r-scoring\n", encoding="utf-8")
    scoring = {
        "run_id": "r-scoring",
        "project": "sample",
        "repo": str(repo),
        "node": {"id": "r-scoring", "plan": "fixture", "section": "s10"},
        "backend": "alpha",
        "launch": "cli",
        "argv": ["codex"],
        "phase": "starting",
        "process_alive": False,
        "session": "session-orchestrating",
        "manifest_path": str(manifest),
        "worktree": str(repo),
    }
    runs._write_json(runs.pointer_path("r-scoring"), scoring)

    monkeypatch.setattr(os, "environ", producer_environment)
    assert dispatch_watch._session_host_fifo() is None, (
        "the producer's environment still names a session host"
    )
    with runs.follower_claim("sample", "session-orchestrating", delivery="stream"):
        report = recovery.dispatch_review_for_run(
            scoring, config=CONFIG, launcher=lambda *a, **k: os.getpid()
        )

    assert report["dispatched"] is True, report
    pointer = runs.read_pointer(str(report["review_run_id"]))
    coordinator = pointer["coordinator"]
    assert coordinator["runtime_session_id"] is None, coordinator
    assert coordinator["harness"] is None, coordinator

    written = os.read(reader, 65536)
    os.close(reader)
    assert written == b"", written
