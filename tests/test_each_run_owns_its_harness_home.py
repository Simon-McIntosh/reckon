"""A run owns the home its harness and its plan writes live in.

The fence seals the operator's dot directories, so a worker whose harness keeps
its state in the operator's home would be unable to write its own transcript,
and a worker whose plan write is serialised through a lock in the operator's
config home would be unable to write any plan at all — even the copy in its own
worktree. Both are proved here through ``launch_plan``: the environment it hands
the harness (``CLAUDE_CONFIG_DIR``, ``CODEX_HOME``) and the grants it composes
into the argv.

Two properties, and the third is what makes the first two mean anything:

* a fenced worker's plan edit lands in its own worktree, and the same call
  without ``checkout_path`` — the route that resolves to the main checkout — is
  refused with the main checkout's plan file byte-identical;
* the operator's codex login is bound read-only into the run's codex home and
  stays readable and unremovable there;
* the declared negative control leaves ``CLAUDE_CONFIG_DIR`` unset, and the
  harness then writes its state under the stand-in ``~/.claude``, where the
  fence refuses it. Without the control the suite would pass against a wiring
  that was never the reason the state write succeeded.

Running this file directly reproduces the red log: its first line is the
declared mutation, verbatim, and what follows is the observed refusal.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from reckon import _backends, _store

NEGATIVE_CONTROL_MUTATION = (
    "leave CLAUDE_CONFIG_DIR unset so the fenced stub writes under the "
    "stand-in ~/.claude"
)

PROJECT = "proj"
SLUG = "plan-a"
AUTHORED = "<p>the authored paragraph</p>"
REPLACEMENT = "<p>the authored paragraph, revised in the worker's worktree</p>"

# The stand-in harness. It stands where the real one would: it writes its own
# state where its own variable points, optionally exercises the codex login
# bound into its home, and optionally drives reckon's MCP server from inside the
# fence. Every step is appended to the record as it happens, so a crash still
# leaves the evidence behind.
_STUB = """import json
import os
import signal
import subprocess
import sys
from pathlib import Path

record = Path(os.environ["STUB_RECORD"])
lines = []


def note(text):
    lines.append(" ".join(str(text).split()))
    record.write_text("\\n".join(lines) + "\\n")


signal.alarm(int(os.environ.get("STUB_ALARM", "240")))

home = os.environ.get("HOME", str(Path.home()))
config_dir = os.environ.get("CLAUDE_CONFIG_DIR") or os.environ.get("CODEX_HOME")
if not config_dir:
    config_dir = str(Path(home) / ".claude")
probe = Path(config_dir) / "session-probe.txt"
try:
    probe.write_text("state")
    note("harness-state-write ok " + str(probe))
except OSError as exc:
    note("harness-state-write refused {} :: {}".format(probe, exc))

bound = os.environ.get("STUB_CODEX_BIND")
if bound:
    target = Path(bound)
    try:
        target.unlink()
        note("codex-login-remove ok " + str(target))
    except OSError as exc:
        note("codex-login-remove refused {} :: {}".format(target, exc))
    try:
        note("codex-login-readable " + repr(target.read_text().strip()))
    except OSError as exc:
        note("codex-login-unreadable {} :: {}".format(target, exc))


def edit(arguments):
    \"\"\"Start reckon's MCP server, call edit_plan once, stop the server.\"\"\"
    server = subprocess.Popen(
        [sys.executable, "-c", "from reckon.mcp import main; main()"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        def exchange(method, params, identifier=None):
            message = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                message["params"] = params
            if identifier is not None:
                message["id"] = identifier
            server.stdin.write(json.dumps(message) + "\\n")
            server.stdin.flush()
            if identifier is None:
                return None
            while True:
                line = server.stdout.readline()
                if not line:
                    return None
                reply = json.loads(line)
                if reply.get("id") == identifier:
                    return reply

        exchange(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "fenced-stub", "version": "0"},
            },
            1,
        )
        exchange("notifications/initialized", None)
        reply = exchange(
            "tools/call", {"name": "edit_plan", "arguments": arguments}, 2
        )
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
    if reply is None:
        return None
    result = reply.get("result") or {}
    text = ""
    for item in result.get("content") or []:
        text += item.get("text") or ""
    try:
        payload = json.loads(text or "{}")
    except ValueError:
        payload = {}
    return {
        "isError": bool(result.get("isError")),
        "rpcError": reply.get("error"),
        "payload": payload,
        "text": text,
    }


