"""A fenced claude run carries the operator's subscription login by bind.

A fenced claude worker's ``CLAUDE_CONFIG_DIR`` is the run's own harness home,
seeded with the operator's hooks and instruction files. It previously carried
no login, so the harness started in the run's home and ended about six seconds
later with ``Not logged in``. The login is one file, ``.credentials.json``, so
the fence binds that one file read-write into the run's harness home while
``~/.claude`` itself stays behind the read-only overlay.

Three properties are proved here through ``launch_plan``'s composed argv:

* a fenced claude launch composes a ``--bind`` (not ``--ro-bind``) of the
  operator's ``~/.claude/.credentials.json`` at
  ``<run>/harness/.credentials.json``;
* the login is bound, not copied — no ``.credentials.json`` file is written
  into the run directory;
* the clive lane composes no such bind, because its server holds the account,
  and an absent operator file composes no bind while the fence still builds.

The declared negative control removes the claude branch from
``_harness_credential_binds`` so no subscription credential bind is composed;
the bind assertion must then fail, and that red log's first line is the
mutation string below, verbatim.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import _backends

NEGATIVE_CONTROL_MUTATION = (
    "remove the claude branch from _harness_credential_binds and observe the "
    "new bind assertion fail"
)


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
        self.manifest.write_text("")

        # The operator's claude home holds the single-file login the fence
        # binds. A sibling file stands so the read-only overlay has a path
        # other than the credential to seal.
        self.claude = self.home / ".claude"
        self.claude.mkdir()
        self.credential = self.claude / ".credentials.json"
        self.credential.write_text('{"claudeAiOauth": {"accessToken": "operator"}}')
        (self.claude / "settings.json").write_text("{}\n")

    def isolate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name, value in (
            ("HOME", self.home),
            ("RECKON_HOME", self.config),
            ("RECKON_STATE_ROOT", self.state),
        ):
            monkeypatch.setenv(name, str(value))

    def plan(self, command: str = "claude", **extra: str) -> _backends.LaunchPlan:
        backend = {"launch": "cli", "command": command, "dialect": command}
        return _backends.launch_plan(
            backend_name=command,
            backend=backend,
            prompt="p",
            worktree=str(self.worktree),
            manifest_path=str(self.manifest),
            writable_directories=[str(self.run)],
            fence_home=str(self.home),
            **extra,
        )


def _bind_pairs(argv: list[str], flag: str) -> list[list[str]]:
    return [argv[i + 1 : i + 3] for i, token in enumerate(argv) if token == flag]


def test_the_login_is_bound_read_write_at_the_run_harness_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The composed argv binds the one login writable into the run home."""
    fixture = Fixture(tmp_path)
    fixture.isolate(monkeypatch)
    plan = fixture.plan("claude")

    harness = fixture.run / "harness"
    assert plan.environment["CLAUDE_CONFIG_DIR"] == str(harness)

    source = str(fixture.credential)
    destination = str(harness / ".credentials.json")
    assert [source, destination] in _bind_pairs(plan.argv, "--bind")
    assert [source, destination] not in _bind_pairs(plan.argv, "--ro-bind")
    # The writable bind is the last word on that path: it is mounted after the
    # run directory's writable grant, which would otherwise re-open it.
    assert plan.argv.index(destination) > plan.argv.index(str(fixture.run))


def test_no_copy_of_the_login_lands_in_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The login is bound, never copied, so the run cannot shadow it."""
    fixture = Fixture(tmp_path)
    fixture.isolate(monkeypatch)
    fixture.plan("claude")

    assert fixture.credential.is_file()
    assert list(fixture.run.rglob(".credentials.json")) == []


def test_clive_composes_no_credential_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The clive lane authenticates at its server and binds no login."""
    fixture = Fixture(tmp_path)
    fixture.isolate(monkeypatch)
    plan = fixture.plan("clive")

    harness = fixture.run / "harness"
    destination = str(harness / ".credentials.json")
    assert all(pair[1] != destination for pair in _bind_pairs(plan.argv, "--bind"))
    assert str(fixture.credential) not in plan.argv


def test_an_absent_login_composes_no_bind_and_the_fence_still_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A machine with no login produces a fence short one bind, not a refusal."""
    fixture = Fixture(tmp_path)
    fixture.isolate(monkeypatch)
    fixture.credential.unlink()

    plan = fixture.plan("claude")

    harness = fixture.run / "harness"
    destination = str(harness / ".credentials.json")
    assert plan.argv[0] == _backends.FENCE_BINARY
    assert str(fixture.credential) not in plan.argv
    assert all(pair[1] != destination for pair in _bind_pairs(plan.argv, "--bind"))
