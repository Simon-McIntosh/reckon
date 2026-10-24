"""The PreToolUse guard on a crew worker's recursive walk over the crew home.

Every case runs against a synthetic crew home under the pytest temporary
directory, pointed at by ``RECKON_HOME``, and never reads or writes the real
one. The walk shapes driven here are the ones a worker was measured running: a
``grep`` for a finding id over the crew home, the same search for its own run
id, and three ``find`` walks over the store root with the output paged.

The declared negative control removes the ancestor-of-the-crew-home rule, so a
walk rooted above the crew home is allowed again; the case with
``grep -rn x <crew home's parent>/`` then fails. Running this file with
``WALK_GUARD_MUTATION=1`` reproduces that red log.
"""

from __future__ import annotations

import ast
import io
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from reckon.hooks import install as installer
from reckon.hooks import worker_walk_guard as guard

RUN_ID = "r-20261023T135314004830-a-worker-cannot-walk-the-crew-home"

NEGATIVE_CONTROL_MUTATION = (
    "remove the ancestor-of-the-crew-home rule; the case refusing "
    "grep -rn x <crew home's parent>/ must then fail"
)


@pytest.fixture(autouse=True)
def _apply_declared_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The declared negative control: only the roots named exactly are refused."""
    if not os.environ.get("WALK_GUARD_MUTATION"):
        return
    monkeypatch.setattr(
        guard,
        "_root_is_refused",
        lambda root, home: (
            root == home or any(root == home / store for store in guard.STORE_ROOTS)
        ),
    )


class Fenced:
    """A synthetic crew home, its run directory, and a worker's worktree."""

    def __init__(self, tmp_path: Path) -> None:
        self.config_home = tmp_path / ".config" / "reckon"
        self.crew = self.config_home / "crew"
        self.run_dir = self.crew / "runs" / RUN_ID
        self.run_dir.mkdir(parents=True)
        (self.run_dir / "manifest.md").write_text("status: in-progress\n")
        self.worktree = tmp_path / "worktree"
        self.worktree.mkdir()


@pytest.fixture
def fenced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fenced:
    home = Fenced(tmp_path)
    monkeypatch.setenv("RECKON_HOME", str(home.config_home))
    monkeypatch.setenv("RECKON_RUN_ID", RUN_ID)
    return home


def _bash(command: str, cwd: Path) -> dict:
    return {"tool_name": "Bash", "cwd": str(cwd), "tool_input": {"command": command}}


def _tool(tool_name: str, path: Path, cwd: Path) -> dict:
    return {
        "tool_name": tool_name,
        "cwd": str(cwd),
        "tool_input": {"pattern": "x", "path": str(path)},
    }


# ── The five walks a worker was measured running are refused ────────────────


def test_the_five_measured_walks_are_refused_naming_their_root(fenced: Fenced) -> None:
    """Each walk from the measured window is denied, and names its root."""
    crew = fenced.crew
    cases = [
        (f'grep -rl "fb2ad51a866" {crew}/ | head', crew),
        (f"grep -rl '{RUN_ID}' {crew}/", crew),
        (
            f"find {crew}/runs -name '*.json' -path '*review-records*' | head -5",
            crew / "runs",
        ),
        (
            f"find {crew}/runs -path '*review-records*' -name '*.json' | head -5",
            crew / "runs",
        ),
        (f"find {crew}/runs -name '*.json' | head -5", crew / "runs"),
    ]
    for command, root in cases:
        allowed, message = guard.decide(_bash(command, fenced.worktree))
        assert allowed is False, command
        assert str(root) in message, (command, message)


def test_a_walk_at_an_ancestor_of_the_crew_home_is_refused(fenced: Fenced) -> None:
    """A walk above the crew home reads it too, so it is refused as well."""
    allowed, message = guard.decide(
        _bash(f"grep -rn x {fenced.config_home}/", fenced.worktree)
    )

    assert allowed is False
    assert str(fenced.config_home) in message