if os.environ.get("STUB_MCP"):
    base = {
        "project": os.environ["STUB_PROJECT"],
        "slug": os.environ["STUB_SLUG"],
        "expected_version": 0,
        "mode": "text",
        "old_html": os.environ["STUB_OLD_HTML"],
        "new_html": os.environ["STUB_NEW_HTML"],
    }
    calls = [("no-checkout-path", dict(base))]
    if os.environ.get("STUB_WORKTREE"):
        calls.append(("checkout-path", dict(base, checkout_path=os.environ["STUB_WORKTREE"])))
    for label, arguments in calls:
        outcome = edit(arguments)
        if outcome is None:
            note("edit {} no-response".format(label))
            continue
        payload = outcome["payload"]
        refused = outcome["isError"] or payload.get("ok") is False
        note(
            "edit {} refused={} isError={} path={} detail={}".format(
                label,
                refused,
                outcome["isError"],
                payload.get("path"),
                (outcome["text"] or json.dumps(outcome["rpcError"]))[:300],
            )
        )
"""

requires_bwrap = pytest.mark.skipif(
    shutil.which("bwrap") is None, reason="bubblewrap is not installed"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _plan_html() -> str:
    """A minimal plan in the shape the store's resolver accepts."""
    from reckon._plan_html import write_state

    bare = (
        "<!doctype html>\n"
        '<html lang="en">\n<head>'
        '<meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{SLUG}</title></head>\n"
        f'<body><main class="plan-doc">{AUTHORED}</main></body>\n</html>\n'
    )
    return write_state(
        bare,
        {
            "slug": SLUG,
            "title": SLUG,
            "status": "active",
            "type": "plan",
            "version": 0,
        },
    )


