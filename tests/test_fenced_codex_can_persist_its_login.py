"""A fenced codex run persists a refreshed login through a read-write bind.

codex rewrites ``auth.json`` in place on a token refresh — it opens the same
path with ``O_TRUNC`` and writes the same inode, with no temp file and no
rename (measured against codex-cli 0.155.1; see the write-mode note beside this
node's logs). A read-only bind of the operator's single login therefore lets a
fenced run rotate the token server-side and then drop the refreshed value, so
the operator's next refresh finds a spent, single-use refresh token and the run
dies with a 401.

The login is one file, so the fence binds that one file read-write into the
run's codex home while every other path under the operator's ``~/.codex`` stays
behind the read-only overlay. The properties proved here, through
``launch_plan``'s composed argv and a real fenced run:

* the composed argv carries a ``--bind`` (not ``--ro-bind``) of the operator's
  ``~/.codex/auth.json`` at ``CODEX_HOME/auth.json``, and the operator's
  ``~/.codex`` directory itself keeps its ``--ro-bind`` overlay;
* no copy of the login is written into the run directory — the login is bound,
  not duplicated, so the run cannot shadow it with a stale copy;
* running the composed fence, a worker rewrites ``CODEX_HOME/auth.json`` and
  the write lands in the operator's file, while a write to any other path under
  ``~/.codex`` is refused read-only.

The declared negative control restores the read-only bind of ``auth.json``; the
read-write bind assertion must then fail. That red log's first line is the
mutation string below, verbatim.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from reckon import _backends

NEGATIVE_CONTROL_MUTATION = (
    "restore the read-only bind of auth.json; the read-write bind assertion must fail"
)

requires_bwrap = pytest.mark.skipif(
    shutil.which("bwrap") is None, reason="bubblewrap is not installed"
)

# The stand-in harness. It rewrites the codex login the way the real binary
# does on a refresh — in place, same path — and tries to write a second file
# under the operator's codex home, which the fence must refuse. Each step is
# appended to the record as it happens so a crash still leaves evidence.
_STUB = """import os
from pathlib import Path

record = Path(os.environ["STUB_RECORD"])
lines = []


def note(text):
    lines.append(" ".join(str(text).split()))
    record.write_text("\\n".join(lines) + "\\n")


home = Path(os.environ["HOME"])
codified = Path(os.environ["CODEX_HOME"])
login = codified / "auth.json"

try:
    login.write_text('{"auth_mode": "chatgpt", "tokens": {"refresh_token": "rotated"}}')
    note("codex-login-rewrite ok " + str(login))
except OSError as exc:
    note("codex-login-rewrite refused {} :: {}".format(login, exc))

try:
    (home / ".codex" / "shadow.json").write_text("x")
    note("codex-home-sibling-write ok")
except OSError as exc:
    note("codex-home-sibling-write refused :: {}".format(exc))
"""


class Fixture:
    """A stand-in operator home, a worktree and a live run directory."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "home"
        self.config = self.home / ".config" / "reckon"
        self.state = self.config / "state"
        self.run = self.config / "crew" / "runs" / "r1"
        self.checkout = self.home / "Code" / "proj"
        self.worktree = self.checkout / ".worktrees" / "wt"
        for directory in (self.state, self.run, self.checkout, self.worktree):
            directory.mkdir(parents=True, exist_ok=True)
        (self.checkout / ".git").mkdir(exist_ok=True)

        self.manifest = self.run / "manifest.md"
        self.record = self.run / "stub-record.txt"
        self.manifest.write_text("")

        self.codex = self.home / ".codex"
        self.codex.mkdir()
        self.auth = self.codex / "auth.json"
        self.auth.write_text("operator-login")
        # A second file under the codex home, so the read-only overlay has a
        # path other than auth.json to be shown sealing.
        (self.codex / "config.toml").write_text("model = 'x'\n")

        self.stub = self.root / "stub.py"
        self.stub.write_text(f"#!{sys.executable}\n{_STUB}")
        self.stub.chmod(0o755)

    def isolate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name, value in (
            ("HOME", self.home),
            ("RECKON_HOME", self.config),
            ("RECKON_STATE_ROOT", self.state),
        ):
            monkeypatch.setenv(name, str(value))

    def plan(self, **extra: str) -> _backends.LaunchPlan:
        backend = {"launch": "cli", "command": str(self.stub), "dialect": "codex"}
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

    def run_stub(self) -> tuple[list[str], subprocess.CompletedProcess[str]]:
        plan = self.plan()
        repo = Path(__file__).resolve().parents[1]
        environment = {
            **os.environ,
            **plan.environment,
            "HOME": str(self.home),
            "RECKON_HOME": str(self.config),
            "RECKON_STATE_ROOT": str(self.state),
            "STUB_RECORD": str(self.record),
            "PYTHONPATH": os.pathsep.join(
                part for part in (str(repo), os.environ.get("PYTHONPATH", "")) if part
            ),
        }
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


def _bind_pairs(argv: list[str], flag: str) -> list[list[str]]:
    return [argv[i + 1 : i + 3] for i, token in enumerate(argv) if token == flag]


def _line(lines: list[str], prefix: str) -> str:
    matches = [line for line in lines if line.startswith(prefix)]
    assert matches, f"no record line starting {prefix!r} in {lines}"
    return matches[0]


def test_the_login_is_bound_read_write_at_the_run_codex_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The composed argv binds the one login writable, at CODEX_HOME/auth.json."""
    fixture = Fixture(tmp_path)
    fixture.isolate(monkeypatch)
    plan = fixture.plan()

    codified = fixture.run / "codex-home"
    assert plan.environment["CODEX_HOME"] == str(codified)

    source = str(fixture.auth)
    destination = str(codified / "auth.json")
    assert [source, destination] in _bind_pairs(plan.argv, "--bind")
    assert [source, destination] not in _bind_pairs(plan.argv, "--ro-bind")
    # The writable bind is the last word on that path: it is mounted after the
    # run directory's writable grant, which would otherwise re-open it.
    assert plan.argv.index(destination) > plan.argv.index(str(fixture.run))


def test_the_rest_of_the_codex_home_keeps_its_read_only_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the login is opened up; the operator's ~/.codex stays sealed."""
    fixture = Fixture(tmp_path)
    fixture.isolate(monkeypatch)
    plan = fixture.plan()

    sealed = str(fixture.codex)
    assert [sealed, sealed] in _bind_pairs(plan.argv, "--ro-bind")


def test_no_copy_of_the_login_lands_in_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The login is bound, never copied, so the run cannot shadow it."""
    fixture = Fixture(tmp_path)
    fixture.isolate(monkeypatch)
    fixture.plan()

    assert fixture.auth.read_text() == "operator-login"
    assert list(fixture.run.rglob("auth.json")) == []


@requires_bwrap
def test_a_fenced_run_persists_a_refreshed_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker rewrites the login; the write lands in the operator's file."""
    fixture = Fixture(tmp_path)
    fixture.isolate(monkeypatch)
    lines, completed = fixture.run_stub()

    assert completed.returncode == 0, completed.stderr
    assert "codex-login-rewrite ok" in _line(lines, "codex-login-rewrite ok")
    # The rewrite reached the operator's file through the single-file bind.
    persisted = json.loads(fixture.auth.read_text())
    assert persisted["tokens"]["refresh_token"] == "rotated"  # noqa: S105
    # Every other path under ~/.codex is still refused.
    assert "codex-home-sibling-write refused" in _line(
        lines, "codex-home-sibling-write refused"
    )
    assert not (fixture.codex / "shadow.json").exists()
