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
import subprocess
import sys
from pathlib import Path
from pwd import getpwuid

import pytest

from reckon.hooks import install as installer
from reckon.hooks import worker_git_guard as guard
from reckon.worker_git_shim import (
    _READ_ACTIONS,
    _READ_ONLY_VERBS,
    _READ_OPTIONS,
    mutating_verb,
)

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


# ── The guard reads every form the shim's classifier reads ──────────────────

# A representative read invocation for each verb the shim's classifier can
# admit, so a case can be built for every verb in the shim's read tables. A verb
# present there but absent here falls back to the bare verb, which the shim
# reads as mutating for a multi-purpose verb; the agreement assertion inside the
# case then fails and names the missing invocation rather than passing silently.
_READ_FORM_INVOCATIONS: dict[str, list[str]] = {
    "branch": ["branch", "--list"],
    "config": ["config", "--get", "user.name"],
    "notes": ["notes", "list"],
    "reflog": ["reflog", "show"],
    "remote": ["remote", "-v"],
    "stash": ["stash", "list"],
    "tag": ["tag", "--list"],
    "worktree": ["worktree", "list", "--porcelain"],
}


@pytest.mark.parametrize("verb", sorted(set(_READ_ONLY_VERBS) | set(_READ_ACTIONS)))
def test_the_guard_allows_every_read_form_the_shim_admits(
    tmp_path: Path, monkeypatch, verb: str
) -> None:
    """Every read form the shim forwards is a read form the guard allows.

    The guard and the shim are two layers over one decision, and this walks the
    shim's own read tables rather than a list kept beside them: for each verb the
    shim can read, the invocation the shim classifies as a read is aimed at
    another checkout with ``RECKON_RUN_ID`` set, and the guard must allow it. A
    read form the shim admits but the guard refuses — the drift this fixes, as
    when the guard kept a read-verb list omitting ``worktree`` — reddens here.
    """
    worktree, other = _fenced(tmp_path, monkeypatch)
    argv = _READ_FORM_INVOCATIONS.get(verb, [verb])

    # The case is only meaningful if the shim really does read this form.
    assert mutating_verb(argv[0], argv[1:]) is None, (
        f"no read invocation is known for {verb!r}; add one to "
        "_READ_FORM_INVOCATIONS so the case exercises a real read form"
    )
    allowed, message = guard.decide(
        _payload(f"git -C {other} {shlex.join(argv)}", worktree)
    )
    assert allowed is True, (verb, message)
    assert message is None


@pytest.mark.parametrize("extra", [[], ["--porcelain"], ["-z"], ["-v"], ["--expired"]])
def test_the_guard_allows_worktree_list_at_another_checkout(
    tmp_path: Path, monkeypatch, extra: list[str]
) -> None:
    """``git worktree list``, whatever it prints with, is a read everywhere.

    The option this fix was raised for is ``--porcelain``; ``-z``, ``-v`` and
    ``--expired`` are the same listing with a different print form, and the shim
    admits them all. Each is aimed at another checkout with a run id set.
    """
    worktree, other = _fenced(tmp_path, monkeypatch)
    argv = ["worktree", "list", *extra]
    assert mutating_verb(argv[0], argv[1:]) is None

    allowed, message = guard.decide(
        _payload(f"git -C {other} {shlex.join(argv)}", worktree)
    )
    assert allowed is True, (extra, message)
    assert message is None


def test_the_shims_read_options_are_the_ones_walked(
    tmp_path: Path, monkeypatch
) -> None:
    """The worktree listing options the test walks are the shim's own.

    A case per option read from the shim's table, so an option added there is
    exercised here without this file listing it again. ``list`` is the action
    rather than an option, so it is excluded from the modifiers walked.
    """
    modifiers = sorted(_READ_OPTIONS["worktree"] - _READ_ACTIONS["worktree"])
    worktree, other = _fenced(tmp_path, monkeypatch)
    for modifier in modifiers:
        allowed, message = guard.decide(
            _payload(f"git -C {other} worktree list {modifier}", worktree)
        )
        assert allowed is True, (modifier, message)


def test_the_mutating_forms_of_a_readable_verb_are_still_denied(
    tmp_path: Path, monkeypatch
) -> None:
    """Admitting a verb's read form must not admit its writing forms.

    The read tables are per-form, not per-verb: ``worktree list`` reads while
    ``worktree add`` writes, and a fix that admitted the verb wholesale would
    open the write. Each of these is denied at another checkout.
    """
    worktree, other = _fenced(tmp_path, monkeypatch)
    for form in (
        "worktree add ../new",
        "worktree remove /tmp/elsewhere",
        "branch newbranch",
        "tag v1",
        "stash",
        "stash pop",
        "config user.name someone",
    ):
        allowed, message = guard.decide(_payload(f"git -C {other} {form}", worktree))
        assert allowed is False, form
        assert str(other) in message


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
    interpreter = Path(__file__).resolve().parents[1] / ".venv" / "bin" / "python"
    script = installer.worker_git_guard_script_path()
    assert commands == [shlex.join([str(interpreter), str(script)])]
    assert shlex.split(commands[0]) == [str(interpreter), str(script)]
    assert script.name == "worker_git_guard.py"
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
    guard_script = cli._worker_git_guard_path()
    interpreter = guard_script.parents[2] / ".venv" / "bin" / "python"
    assert bash_commands == [shlex.join([str(interpreter), str(guard_script)])]
    assert [group for group in opted_groups if group.get("matcher") == "Bash"] == [
        installer.worker_git_guard_group(guard_script)
    ]
    assert interpreter.is_file()
    assert guard_script.name == "worker_git_guard.py"

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


