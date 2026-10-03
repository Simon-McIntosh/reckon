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

The writable bind that lets a refresh through is also what would let a run
blank the operator's only login, so the file's guarantees are asserted rather
than assumed: a fenced run cannot remove it at either path (the run home's
mount point refuses the unlink, the operator's directory is read-only) and
cannot replace it, by rename over it or by unlink and recreate. A truncation to
zero bytes is possible through the bind, and is detected after the run by
comparing the size recorded before the launch with the size read back — two
``stat`` calls that never open the credential — and reported on the run's
record and on the run's stderr log, and healed rather than pinned when the
write that follows a refresh's truncate lands.

The declared negative control restores the read-only bind of ``auth.json``; the
read-write bind assertion must then fail, and its red log's first line is the
mutation string below, verbatim. The truncation check's own negative control is
the second mutation string below: with the post-run size check removed, the
zero-byte truncation case finds no report and fails."""

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

TRUNCATION_NEGATIVE_CONTROL_MUTATION = (
    "With the post-run size check removed, the zero-byte truncation case in "
    "tests/test_fenced_codex_can_persist_its_login.py finds no report and fails."
)

requires_bwrap = pytest.mark.skipif(
    shutil.which("bwrap") is None, reason="bubblewrap is not installed"
)

# The stand-in harness. Its default action rewrites the codex login the way the
# real binary does on a refresh — in place, same path — and tries to write a
# second file under the operator's codex home, which the fence must refuse. The
# other actions probe what the writable bind lets a run do to the login it must
# not: remove it at either of its two paths, replace it by rename or by unlink
# and recreate, and truncate it to zero bytes. Each step is appended to the
# record as it happens so a crash still leaves evidence.
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
action = os.environ.get("STUB_ACTION", "refresh")

if action == "refresh":
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
elif action == "truncate":
    try:
        os.truncate(login, 0)
        note("codex-login-truncate ok size={}".format(login.stat().st_size))
    except OSError as exc:
        note("codex-login-truncate refused :: {}".format(exc))
elif action == "remove":
    for label, target in (
        ("run-home", login),
        ("operator-path", home / ".codex" / "auth.json"),
    ):
        try:
            target.unlink()
            note("codex-login-remove-at-{} ok".format(label))
        except OSError as exc:
            note("codex-login-remove-at-{} refused :: {}".format(label, exc))
elif action == "replace":
    try:
        replacement = codified / "replacement.json"
        replacement.write_text("replacement")
        os.replace(replacement, login)
        note("codex-login-rename-over ok")
    except OSError as exc:
        note("codex-login-rename-over refused :: {}".format(exc))
    try:
        login.unlink()
        login.write_text("recreated")
        note("codex-login-unlink-recreate ok")
    except OSError as exc:
        note("codex-login-unlink-recreate refused :: {}".format(exc))
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

    def run_stub(
        self, action: str = "refresh"
    ) -> tuple[list[str], subprocess.CompletedProcess[str]]:
        plan = self.plan()
        repo = Path(__file__).resolve().parents[1]
        environment = {
            **os.environ,
            **plan.environment,
            "HOME": str(self.home),
            "RECKON_HOME": str(self.config),
            "RECKON_STATE_ROOT": str(self.state),
            "STUB_RECORD": str(self.record),
            "STUB_ACTION": action,
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


@requires_bwrap
def test_a_fenced_run_cannot_remove_the_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The login survives an unlink at either of its two paths.

    The run's own codex home holds the bind's mount point, which the kernel
    refuses to unlink, and the operator's ``~/.codex`` is behind the read-only
    overlay. Neither attempt may take the operator's only login.
    """
    fixture = Fixture(tmp_path)
    fixture.isolate(monkeypatch)
    lines, completed = fixture.run_stub("remove")

    assert completed.returncode == 0, completed.stderr
    # Errno 16: the kernel refuses to unlink the bind's mount point. Errno 30:
    # the operator's own directory is behind the read-only overlay.
    assert "Errno 16" in _line(lines, "codex-login-remove-at-run-home refused")
    assert "Errno 30" in _line(lines, "codex-login-remove-at-operator-path refused")
    assert fixture.auth.read_text() == "operator-login"
    assert (fixture.home / ".codex" / "auth.json").read_text() == "operator-login"


@requires_bwrap
def test_a_fenced_run_cannot_replace_the_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The login survives a rename over it and an unlink-and-recreate.

    A rename onto the bind's mount point is refused by the kernel, and the
    unlink that would have to precede a recreate is refused for the same reason
    the removal case is.
    """
    fixture = Fixture(tmp_path)
    fixture.isolate(monkeypatch)
    lines, completed = fixture.run_stub("replace")

    assert completed.returncode == 0, completed.stderr
    # Errno 16 on both: nothing may be renamed onto, or unlinked from, the
    # bind's mount point.
    assert "Errno 16" in _line(lines, "codex-login-rename-over refused")
    assert "Errno 16" in _line(lines, "codex-login-unlink-recreate refused")
    assert fixture.auth.read_text() == "operator-login"


@requires_bwrap
def test_a_truncated_login_is_reported_after_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that blanks the login is reported, not silently persisted.

    The writable bind makes the truncation possible, so the run composes a
    record of the login's size before the fence opens; the reading after the
    run compares it with a second ``stat``, writes the loss onto the run's own
    record and appends it to the run's stderr, and a later observation of the
    run carries it too. The credential is never opened, copied or printed.
    """
    fixture = Fixture(tmp_path)
    fixture.isolate(monkeypatch)
    lines, completed = fixture.run_stub("truncate")

    assert completed.returncode == 0, completed.stderr
    assert "codex-login-truncate ok size=0" in _line(lines, "codex-login-truncate ok")
    # The truncation landed on the operator's only login through the bind.
    assert fixture.auth.stat().st_size == 0

    # The run recorded the size to compare against before its launch.
    record_path = _backends.codex_login_record_path(fixture.run)
    recorded = json.loads(record_path.read_text(encoding="utf-8"))
    assert recorded["size_before"] == len("operator-login")

    report = _backends.observe_codex_login(fixture.run)
    assert report is not None
    assert report["truncated"] is True
    assert report["size_after"] == 0
    detail = _backends.codex_login_truncation_detail(report)

    # Reported on the run's record and on the run's stderr.
    assert json.loads(record_path.read_text(encoding="utf-8"))["truncated"] is True
    assert detail in (fixture.run / "stderr.log").read_text(encoding="utf-8")
    # Two sizes were compared; the credential's contents were not read or
    # printed anywhere the report reaches.
    assert "operator-login" not in json.dumps(report)
    assert "operator-login" not in (fixture.run / "stderr.log").read_text(
        encoding="utf-8"
    )

    # A reader of the run's stream observation is told as well, and one loss is
    # reported once however many times the run is observed.
    observation = _backends.observe_log(
        backend_name="stub",
        backend={"launch": "cli", "command": str(fixture.stub), "dialect": "codex"},
        log_path=str(fixture.run / "stream.jsonl"),
    )
    assert detail in observation.detail
    assert (fixture.run / "stderr.log").read_text(encoding="utf-8").count(detail) == 1


@requires_bwrap
def test_a_login_rewritten_after_a_truncation_clears_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A zero caught mid-refresh is cleared once the rewrite lands.

    A refresh truncates and rewrites the same file, so a reading between the
    two sees zero momentarily. The record heals rather than pinning a loss that
    did not happen, while keeping the moment the zero was seen.
    """
    fixture = Fixture(tmp_path)
    fixture.isolate(monkeypatch)
    fixture.run_stub("truncate")
    assert _backends.observe_codex_login(fixture.run) is not None

    fixture.auth.write_text("operator-login")  # the refresh's write completed
    assert _backends.observe_codex_login(fixture.run) is None

    record_path = _backends.codex_login_record_path(fixture.run)
    recorded = json.loads(record_path.read_text(encoding="utf-8"))
    assert recorded["truncated"] is False
    assert recorded["healed_at"]
    assert recorded["detected_at"]


def test_a_run_without_a_login_record_is_never_reported_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every other lane stays silent: no record, no report."""
    fixture = Fixture(tmp_path)
    fixture.isolate(monkeypatch)

    assert _backends.observe_codex_login(fixture.run) is None
