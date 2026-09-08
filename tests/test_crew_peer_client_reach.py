"""The peer-channel client must name the interpreter that composed it.

The worker prompt advertises a peer-channel client to every node, including
the report roles whose delivery directory is not the package source tree.
Composed with a bare interpreter name, the client depends entirely on where
the worker happens to stand: ``python -c`` puts the working directory on the
import path, so the same command that imports from the assigned worktree
fails from a delivery directory with ``ModuleNotFoundError``. The roles that
are told about the channel in the same words are exactly the ones it does not
work for.

The fix is to name the interpreter the composing process runs under. That
interpreter can import the package from any working directory, because it is
the one reckon is installed into; and it is derived at composition time from
the process, never written as a literal host path. This module asserts the
composition and proves the reach by executing the exact advertised command
from both directory kinds and requiring the same outcome in each.
"""

from __future__ import annotations

import inspect
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from reckon.crew.node import TaskNode
from reckon.crew.prompts import compose_prompt

REPO_ROOT = Path(__file__).resolve().parents[1]

# A bare interpreter name is a token a worker's PATH must resolve; any of
# these at the head of the client means the composition is back to relying on
# the working directory. The derived interpreter is an absolute path, which
# this pattern never matches.
BARE_INTERPRETER = re.compile(
    r"^(?:python|python[0-9]+(?:\.[0-9]+)*|pythonw|pypy(?:[0-9]+(?:\.[0-9]+)*)?)(?:\s|$)"
)


def _node() -> TaskNode:
    return TaskNode(
        id="peer-reach-node",
        goal="the advertised peer-channel client runs from a delivery directory",
        plan="plan-a",
        section="s8",
        role="review",
        done_when="the exact advertised client command yields usage from both "
        "a delivery directory and the worktree",
        write_paths=["reckon/crew/prompts.py"],
        time_budget="20m",
    )


def _prompt() -> str:
    # The working directory differs from the worktree, mirroring the read-only
    # roles for whom the delivery directory is not the package source tree.
    return compose_prompt(
        node=_node(),
        project="proj",
        worktree="/repo/worktrees/peer-reach-run",
        working_directory="/delivery/directory",
        manifest_path="/state/runs/peer-reach-run/manifest.md",
        time_budget="20m",
        needs_help_after_failures=2,
    )


def _client_line(prompt: str) -> str:
    for line in prompt.splitlines():
        if line.strip().startswith("Client prefix:"):
            return line.split("Client prefix:", 1)[1].strip()
    raise AssertionError("no Client prefix line in the composed prompt")


def _run(client: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    # Strip PYTHONPATH/HOME from the child so the only thing that can import
    # reckon is the named interpreter's own installation: a reach proven here
    # is proven by the interpreter, not by a leaked environment variable.
    child_env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("PYTHONPATH", "PYTHONHOME")
    }
    return subprocess.run(
        shlex.split(client),
        cwd=cwd,
        env=child_env,
        capture_output=True,
        text=True,
        check=False,
    )


def _assert_usage_outcome(result: subprocess.CompletedProcess[str]) -> None:
    """The client's own usage, not an import error — the same outcome the plan
    requires from both directory kinds."""
    assert result.returncode == 2, result
    assert "usage:" in result.stderr, result
    assert "ModuleNotFoundError" not in result.stderr + result.stdout, result
    assert "No module named 'reckon'" not in result.stderr + result.stdout, result


# ── The composition names the composing interpreter, never a bare name ──────


def test_composed_client_does_not_begin_with_a_bare_interpreter_name():
    client = _client_line(_prompt())

    assert not BARE_INTERPRETER.match(client)


def test_composed_client_names_the_composing_interpreter():
    client = _client_line(_prompt())

    interpreter, _, rest = client.partition(" -c ")
    assert interpreter == sys.executable
    assert rest.startswith("'from reckon.crew.dispatch import _peer_command")


def test_the_interpreter_is_derived_from_the_composing_process_not_a_literal():
    import reckon.crew.prompts as prompts_mod

    client = _client_line(_prompt())
    # Every absolute path in the client is exactly the composing interpreter:
    # no source tree, worktree, delivery directory or manifest path is written
    # into the prompt text, and the derivation goes through the composing
    # process's interpreter rather than spelling one out.
    assert re.findall(r"/[^\s'\"]+", client) == [sys.executable]
    assert "sys.executable" in inspect.getsource(prompts_mod.compose_prompt)


# ── Reach: the exact advertised command runs from both directory kinds ──────


def test_client_reaches_from_a_delivery_directory(tmp_path: Path):
    # tmp_path is not the package source tree and does not contain the
    # package, so any successful import is the named interpreter's doing.
    client = _client_line(_prompt())

    _assert_usage_outcome(_run(client, cwd=tmp_path))


def test_client_still_works_from_the_worktree():
    # The repo root is the package source tree a full-worktree role stands in;
    # the client keeps working unchanged there.
    client = _client_line(_prompt())

    _assert_usage_outcome(_run(client, cwd=REPO_ROOT))


def test_a_different_interpreter_cannot_reach_from_the_delivery_directory(
    tmp_path: Path,
):
    # The negative control that keeps the positive reach honest: the venue's
    # base interpreter (guaranteed distinct from the composing interpreter and
    # without its site-packages) resolves nothing from a non-source directory,
    # which is exactly the failure the bare-name client had. It is derived
    # from the same environment, never a hardcoded host path.
    client_line = f"{sys._base_executable} -c 'from reckon.crew.dispatch import _peer_command; raise SystemExit(_peer_command())'"

    result = _run(client_line, cwd=tmp_path)

    assert result.returncode == 1
    assert "ModuleNotFoundError" in result.stderr
