"""The session-host plugin link: its four doctor states and its liveness.

``reckon sync`` links the main checkout's ``plugins/crew-host`` into
``~/.claude/skills/reckon-crew-host`` and ``reckon doctor`` reports the link as
missing, dangling, pointing into a worker worktree, or valid. Every doctor
check runs under a temporary HOME, so no run reads or writes the operator's
real skills directory.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon.cli import (
    CREW_HOST_DANGLING,
    CREW_HOST_MISSING,
    CREW_HOST_VALID,
    CREW_HOST_WORKTREE,
    crew_host_link_state,
    link_crew_host_plugin,
    main,
)

PLUGIN_NAME = "reckon-crew-host"


# ── fixtures ────────────────────────────────────────────────────────────────


def _write_plugin(root: Path) -> Path:
    """Lay out a plugin directory the sync and doctor checks accept as built."""
    plugin = root / "plugins" / "crew-host"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "crew-host", "version": "0.0.1"})
    )
    (plugin / "bin").mkdir()
    (plugin / "bin" / "crew-host").write_text("#!/bin/sh\nexit 0\n")
    return plugin


def _git(cwd: Path, *args: str) -> None:
    env = dict(os.environ)
    env.update(
        GIT_AUTHOR_NAME="test",
        GIT_AUTHOR_EMAIL="test@example.invalid",
        GIT_COMMITTER_NAME="test",
        GIT_COMMITTER_EMAIL="test@example.invalid",
    )
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.fixture
def main_checkout(tmp_path: Path) -> Path:
    """A main git repository holding a built session-host plugin."""
    if shutil.which("git") is None:  # pragma: no cover - environment guard
        pytest.skip("git is required to build a linked worktree")
    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "README").write_text("root\n")
    _git(root, "add", "README")
    _git(root, "commit", "-q", "-m", "root")
    _write_plugin(root)
    return root


@pytest.fixture
def worktree_of(tmp_path: Path, main_checkout: Path) -> Path:
    """A linked worktree of ``main_checkout``, also holding the plugin."""
    worktree = tmp_path / "worker-worktree"
    _git(main_checkout, "worktree", "add", "-q", str(worktree))
    _write_plugin(worktree)
    return worktree


# ── crew_host_link_state: the four states ───────────────────────────────────


def test_state_missing_when_nothing_is_linked(tmp_path: Path, main_checkout: Path):
    skills = tmp_path / "skills"
    skills.mkdir()
    assert crew_host_link_state(skills, main_checkout).state == CREW_HOST_MISSING


def test_state_dangling_when_the_target_is_gone(tmp_path: Path, main_checkout: Path):
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / PLUGIN_NAME).symlink_to(tmp_path / "vanished" / "plugin")
    assert crew_host_link_state(skills, main_checkout).state == CREW_HOST_DANGLING


def test_state_worktree_when_the_link_names_a_worker_worktree(
    tmp_path: Path, main_checkout: Path, worktree_of: Path
):
    skills = tmp_path / "skills"
    skills.mkdir()
    dest = skills / PLUGIN_NAME
    (dest).symlink_to(worktree_of / "plugins" / "crew-host", target_is_directory=True)
    link = crew_host_link_state(skills, main_checkout)
    assert link.state == CREW_HOST_WORKTREE, link.detail


def test_state_valid_when_the_link_names_the_main_checkout(
    tmp_path: Path, main_checkout: Path
):
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / PLUGIN_NAME).symlink_to(
        main_checkout / "plugins" / "crew-host", target_is_directory=True
    )
    link = crew_host_link_state(skills, main_checkout)
    assert link.state == CREW_HOST_VALID, link.detail


# ── link_crew_host_plugin: idempotent ────────────────────────────────────────


def test_link_is_idempotent_and_leaves_one_entry(tmp_path: Path, main_checkout: Path):
    skills = tmp_path / "skills"
    source = main_checkout / "plugins" / "crew-host"
    assert link_crew_host_plugin(source, skills) == "linked"
    assert link_crew_host_plugin(source, skills) == "ok"
    assert [p.name for p in skills.iterdir()] == [PLUGIN_NAME]
    assert (skills / PLUGIN_NAME).resolve() == source.resolve()
    assert crew_host_link_state(skills, main_checkout).state == CREW_HOST_VALID


def test_link_repoints_a_symlink_at_the_wrong_target(
    tmp_path: Path, main_checkout: Path
):
    skills = tmp_path / "skills"
    skills.mkdir()
    dest = skills / PLUGIN_NAME
    dest.symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    assert link_crew_host_plugin(main_checkout / "plugins" / "crew-host", skills) == (
        "repointed"
    )
    assert dest.resolve() == (main_checkout / "plugins" / "crew-host").resolve()


# ── reckon doctor: the four states under a temporary HOME ────────────────────


def _no_validation(dest: Path) -> None:
    """The plugin-validate stub: no Claude CLI is on PATH during the suite."""


def _build_home(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A temporary HOME whose skills, mounts and MCP checks are otherwise green."""
    home = tmp_path / "home"
    skills_dir = home / ".claude" / "skills"
    for skill in sorted(
        path.name
        for path in cli_module._skills_source().iterdir()
        if path.is_dir() and (path / "SKILL.md").is_file()
    ):
        (skills_dir / skill).mkdir(parents=True, exist_ok=True)
        (skills_dir / skill / "SKILL.md").write_text(f"# {skill}\n")

    mounted = tmp_path / "myproject" / "docs"
    mounted.mkdir(parents=True)
    docs_server = home / "docs-server"
    docs_server.mkdir(parents=True)
    (docs_server / "mounts.json").write_text(json.dumps({"myproject": str(mounted)}))
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "claude_desktop_config.json").write_text(
        json.dumps({"mcpServers": {"reckon": {"command": "uv"}}})
    )
    return home, skills_dir, docs_server