class Fixture:
    """A stand-in home, a main checkout, a worktree and a live run directory.

    The run directory sits under the stand-in config home, exactly where a real
    run's does, so its writability rests on the fence's grant rather than on it
    merely lying outside the protected set.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "home"
        self.config = self.home / ".config" / "reckon"
        self.state = self.config / "state"
        self.run = self.config / "crew" / "runs" / "r1"
        self.locks = self.config / "locks"
        self.checkout = self.home / "Code" / PROJECT
        self.worktree = self.checkout / ".worktrees" / "wt"
        # The lock directory is deliberately absent: the launch path seeds it,
        # so a grant that assumed one would bind a path nothing had created.
        for directory in (self.state, self.run, self.checkout, self.worktree):
            directory.mkdir(parents=True, exist_ok=True)
        # The checkout class is recognised by a ``.git`` entry, not by name.
        (self.checkout / ".git").mkdir(exist_ok=True)

        self.manifest = self.run / "manifest.md"
        self.record = self.run / "stub-record.txt"
        self.prompt = self.run / "prompt.txt"
        self.prompt.write_text("p")
        self.stub = root / "stub.py"
        # The dialect command is executed directly, so the stand-in carries the
        # interpreter that is running this test and the bit that makes it run.
        self.stub.write_text(f"#!{sys.executable}\n{_STUB}")
        self.stub.chmod(0o755)
        self.manifest.write_text("")

        # One stand-in per protected class this measurement rests on.
        self.claude = self.home / ".claude"
        self.claude.mkdir()
        (self.claude / "sentinel").write_text("keep")
        self.codex = self.home / ".codex"
        self.codex.mkdir()
        (self.codex / "auth.json").write_text("operator-login")

        for tree in (self.checkout, self.worktree):
            docs = tree / "docs"
            docs.mkdir(exist_ok=True)
            (docs / f"{SLUG}.html").write_text(_plan_html(), encoding="utf-8")
        self.main_plan = self.checkout / "docs" / f"{SLUG}.html"
        self.worktree_plan = self.worktree / "docs" / f"{SLUG}.html"

        (self.config / "mounts.json").write_text(
            json.dumps({PROJECT: str(self.checkout / "docs")})
        )

    def isolate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Point every environment-resolved path at the stand-in tree.

        The fence resolves the home from its arguments, but the lock directory
        is resolved the way the writer resolves it, so the variable the writer
        reads is isolated here too — an ambient one would send a grant at a real
        path and seed a directory outside this test.
        """
        for name, value in (
            ("HOME", self.home),
            ("RECKON_HOME", self.config),
            ("RECKON_STATE_ROOT", self.state),
            ("RECKON_MOUNTS_PATH", self.config / "mounts.json"),
        ):
            monkeypatch.setenv(name, str(value))

    def environment(self, **extra: str) -> dict[str, str]:
        repo = Path(__file__).resolve().parents[1]
        inherited = os.environ.get("PYTHONPATH", "")
        return {
            **os.environ,
            "HOME": str(self.home),
            "RECKON_HOME": str(self.config),
            "RECKON_STATE_ROOT": str(self.state),
            "RECKON_MOUNTS_PATH": str(self.config / "mounts.json"),
            "PYTHONPATH": os.pathsep.join(
                part for part in (str(repo), inherited) if part
            ),
            "STUB_RECORD": str(self.record),
            **extra,
        }

    def plan(self, dialect: str = "claude", **extra: str) -> _backends.LaunchPlan:
        backend = {"launch": "cli", "command": str(self.stub), "dialect": dialect}
        return _backends.launch_plan(
            backend_name="stub",
            backend=backend,
            prompt="p",
            worktree=str(self.worktree),
            manifest_path=str(self.manifest),
            writable_directories=[str(self.run)],
            fence_home=str(self.home),
            **extra,
        )

    def run_stub(
        self, dialect: str = "claude", drop: tuple[str, ...] = (), **extra: str
    ) -> tuple[list[str], subprocess.CompletedProcess[str]]:
        plan = self.plan(dialect=dialect)
        # The composed launch's own environment is merged at spawn and then the
        # drops are applied, so a drop removes the variable the launch set —
        # which is the mutation, rather than removing a value it re-adds.
        environment = {**self.environment(**extra), **plan.environment}
        for name in drop:
            environment.pop(name, None)
        completed = subprocess.run(
            plan.argv,
            env=environment,
            cwd=plan.cwd,
            input=plan.stdin_text,
            capture_output=True,
            text=True,
            check=False,
        )
        if not self.record.exists():
            raise AssertionError(
                "the stub left no record: exit="
                f"{completed.returncode} stderr={completed.stderr[-2000:]}"
            )
        return self.record.read_text().splitlines(), completed

    def mcp_environment(self) -> dict[str, str]:
        return {
            "STUB_MCP": "1",
            "STUB_RECORD": str(self.record),
            "STUB_PROJECT": PROJECT,
            "STUB_SLUG": SLUG,
            "STUB_OLD_HTML": AUTHORED,
            "STUB_NEW_HTML": REPLACEMENT,
            "STUB_WORKTREE": str(self.worktree),
        }


def _line(lines: list[str], prefix: str) -> str:
    matches = [line for line in lines if line.startswith(prefix)]
    assert matches, f"no record line starting {prefix!r} in {lines}"
    return matches[0]


def _bind_pairs(argv: list[str], flag: str = "--bind") -> list[list[str]]:
    return [argv[i + 1 : i + 3] for i, token in enumerate(argv) if token == flag]