def test_a_walk_into_a_store_root_is_refused(fenced: Fenced) -> None:
    """The store roots hold every run's records, not one run's."""
    for store in guard.STORE_ROOTS:
        root = fenced.crew / store
        root.mkdir(exist_ok=True)
        allowed, message = guard.decide(_bash(f"grep -rn x {root}/", fenced.worktree))
        assert allowed is False, store
        assert str(root) in message, store


def test_a_descent_from_the_crew_home_after_a_cd_is_refused(fenced: Fenced) -> None:
    """The segment's own working directory is the root a ``.`` names."""
    allowed, message = guard.decide(
        _bash(f"cd {fenced.crew} && grep -r x .", fenced.worktree)
    )

    assert allowed is False
    assert str(fenced.crew) in message


def test_a_walk_hidden_in_a_nested_script_is_refused(fenced: Fenced) -> None:
    """A ``bash -c`` body and a command substitution are scanned on their own."""
    for command in (
        f"bash -c 'cd {fenced.crew} && grep -r x .'",
        f'echo "$(grep -rl x {fenced.crew})"',
    ):
        allowed, message = guard.decide(_bash(command, fenced.worktree))
        assert allowed is False, command
        assert str(fenced.crew) in message


def test_each_recursive_walk_form_is_refused(fenced: Fenced) -> None:
    """Every program the guard reads as a tree walk is refused at the home."""
    crew = fenced.crew
    commands = [
        f"grep -r x {crew}",
        f"grep -R x {crew}",
        f"grep --recursive x {crew}",
        f"grep -rl x {crew}",
        f"egrep -r x {crew}",
        f"fgrep -r x {crew}",
        f"ugrep -r x {crew}",
        f"ls -R {crew}/runs",
        f"ls -lR {crew}/runs",
        f"rg x {crew}",
        f"fd x {crew}",
        f"tree {crew}/reviews",
        f"du {crew}",
        f"find {crew} -name x",
        f"bfs {crew} -name x",
    ]
    for command in commands:
        allowed, _ = guard.decide(_bash(command, fenced.worktree))
        assert allowed is False, command


# ── The walks the section allows are allowed ───────────────────────────────


def test_the_allowed_walks_and_reads_are_allowed(fenced: Fenced) -> None:
    """One run's own records are readable, and a walk outside the home is free."""
    allowed_commands = [
        f"grep -n x {fenced.run_dir}/manifest.md",
        f"grep -rn x {fenced.run_dir}/",
        f"grep -r x {fenced.run_dir}/",
        f"find {fenced.run_dir} -maxdepth 2",
        f"find {fenced.run_dir} -maxdepth 0 -name '*.md'",
        f"ls {fenced.crew}/runs",
        "grep -rn x src/",
        "find . -maxdepth 1 -name '*.md'",
    ]
    for command in allowed_commands:
        allowed, message = guard.decide(_bash(command, fenced.worktree))
        assert allowed is True, (command, message)
        assert message is None


# ── The Grep and Glob tools are judged on the directory they target ─────────


def test_a_grep_tool_on_the_crew_home_is_refused_and_a_run_directory_allowed(
    fenced: Fenced,
) -> None:
    refused, message = guard.decide(_tool("Grep", fenced.crew, fenced.worktree))
    allowed, note = guard.decide(_tool("Grep", fenced.run_dir, fenced.worktree))

    assert refused is False
    assert str(fenced.crew) in message
    assert allowed is True
    assert note is None


def test_a_glob_tool_on_the_crew_home_is_refused(fenced: Fenced) -> None:
    refused, message = guard.decide(_tool("Glob", fenced.crew, fenced.worktree))

    assert refused is False
    assert str(fenced.crew) in message


# ── Without a run id the guard is not in scope ─────────────────────────────


