"""The composed manifest block matches the schema the reader enforces.

Two defects were taught by the template that hands the manifest contract out.
The first is this plan's own: the block offered `commits: <sha list>;
changed_paths: <explicit list>` — two keys and two values on one line,
semicolon-separated — which is the exact malformed form the manifest reader
misparses, and three workers dutifully reproduced it; the changed paths parse
to empty and the promotion guard that reads them does not fire. The second is
an external-wait declaration the reader accepts only complete: `status:
waiting` must carry all of wait_condition, wait_probe, wait_terminal and
resume_brief, and the block offered none of them, so a worker that parked on a
submitted job produced a manifest the reader found unreadable. These tests bind
the composed prompt to the schema on the only side a worker ever sees: the two
fields sit on separate lines, the status vocabulary offers waiting, and the
four wait fields are present in the block.
"""

from __future__ import annotations

from reckon.crew.node import TaskNode
from reckon.crew.prompts import compose_prompt

REQUIRED_WAIT_FIELDS = ("wait_condition", "wait_probe", "wait_terminal", "resume_brief")
TERMINAL_STATUS_WORDS = ("complete", "blocked", "failed")


def _node(*, role: str = "implement") -> TaskNode:
    return TaskNode(
        id="manifest-block-node",
        goal="the manifest block the prompt hands out matches the schema the reader enforces",
        plan="plan-a",
        section="",
        role=role,
        done_when=(
            "commits and changed_paths sit on separate lines, waiting is "
            "offered, and every required wait field is in the block"
        ),
        write_paths=["reckon/crew/prompts.py"],
        time_budget="20m",
    )


def _prompt(*, role: str = "implement") -> str:
    return compose_prompt(
        node=_node(role=role),
        project="proj",
        worktree="/repo/worktrees/manifest-block-run",
        working_directory="/repo/worktrees/manifest-block-run",
        manifest_path="/state/runs/manifest-block-run/manifest.md",
        time_budget="20m",
        needs_help_after_failures=2,
    )


def _manifest_block(prompt: str) -> list[str]:
    """The key-per-line instruction block, between the MANIFEST header and the
    worktree rules that close it."""
    _, rest = prompt.split("MANIFEST (write exactly these keys", 1)
    body, _ = rest.split("WORKTREE AND PARALLEL-SAFETY RULES", 1)
    return body.splitlines()


def _manifest_line(prompt: str, key: str) -> str:
    """The manifest line that declares the named key."""
    for line in _manifest_block(prompt):
        if line.strip().startswith(key + ":"):
            return line.strip()
    raise AssertionError(f"the manifest block lacks a `{key}:` line")


# ── One key per line; the two fields never share a line ──────────────────


def test_commits_and_changed_paths_each_start_their_own_line():
    prompt = _prompt()

    commits_line = _manifest_line(prompt, "commits")
    paths_line = _manifest_line(prompt, "changed_paths")

    assert commits_line.startswith("commits:")
    assert paths_line.startswith("changed_paths:")
    assert commits_line != paths_line


def test_no_manifest_line_carries_both_keys_on_one_line():
    for line in _manifest_block(_prompt()):
        assert not ("commits:" in line and "changed_paths:" in line), line


# ── The status vocabulary offers waiting ─────────────────────────────────


def test_status_vocabulary_offers_waiting_alongside_the_terminal_states():
    status_line = _manifest_line(_prompt(), "status")
    value = status_line.split(":", 1)[1].strip()
    offered = {word.strip() for word in value.split("|")}

    assert "waiting" in offered
    for state in TERMINAL_STATUS_WORDS:
        assert state in offered, f"status vocabulary omits {state}"


# ── The four fields that make a wait declaration readable are offered ────


def test_each_required_wait_field_appears_in_the_manifest_block():
    prompt = _prompt()

    for field in REQUIRED_WAIT_FIELDS:
        line = _manifest_line(prompt, field)
        assert line.startswith(field + ":"), line