@requires_bwrap
def test_a_run_sets_the_harness_home_inside_its_own_run_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claude-shaped harness reads and writes state under the run."""
    fixture = Fixture(tmp_path)
    fixture.isolate(monkeypatch)
    plan = fixture.plan()

    harness = fixture.run / "harness"
    assert plan.environment["CLAUDE_CONFIG_DIR"] == str(harness)
    assert harness.is_dir()
    # The home is seeded for a live run and the run directory is granted, so
    # the seal on ``~/.config/reckon`` is what the run's grant has to re-open.
    assert plan.argv[:4] == ["bwrap", "--dev-bind", "/", "/"]
    assert fixture.config in _backends.protected_paths(str(fixture.home))
    assert [str(fixture.run), str(fixture.run)] in _bind_pairs(plan.argv)

    lines, completed = fixture.run_stub()
    assert completed.returncode == 0, completed.stderr
    assert str(harness / "session-probe.txt") in _line(lines, "harness-state-write ok")
    assert (harness / "session-probe.txt").is_file()
    # The operator's own home was not written to.
    assert not (fixture.claude / "session-probe.txt").exists()


@requires_bwrap
def test_a_manifest_directory_that_is_not_a_live_run_gets_no_harness_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A preview composes a plan before anything exists, and invents nothing.

    A manifest whose directory does not exist is not a run, so no home is seeded
    and no variable points at one — otherwise composing a plan for display
    would create directories for runs that were never dispatched.
    """
    fixture = Fixture(tmp_path)
    fixture.isolate(monkeypatch)
    plan = _backends.launch_plan(
        backend_name="stub",
        backend={"launch": "cli", "command": str(fixture.stub), "dialect": "claude"},
        prompt="p",
        worktree=str(fixture.worktree),
        manifest_path=str(tmp_path / "never-created" / "manifest.md"),
    )
    assert "CLAUDE_CONFIG_DIR" not in plan.environment
    assert not (tmp_path / "never-created").exists()


@requires_bwrap
def test_the_codex_login_is_bound_read_only_into_the_runs_codex_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator's login is exposed, not copied, and not writable."""
    fixture = Fixture(tmp_path)
    fixture.isolate(monkeypatch)
    plan = fixture.plan(dialect="codex")

    codified = fixture.run / "codex-home"
    assert plan.environment["CODEX_HOME"] == str(codified)
    assert codified.is_dir()
    source = str(fixture.codex / "auth.json")
    destination = str(codified / "auth.json")
    assert [source, destination] in _bind_pairs(plan.argv, "--ro-bind")
    # The bind is composed after the run directory's writable grant, so the file
    # overlay is the last word on that path rather than a grant re-opening it.
    assert plan.argv.index(destination) > plan.argv.index(str(fixture.run))

    lines, completed = fixture.run_stub(dialect="codex", STUB_CODEX_BIND=destination)
    assert completed.returncode == 0, completed.stderr
    assert "codex-login-remove refused" in _line(lines, "codex-login-remove refused")
    assert destination in _line(lines, "codex-login-remove refused")
    assert _line(lines, "codex-login-readable").endswith("'operator-login'")
    assert (codified / "auth.json").is_file()
    assert (fixture.codex / "auth.json").read_text() == "operator-login"


@requires_bwrap
def test_the_grants_include_the_lock_namespace_a_plan_write_serialises_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sealed lock directory refuses every plan write, the worktree included.

    The write lock lives under the reckon config home, which the fence seals, so
    the lock namespace is granted alongside the run's own roots. Its resolution
    is asserted against the writer's own, because a grant computed from a home
    the writer does not use would fence a directory nobody opens.
    """
    fixture = Fixture(tmp_path)
    fixture.isolate(monkeypatch)
    assert _backends.write_lock_directory() == _store._config_home() / "locks"
    assert not fixture.locks.exists()

    plan = fixture.plan()
    assert fixture.locks.is_dir()
    assert [str(fixture.locks), str(fixture.locks)] in _bind_pairs(plan.argv)
    assert fixture.config in _backends.protected_paths(str(fixture.home))

    # Without the variable, the writer falls back to the home under test, so
    # the same resolution lands on the same directory.
    monkeypatch.delenv("RECKON_HOME")
    assert _backends.write_lock_directory(str(fixture.home)) == fixture.locks


@requires_bwrap
def test_a_fenced_worker_edits_its_worktree_and_is_refused_on_the_main_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route that resolves to the main checkout cannot write there.

    A worker that calls reckon's MCP ``edit_plan`` without ``checkout_path``
    writes the main checkout's plan rather than its own copy, which is a write
    that goes live for every session through the editable install. Run inside
    the fence, with the MCP server a child of the fenced process, that write is
    refused and the main checkout's plan file is byte-identical afterwards,
    while the same call naming the worker's worktree lands there.
    """
    fixture = Fixture(tmp_path)
    fixture.isolate(monkeypatch)
    before = _sha256(fixture.main_plan)
    worktree_before = _sha256(fixture.worktree_plan)

    lines, completed = fixture.run_stub(**fixture.mcp_environment())
    assert completed.returncode == 0, completed.stderr

    refused = _line(lines, "edit no-checkout-path refused=True")
    assert "Read-only file system" in refused, refused
    assert _line(lines, "edit checkout-path refused=False")

    assert _sha256(fixture.main_plan) == before
    assert str(fixture.main_plan) not in _line(lines, "edit checkout-path")
    assert _sha256(fixture.worktree_plan) != worktree_before
    assert REPLACEMENT in fixture.worktree_plan.read_text()
    assert REPLACEMENT not in fixture.main_plan.read_text()
    landed = _line(lines, "edit checkout-path refused=False")
    assert str(fixture.worktree_plan) in landed