def _run_doctor(
    tmp_path: Path,
    main_checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
    link_to: Path | None = None,
    validator=_no_validation,
):
    home, skills_dir, docs_server = _build_home(tmp_path)
    if link_to is not None:
        (skills_dir / PLUGIN_NAME).symlink_to(link_to, target_is_directory=True)

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("RECKON_HOME", str(docs_server))
    # The link check resolves the personal skills directory from the
    # environment variable; point it at this case's temporary tree so the
    # operator's real ~/.claude/skills is neither read nor reported on.
    monkeypatch.setenv(cli_module.CLAUDE_SKILLS_DIR_ENV, str(skills_dir))
    monkeypatch.setattr(cli_module, "_reckon_checkout", lambda: main_checkout)
    monkeypatch.setattr(cli_module, "_project_environment_drift", lambda: (None, []))
    # No real Claude CLI runs during the suite; the plugin-validate leg is
    # exercised through its own tests and through the validator each doctor
    # case injects.
    monkeypatch.setattr(cli_module, "_claude_plugin_validate", validator)
    return CliRunner().invoke(main, ["doctor"]), skills_dir


def test_doctor_reports_a_missing_link_without_failing(
    tmp_path: Path, main_checkout: Path, monkeypatch: pytest.MonkeyPatch
):
    result, _ = _run_doctor(tmp_path, main_checkout, monkeypatch)
    assert f"{PLUGIN_NAME} not linked" in result.output
    assert "run: reckon sync" in result.output
    assert result.exit_code == 0, result.output


def test_doctor_flags_a_dangling_link(
    tmp_path: Path, main_checkout: Path, monkeypatch: pytest.MonkeyPatch
):
    result, _ = _run_doctor(
        tmp_path, main_checkout, monkeypatch, link_to=tmp_path / "gone" / "plugin"
    )
    assert f"{PLUGIN_NAME} dangling" in result.output
    assert result.exit_code != 0