def test_without_a_run_id_every_case_is_allowed(
    fenced: Fenced, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RECKON_RUN_ID", raising=False)

    for payload in (
        _bash(f"grep -rl x {fenced.crew}/", fenced.worktree),
        _bash(f"cd {fenced.crew} && grep -r x .", fenced.worktree),
        _tool("Grep", fenced.crew, fenced.worktree),
        _tool("Glob", fenced.crew, fenced.worktree),
    ):
        allowed, message = guard.decide(payload)
        assert allowed is True
        assert message is None


# ── An unloadable shell splitter denies rather than admitting ──────────────


def test_an_unloadable_splitter_denies_with_a_reason(
    fenced: Fenced, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(guard, "_splitter", None)
    monkeypatch.setattr(guard, "_splitter_error", "synthetic splitter failure")

    allowed, reason = guard.decide(_bash("grep -rn x src/", fenced.worktree))

    assert allowed is False
    assert "could not load its rules" in reason


# ── The hook entry point denies through a stdout permission decision ───────


def test_main_denies_a_crew_home_walk_with_a_deny_payload(
    fenced: Fenced, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    payload = _bash(f"grep -rl x {fenced.crew}/", fenced.worktree)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    exit_code = guard.main()

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    decision = json.loads(captured.out)["hookSpecificOutput"]
    assert decision["hookEventName"] == "PreToolUse"
    assert decision["permissionDecision"] == "deny"
    assert str(fenced.crew) in decision["permissionDecisionReason"]


# ── The installer composes both matcher groups for the walk guard ──────────


def _pre_tool_use_commands(document: dict, matcher: str) -> list[str]:
    return [
        hook["command"]
        for group in document["hooks"].get("PreToolUse", [])
        if group.get("matcher") == matcher
        for hook in group.get("hooks", [])
    ]


def _bound_command(script: Path) -> str:
    return shlex.join([str(installer.interpreter_path(script)), str(script)])


def test_the_installer_composes_a_bash_and_a_grep_glob_group_for_the_walk_guard() -> (
    None
):
    """The fragment carries both groups, each naming the walk guard script."""
    document = installer.build_hook_snippet(
        include_git_guard=True, include_walk_guard=True
    )
    walk_script = installer.worker_walk_guard_script_path()

    assert _pre_tool_use_commands(document, "Bash") == [
        _bound_command(installer.worker_git_guard_script_path()),
        _bound_command(walk_script),
    ]
    assert _pre_tool_use_commands(document, "Grep|Glob") == [
        _bound_command(walk_script)
    ]
    assert walk_script.is_file()


def test_the_fragment_is_unchanged_when_neither_guard_is_requested() -> None:
    """Asking for neither guard adds no entry to the fragment."""
    default = installer.build_hook_snippet()
    opted_in = installer.build_hook_snippet(
        include_git_guard=True, include_walk_guard=True
    )

    assert "PreToolUse" not in default["hooks"]
    assert [g["matcher"] for g in opted_in["hooks"]["PreToolUse"]] == [
        "Bash",
        "Bash",
        "Grep|Glob",
    ]


def test_the_composed_walk_guard_command_points_into_a_reckon_checkout() -> None:
    """The composed command names a checkout carrying the script and its splitter."""
    commands = _pre_tool_use_commands(
        installer.build_hook_snippet(include_git_guard=True, include_walk_guard=True),
        "Grep|Glob",
    )
    assert len(commands) == 1

    interpreter, script_path = map(Path, shlex.split(commands[0]))
    assert (
        interpreter == Path(__file__).resolve().parents[1] / ".venv" / "bin" / "python"
    )
    script = script_path.resolve()
    assert script.name == "worker_walk_guard.py"
    assert script.is_file()
    # The shell half of the guard loads this module from beside itself.
    assert (script.parent / "worker_git_guard.py").is_file()


def test_the_walk_guard_groups_are_recognised_for_removal() -> None:
    """Sync removes the walk guard by the same recogniser it removes the rest by."""
    groups = [
        group
        for group in installer.build_hook_snippet(
            include_git_guard=True, include_walk_guard=True
        )["hooks"]["PreToolUse"]
        if installer.is_worker_walk_guard_group(group)
    ]

    assert all(len(group["hooks"]) == 1 for group in groups)
    assert [installer._is_crew_guard_group(group) for group in groups] == [True, True]
    assert [group["matcher"] for group in groups] == list(installer.WALK_GUARD_MATCHERS)


# ── The operator's sync path installs the walk guard behind the git guard's flag ─


def _configured_groups(target: Path) -> list[dict]:
    from reckon import cli

    cli._configure_crew_guards(target, remove=False, include_git_guard=True)
    return json.loads(target.read_text())["hooks"]["PreToolUse"]


def test_the_sync_installer_binds_the_walk_guard_behind_the_git_guard_flag(
    tmp_path: Path,
) -> None:
    """The operator's install path is what reaches a worker, so it is driven here.

    ``--include-git-guard`` is the only option the operator passes, and the walk
    guard is installed wherever the git guard is: a run that may not touch a
    peer's checkout is the same run that may not walk the crew home. Both
    matchers are asserted, because the guard reads a Bash command's text and the
    Grep/Glob tools' target, and a wiring that bound only one would leave the
    other reachable.

    The sync path resolves each guard script through the shared resolver that
    finds an executable copy of the installed package. That copy may be a
    checkout other than the one running this test — a worktree sharing the main
    checkout's environment sees both on ``sys.path`` — so the bound command is
    checked against the properties the guard needs: a ``worker_walk_guard.py``
    shipped inside a ``reckon/hooks`` directory beside the git guard's shell
    splitter, launched by that checkout's own interpreter.
    """
    target = tmp_path / "settings.json"
    groups = _configured_groups(target)

    installed = [
        group for group in groups if installer.is_worker_walk_guard_group(group)
    ]
    assert [group["matcher"] for group in installed] == list(
        installer.WALK_GUARD_MATCHERS
    )
    for group in installed:
        interpreter, script_path = map(Path, shlex.split(group["hooks"][0]["command"]))
        script = script_path.resolve()
        assert script.name == "worker_walk_guard.py"
        assert script.parent.name == "hooks"
        assert script.parent.parent.name == "reckon"
        assert script.is_file()
        # The guard loads the git guard's shell splitter from beside itself.
        assert (script.parent / "worker_git_guard.py").is_file()
        # Independent of the resolver's own arithmetic: the command must name a
        # path laid out as a checkout interpreter, ".venv/bin/python".
        assert installer.interpreter_bootstrap.is_checkout_interpreter(str(interpreter))
    assert any(installer.is_worker_git_guard_group(group) for group in groups), (
        "the git guard is installed by the same flag"
    )


def test_the_sync_installer_removes_the_walk_guard_groups_again(
    tmp_path: Path,
) -> None:
    """Removal takes the walk groups out by the same recogniser, leaving none."""
    from reckon import cli

    target = tmp_path / "settings.json"
    _configured_groups(target)

    cli._configure_crew_guards(target, remove=True)

    remaining = json.loads(target.read_text()).get("hooks", {}).get("PreToolUse", [])
    assert not any(installer.is_worker_walk_guard_group(group) for group in remaining)


# ── The guard denies a crew home walk as a real, isolated subprocess ───────


def test_the_walk_guard_denies_a_crew_home_walk_when_run_isolated(
    tmp_path: Path,
) -> None:
    """The composed guard denies as a real subprocess, without the package.

    Launched with ``-I``, so neither ``PYTHONPATH`` nor the user site supplies
    ``reckon``: the guard is stdlib-only and reaches its shell splitter only by
    loading the file beside it, so a refusal here is evidence that load worked.
    """
    home = Fenced(tmp_path)
    payload = _bash(f"grep -rl x {home.crew}/", home.worktree)
    env = {
        **os.environ,
        "RECKON_HOME": str(home.config_home),
        "RECKON_RUN_ID": RUN_ID,
    }
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-I", str(installer.worker_walk_guard_script_path())],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=str(home.worktree),
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    decision = json.loads(result.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert str(home.crew) in decision["permissionDecisionReason"]


# ── The guard is stdlib-only, so the interpreter policy leaves it alone ────


def test_the_walk_guard_imports_no_reckon_module() -> None:
    """The hook imports nothing from the package, so it needs no bootstrap."""
    tree = ast.parse(Path(guard.__file__).read_text())

    imported = {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "reckon" not in imported
    assert guard._splitter is not None


if __name__ == "__main__":  # pragma: no cover - reproduces the red log
    print(NEGATIVE_CONTROL_MUTATION)
