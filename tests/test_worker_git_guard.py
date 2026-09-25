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