def test_doctor_flags_a_worktree_link(
    tmp_path: Path,
    main_checkout: Path,
    worktree_of: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    result, _ = _run_doctor(
        tmp_path,
        main_checkout,
        monkeypatch,
        link_to=worktree_of / "plugins" / "crew-host",
    )
    assert f"{PLUGIN_NAME} worktree" in result.output
    assert result.exit_code != 0


def test_doctor_accepts_a_valid_link(
    tmp_path: Path, main_checkout: Path, monkeypatch: pytest.MonkeyPatch
):
    result, _ = _run_doctor(
        tmp_path,
        main_checkout,
        monkeypatch,
        link_to=main_checkout / "plugins" / "crew-host",
    )
    assert f"✓  {PLUGIN_NAME}" in result.output
    assert f"✗  {PLUGIN_NAME}" not in result.output


def test_doctor_fails_when_the_plugin_does_not_validate(
    tmp_path: Path, main_checkout: Path, monkeypatch: pytest.MonkeyPatch
):
    result, _ = _run_doctor(
        tmp_path,
        main_checkout,
        monkeypatch,
        link_to=main_checkout / "plugins" / "crew-host",
        validator=lambda dest: "plugin.json: monitors[0] names no command",
    )
    assert f"{PLUGIN_NAME} invalid" in result.output
    assert "monitors[0] names no command" in result.output
    assert result.exit_code != 0


# ── _claude_plugin_validate: the external CLI leg ────────────────────────────


def _fake_claude(tmp_path: Path, body: str) -> Path:
    """Place a fake ``claude`` executable on a temporary bin directory."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "claude"
    exe.write_text(f"#!/bin/sh\n{body}")
    exe.chmod(0o755)
    return bin_dir


def test_claude_plugin_validate_invokes_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    marker = tmp_path / "argv"
    bin_dir = _fake_claude(tmp_path, f'printf "%s" "$*" > "{marker}"\nexit 0\n')
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    assert cli_module._claude_plugin_validate(tmp_path / "plugin") == ""
    assert marker.read_text() == f"plugin validate {tmp_path / 'plugin'}"


def test_claude_plugin_validate_reports_a_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    bin_dir = _fake_claude(tmp_path, "echo 'no plugin.json' >&2\nexit 3\n")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    message = cli_module._claude_plugin_validate(tmp_path / "plugin")
    assert message is not None and "no plugin.json" in message


def test_claude_plugin_validate_is_skipped_without_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert cli_module._claude_plugin_validate(tmp_path / "plugin") is None


# ── sync: links the main checkout, refuses from a worktree ───────────────────


def _run_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    checkout: Path,
    *,
    skills_dir: Path | None = None,
):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    docs = tmp_path / "project" / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("RECKON_HOME", str(home / "docs-server"))
    # The link resolves the personal skills directory from the environment
    # variable. Point it at the caller's directory when given — the autouse
    # fixture's temporary one, or the fake home's default — so no sync run
    # reaches the operator's real ~/.claude/skills.
    monkeypatch.setenv(
        cli_module.CLAUDE_SKILLS_DIR_ENV,
        str(skills_dir if skills_dir is not None else home / ".claude" / "skills"),
    )
    monkeypatch.setattr(cli_module, "_reckon_checkout", lambda: checkout)
    return (
        CliRunner().invoke(
            main,
            [
                "sync",
                str(docs),
                "--project",
                "sample",
                "--mounts",
                str(tmp_path / "mounts.json"),
                "--state-root",
                str(tmp_path / "config-state"),
                "--claude-settings",
                str(tmp_path / "settings.json"),
            ],
        ),
        home,
    )


def test_sync_links_the_plugin_once(tmp_path: Path, main_checkout: Path, monkeypatch):
    result, home = _run_sync(tmp_path, monkeypatch, main_checkout)
    assert result.exit_code == 0, result.output
    dest = home / ".claude" / "skills" / PLUGIN_NAME
    assert dest.is_symlink()
    assert dest.resolve() == (main_checkout / "plugins" / "crew-host").resolve()

    # A second sync leaves exactly the one link.
    result, _ = _run_sync(tmp_path, monkeypatch, main_checkout)
    assert result.exit_code == 0, result.output
    assert [p.name for p in dest.parent.iterdir()] == [PLUGIN_NAME]


def test_sync_refuses_to_link_from_a_worktree(
    tmp_path: Path, main_checkout: Path, worktree_of: Path, monkeypatch
):
    result, home = _run_sync(tmp_path, monkeypatch, worktree_of)
    assert "refused" in result.output
    assert "worker worktree" in result.output
    assert not (home / ".claude" / "skills" / PLUGIN_NAME).exists()


def _entry_signature(link: Path) -> tuple[bool, str | None]:
    """An entry's presence and the target it names, read without following it.

    ``os.readlink`` rather than ``resolve`` so a symlink that dangles still
    reports the target it names; a real file or directory reports presence with
    no target. The pair is compared before and after a run to show the entry was
    not touched, whatever it happened to be.
    """
    if link.is_symlink():
        return True, os.readlink(link)
    return link.exists(), None


def test_sync_leaves_the_real_skills_directory_untouched(
    tmp_path: Path,
    main_checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_claude_skills_dir: Path,
):
    """A sync run links into the test's temporary directory, not the real one.

    The autouse fixture points ``RECKON_CLAUDE_SKILLS_DIR`` at a per-test
    temporary directory, so the link lands there. The operator's own
    ``~/.claude/skills/reckon-crew-host`` entry must be exactly as it was —
    present or absent, and naming the target it named — because a test that
    repointed it would corrupt the user-level link every session loads.
    """
    real = Path.home() / ".claude" / "skills" / PLUGIN_NAME
    before = _entry_signature(real)

    result = _run_sync(
        tmp_path, monkeypatch, main_checkout, skills_dir=isolated_claude_skills_dir
    )[0]
    assert result.exit_code == 0, result.output

    dest = isolated_claude_skills_dir / PLUGIN_NAME
    assert dest.is_symlink()
    assert dest.resolve() == (main_checkout / "plugins" / "crew-host").resolve()
    assert _entry_signature(real) == before
