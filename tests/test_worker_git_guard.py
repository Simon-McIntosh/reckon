"""The PreToolUse Bash guard on mutating git run from inside a crew run.

Every pointer and repository these tests use is synthetic: a live run pointer
is written under a temporary configuration home and the checkouts are created
under the pytest temporary directory. The one real file the installer case
reads is the user-scope harness settings file, and it is touched only to prove
the dry run leaves it byte-identical.
"""

from __future__ import annotations

import io
import json
import os
import shlex
from pathlib import Path
from pwd import getpwuid

from reckon.hooks import install as installer
from reckon.hooks import worker_git_guard as guard

RUN_ID = "r-20260925T210146231713-worker-git-stays-in-its-worktree"


def _repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    (repo / ".git").mkdir(parents=True)
    return repo


def _write_pointer(home: Path, worktree: Path) -> None:
    live_dir = home / "crew" / "live"
    live_dir.mkdir(parents=True, exist_ok=True)
    (live_dir / f"{RUN_ID}.json").write_text(
        json.dumps({"run_id": RUN_ID, "worktree": str(worktree)})
    )


def _payload(command: str, cwd: Path) -> dict:
    return {"tool_name": "Bash", "cwd": str(cwd), "tool_input": {"command": command}}


def _fenced(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """A live run pointer in a temporary home, plus the worktree and another
    checkout beside it. Returns ``(worktree, other)``."""
    home = tmp_path / "config"
    worktree = _repo(tmp_path, "worktree")
    other = _repo(tmp_path, "other-checkout")
    _write_pointer(home, worktree)
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_RUN_ID", RUN_ID)
    return worktree, other


# ── A mutating verb against another checkout is denied ──────────────────────


def test_a_mutating_verb_against_another_checkout_is_denied(
    tmp_path: Path, monkeypatch
) -> None:
    worktree, other = _fenced(tmp_path, monkeypatch)

    by_option = guard.decide(
        _payload(f"git -C {other} checkout -- docs/plans/x.html", worktree)
    )
    by_cd = guard.decide(
        _payload(f"cd {other} && git checkout -- docs/plans/x.html", worktree)
    )

    for allowed, message in (by_option, by_cd):
        assert allowed is False
        # The refusal names the run, the worktree it is fenced to, and the
        # target the command would have touched.
        assert RUN_ID in message
        assert str(worktree) in message
        assert str(other) in message


# ── The same verb inside the run's own worktree is allowed ──────────────────


def test_a_mutating_verb_in_the_run_worktree_is_allowed(
    tmp_path: Path, monkeypatch
) -> None:
    worktree, _ = _fenced(tmp_path, monkeypatch)

    by_option = guard.decide(_payload(f"git -C {worktree} commit -m wip", worktree))
    by_cd_inside = guard.decide(
        _payload(f"cd {worktree} && git add docs/plans/x.html", worktree)
    )
    from_cwd = guard.decide(_payload("git switch -c topic", worktree))

    assert by_option == (True, None)
    assert by_cd_inside == (True, None)
    assert from_cwd == (True, None)


# ── A read-only verb is allowed everywhere ─────────────────────────────────


def test_a_read_only_verb_is_allowed_anywhere(tmp_path: Path, monkeypatch) -> None:
    worktree, other = _fenced(tmp_path, monkeypatch)

    for verb in ("status", "log", "diff", "show", "rev-parse", "grep"):
        allowed, message = guard.decide(
            _payload(f"git -C {other} {verb} -- docs/plans/x.html", worktree)
        )
        assert allowed is True, verb
        assert message is None


# ── With no run id the guard is not in scope ───────────────────────────────


def test_without_a_run_id_everything_is_allowed(tmp_path: Path, monkeypatch) -> None:
    _, other = _fenced(tmp_path, monkeypatch)
    monkeypatch.delenv("RECKON_RUN_ID", raising=False)

    allowed, message = guard.decide(
        _payload(f"git -C {other} checkout -- docs/plans/x.html", tmp_path)
    )

    assert allowed is True
    assert message is None


# ── A verb the command aliases is judged by what it expands to ──────────────


def test_a_verb_aliased_in_the_command_is_resolved(tmp_path: Path, monkeypatch) -> None:
    worktree, other = _fenced(tmp_path, monkeypatch)

    mutating = guard.decide(
        _payload(
            f"git -c alias.co=checkout -C {other} co -- docs/plans/x.html", worktree
        )
    )
    read_only = guard.decide(
        _payload(f"git -c alias.st=status -C {other} st", worktree)
    )
    inside = guard.decide(
        _payload(
            f"git -c alias.co=checkout -C {worktree} co -- docs/plans/x.html", worktree
        )
    )

    assert mutating[0] is False
    assert str(other) in mutating[1]
    assert read_only == (True, None)
    assert inside == (True, None)


def test_a_verb_the_command_does_not_define_is_not_assumed_read_only(
    tmp_path: Path, monkeypatch
) -> None:
    """An alias configured outside the command cannot be expanded here.

    ``co`` may be a mutating alias in the operator's git config. The guard
    cannot read that config, so an unknown verb aimed at another checkout is
    refused rather than assumed harmless.
    """
    worktree, other = _fenced(tmp_path, monkeypatch)

    unknown = guard.decide(
        _payload(f"git -C {other} co -- docs/plans/x.html", worktree)
    )
    read_only = guard.decide(_payload(f"git -C {other} status --porcelain", worktree))

    assert unknown[0] is False
    assert str(other) in unknown[1]

    # A genuinely read-only verb stays allowed everywhere, and an unknown verb
    # inside the run's own worktree is not this guard's business.
    assert read_only == (True, None)
    assert guard.decide(_payload("git frobnicate", worktree)) == (True, None)


# ── A leading environment assignment names the target repository ────────────


def test_a_leading_assignment_names_the_target_repository(
    tmp_path: Path, monkeypatch
) -> None:
    worktree, other = _fenced(tmp_path, monkeypatch)

    by_git_dir = guard.decide(
        _payload(f"GIT_DIR={other} git checkout -- docs/plans/x.html", worktree)
    )
    by_work_tree = guard.decide(
        _payload(f"GIT_WORK_TREE={other} git commit -m wip", worktree)
    )
    inside = guard.decide(_payload(f"GIT_DIR={worktree} git commit -m wip", worktree))

    assert by_git_dir[0] is False
    assert str(other) in by_git_dir[1]
    assert by_work_tree[0] is False
    assert str(other) in by_work_tree[1]
    assert inside == (True, None)


# ── A nested script is scanned, and a cd inside it is tracked ───────────────


def test_a_mutating_verb_inside_a_nested_script_is_denied(
    tmp_path: Path, monkeypatch
) -> None:
    worktree, other = _fenced(tmp_path, monkeypatch)

    substitution = guard.decide(
        _payload(f'echo "$(git -C {other} checkout -- docs/plans/x.html)"', worktree)
    )
    backtick = guard.decide(
        _payload(f"echo `git -C {other} checkout -- docs/plans/x.html`", worktree)
    )
    shell_c = guard.decide(
        _payload(f"bash -c 'cd {other} && git reset --hard HEAD~1'", worktree)
    )
    inside = guard.decide(
        _payload(f"bash -c 'cd {worktree} && git commit -m wip'", worktree)
    )

    for allowed, message in (substitution, backtick, shell_c):
        assert allowed is False
        assert str(other) in message
    assert inside == (True, None)


# ── A command form that cannot be parsed is refused, not allowed ────────────


def test_an_unparsable_command_carrying_a_mutating_verb_is_denied(
    tmp_path: Path, monkeypatch
) -> None:
    worktree, other = _fenced(tmp_path, monkeypatch)

    mutating = guard.decide(
        _payload(f"git -C {other} checkout -- docs/plans/x.html 'unclosed", worktree)
    )
    read_only = guard.decide(_payload("git status --porcelain 'unclosed", worktree))

    assert mutating[0] is False
    assert "parse" in mutating[1].lower()
    assert read_only == (True, None)


# ── The installer entry ─────────────────────────────────────────────────────


def _pre_tool_use_bash_commands(payload: dict) -> list[str]:
    return [
        hook["command"]
        for group in payload["hooks"].get("PreToolUse", [])
        if group.get("matcher") == "Bash"
        for hook in group.get("hooks", [])
    ]


def test_the_installer_emits_the_guard_entry_without_writing_settings() -> None:
    """The fragment can carry the guard entry, and composing it writes nothing.

    The default call is a dry run: it prints the fragment and opens no file. So
    the entry can be emitted while the user-scope settings file — the one the
    lead's explicit approval gates an actual install of — is left untouched.
    """
    real_settings = Path(getpwuid(os.getuid()).pw_dir) / ".claude" / "settings.json"
    before = real_settings.read_bytes() if real_settings.is_file() else None
    buffer = io.StringIO()

    result = installer.install_hook_settings(stream=buffer, include_git_guard=True)

    commands = _pre_tool_use_bash_commands(json.loads(buffer.getvalue()))
    assert commands == _pre_tool_use_bash_commands(result.document)
    assert commands == [str(installer.worker_git_guard_script_path())]
    assert Path(commands[0]).name == "worker_git_guard.py"
    assert installer.worker_git_guard_script_path().is_file()
    after = real_settings.read_bytes() if real_settings.is_file() else None
    assert after == before


def test_the_sync_installer_wires_the_guard_behind_the_same_opt_in(
    tmp_path: Path,
) -> None:
    """The CLI path that installs the sibling guards can bind this one too.

    ``_configure_crew_guards`` composes the native-agent and worker-message
    guards itself, so an entry only ``install.py`` knows about is reachable
    from tests and from nothing an operator runs. The git guard is wired
    through that same path, behind the same opt-in, and the real user-scope
    settings file is fingerprinted to prove nothing was installed by the test.
    """
    from reckon import cli

    real_settings = Path(getpwuid(os.getuid()).pw_dir) / ".claude" / "settings.json"
    before = real_settings.read_bytes() if real_settings.is_file() else None

    target = tmp_path / "settings.json"
    default_changed = cli._configure_crew_guards(target, remove=False)
    default_groups = json.loads(target.read_text())["hooks"]["PreToolUse"]
    default_commands = [
        hook["command"] for group in default_groups for hook in group.get("hooks", [])
    ]

    opted_changed = cli._configure_crew_guards(
        target, remove=False, include_git_guard=True
    )
    opted_groups = json.loads(target.read_text())["hooks"]["PreToolUse"]
    bash_commands = [
        hook["command"]
        for group in opted_groups
        if group.get("matcher") == "Bash"
        for hook in group.get("hooks", [])
    ]

    assert default_changed is True
    assert opted_changed is True
    assert default_commands and not any(
        Path(shlex.split(command)[0]).name == "worker_git_guard.py"
        for command in default_commands
    )
    assert bash_commands == [str(cli._worker_git_guard_path())]
    assert Path(bash_commands[0]).name == "worker_git_guard.py"

    # The guard group is reckon's own, so a later remove takes it back out.
    cli._configure_crew_guards(target, remove=True)
    removed_groups = (
        json.loads(target.read_text()).get("hooks", {}).get("PreToolUse", [])
    )
    assert not any(
        Path(shlex.split(hook["command"])[0]).name == "worker_git_guard.py"
        for group in removed_groups
        for hook in group.get("hooks", [])
    )

    after = real_settings.read_bytes() if real_settings.is_file() else None
    assert after == before


def test_the_fragment_is_unchanged_when_the_guard_is_not_requested() -> None:
    """Opting in is what adds the entry; the default fragment keeps its events."""
    default = installer.build_hook_snippet()
    opted_in = installer.build_hook_snippet(include_git_guard=True)

    assert set(default["hooks"]) == {"UserPromptSubmit", "SessionStart", "Stop"}
    assert "PreToolUse" not in default["hooks"]
    assert set(opted_in["hooks"]) == {
        "UserPromptSubmit",
        "SessionStart",
        "Stop",
        "PreToolUse",
    }


# ── The hook entry point refuses through its exit code ─────────────────────


def test_main_denies_a_cross_checkout_command_with_a_deny_payload(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    import sys

    worktree, other = _fenced(tmp_path, monkeypatch)
    payload = _payload(f"git -C {other} reset --hard HEAD~1", worktree)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    exit_code = guard.main()

    captured = capsys.readouterr()
    assert exit_code == 2
    emitted = json.loads(captured.err)
    assert emitted["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert RUN_ID in emitted["systemMessage"]