@requires_bwrap
def test_the_negative_control_leaves_the_harness_state_under_the_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The declared mutation: without the variable the harness writes home.

    If the run's harness home were not the reason the state write succeeds, the
    unset variable would change nothing and this control would fail.
    """
    fixture = Fixture(tmp_path)
    fixture.isolate(monkeypatch)
    lines, completed = fixture.run_stub(
        drop=("CLAUDE_CONFIG_DIR",), **fixture.mcp_environment()
    )
    assert completed.returncode == 0, completed.stderr
    state = _line(lines, "harness-state-write refused")
    assert str(fixture.claude / "session-probe.txt") in state, state
    assert not (fixture.claude / "session-probe.txt").exists()
    assert not (fixture.run / "harness" / "session-probe.txt").exists()
    # The rest of the measure is unmoved by the mutation: the main checkout is
    # still sealed and the worktree still writable.
    assert _line(lines, "edit no-checkout-path refused=True")
    assert _line(lines, "edit checkout-path refused=False")


_ENVIRONMENT_NAMES = (
    "HOME",
    "RECKON_HOME",
    "RECKON_STATE_ROOT",
    "RECKON_MOUNTS_PATH",
)


def _point_at(fixture: Fixture) -> dict[str, str | None]:
    """Set the environment-resolved paths at the stand-in, saving the old ones.

    The red-log entry point has no ``monkeypatch`` to undo this, so it restores
    by hand; the tests pass ``monkeypatch`` and get the same isolation undone
    for them.
    """
    saved = {name: os.environ.get(name) for name in _ENVIRONMENT_NAMES}
    os.environ.update(
        {
            "HOME": str(fixture.home),
            "RECKON_HOME": str(fixture.config),
            "RECKON_STATE_ROOT": str(fixture.state),
            "RECKON_MOUNTS_PATH": str(fixture.config / "mounts.json"),
        }
    )
    return saved


def _unpoint(saved: dict[str, str | None]) -> None:
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def _negative_control_report(root: Path) -> list[str]:
    fixture = Fixture(root)
    saved = _point_at(fixture)
    before = _sha256(fixture.main_plan)
    try:
        lines, completed = fixture.run_stub(
            drop=("CLAUDE_CONFIG_DIR",), **fixture.mcp_environment()
        )
    finally:
        _unpoint(saved)
    return [
        *lines,
        f"stub exit: {completed.returncode}",
        f"main checkout plan unchanged: {_sha256(fixture.main_plan) == before}",
        (
            "stand-in ~/.claude holds only its sentinel: "
            f"{sorted(p.name for p in fixture.claude.iterdir())}"
        ),
    ]


if __name__ == "__main__":  # pragma: no cover - reproduces the red log
    print(NEGATIVE_CONTROL_MUTATION)
    with tempfile.TemporaryDirectory() as directory:
        for line in _negative_control_report(Path(directory)):
            print(line)
    sys.exit(0)