def test_sync_replaces_an_installed_guard_without_duplicating_it(
    tmp_path: Path,
) -> None:
    from reckon import cli

    target = tmp_path / "settings.json"
    composed = installer.build_hook_snippet(include_git_guard=True)["hooks"][
        "PreToolUse"
    ][0]
    assert installer._is_crew_guard_group(composed)
    target.write_text(json.dumps({"hooks": {"PreToolUse": [composed]}}))

    cli._configure_crew_guards(target, remove=False, include_git_guard=True)

    groups = json.loads(target.read_text())["hooks"]["PreToolUse"]
    guards = [group for group in groups if group.get("matcher") == "Bash"]
    assert len(guards) == 1
    assert installer._is_crew_guard_group(guards[0])
    assert len(_pre_tool_use_bash_commands({"hooks": {"PreToolUse": guards}})) == 1


def test_guard_group_recognises_bare_and_composed_commands() -> None:
    script = installer.worker_git_guard_script_path()
    bare = {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": shlex.join([str(script)])}],
    }
    composed = installer.worker_git_guard_group(script)

    for group in (bare, composed):
        assert installer.is_worker_git_guard_group(group)
        assert installer._is_crew_guard_group(group)

    unrelated = {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": "/opt/hooks/other_guard.py"}],
    }
    assert not installer.is_worker_git_guard_group(unrelated)
    assert not installer._is_crew_guard_group(unrelated)


def test_sync_uses_the_installer_guard_group_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reckon import cli

    composed_paths: list[Path] = []
    original_composer = installer.worker_git_guard_group

    def record_composition(script_path: Path) -> dict:
        composed_paths.append(script_path)
        return original_composer(script_path)

    monkeypatch.setattr(installer, "worker_git_guard_group", record_composition)
    target = tmp_path / "settings.json"
    cli._configure_crew_guards(target, remove=False, include_git_guard=True)
    assert composed_paths == [cli._worker_git_guard_path()]

    seen: list[dict] = []
    original_recognizer = installer._is_crew_guard_group

    def record_recognition(group: dict) -> bool:
        seen.append(group)
        return original_recognizer(group)

    monkeypatch.setattr(installer, "_is_crew_guard_group", record_recognition)
    cli._configure_crew_guards(target, remove=False, include_git_guard=True)
    assert any(group.get("matcher") == "Bash" for group in seen)


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


# ── The guard the installer binds runs from a reckon checkout ───────────────


def test_the_composed_guard_command_points_into_a_reckon_checkout() -> None:
    """The guard entry the installer emits resolves inside a reckon checkout.

    The guard imports ``reckon.worker_git_shim`` from the checkout it is
    launched beside, so a command naming a standalone copy would run a script
    whose import fails open. The composed command is asserted to name a path
    whose checkout carries both the guard and the shim module it imports.
    """
    commands = _pre_tool_use_bash_commands(
        installer.build_hook_snippet(include_git_guard=True)
    )
    assert len(commands) == 1

    interpreter, script_path = map(Path, shlex.split(commands[0]))
    assert (
        interpreter == Path(__file__).resolve().parents[1] / ".venv" / "bin" / "python"
    )
    assert interpreter.is_file()
    script = script_path.resolve()
    assert script.name == "worker_git_guard.py"
    assert script.is_file()

    checkout = script.parents[2]
    assert (checkout / "reckon" / "__init__.py").is_file()
    # The very module the guard imports from its checkout is present there.
    assert (checkout / "reckon" / "worker_git_shim.py").is_file()


def test_the_guard_denies_a_cross_checkout_commit_when_run_isolated(
    tmp_path: Path,
) -> None:
    """The composed guard denies a cross-checkout commit as a real subprocess.

    The installer's command is exercised as the harness runs it: the guard
    script launched under the current interpreter with ``-I`` — isolated, so
    neither ``PYTHONPATH`` nor the user site supplies ``reckon`` — against a
    live pointer in a synthetic config home. The guard reaches its read-form
    classifier only by importing ``reckon.worker_git_shim`` from its own
    checkout, so a commit denied here is evidence the import resolved and the
    decision came from the shim's table rather than a fail-open.
    """
    worktree = _repo(tmp_path, "worktree")
    other = _repo(tmp_path, "other-checkout")
    home = tmp_path / "config"
    _write_pointer(home, worktree)

    payload = _payload(f"git -C {other} commit -m wip", worktree)
    env = {**os.environ, "RECKON_HOME": str(home), "RECKON_RUN_ID": RUN_ID}
    env.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, "-I", str(installer.worker_git_guard_script_path())],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=str(worktree),
        env=env,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    decision = json.loads(completed.stdout)["hookSpecificOutput"]
    assert decision["hookEventName"] == "PreToolUse"
    assert decision["permissionDecision"] == "deny"
    assert RUN_ID in decision["permissionDecisionReason"]


# ── The hook entry point refuses through a stdout permission decision ──────


def test_main_denies_a_cross_checkout_command_with_a_deny_payload(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    import sys

    worktree, other = _fenced(tmp_path, monkeypatch)
    payload = _payload(f"git -C {other} reset --hard HEAD~1", worktree)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    exit_code = guard.main()

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    decision = json.loads(captured.out)["hookSpecificOutput"]
    assert decision["hookEventName"] == "PreToolUse"
    assert decision["permissionDecision"] == "deny"
    assert RUN_ID in decision["permissionDecisionReason"]
