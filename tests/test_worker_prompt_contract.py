"""The composed manifest block matches the schema the reader enforces.

Three defects were taught by the template that hands the manifest contract out.
The first is this plan's own: the block offered `commits: <sha list>;
changed_paths: <explicit list>` — two keys and two values on one line,
semicolon-separated — which is the exact malformed form the manifest reader
misparses, and three workers dutifully reproduced it; the changed paths parse
to empty and the promotion guard that reads them does not fire. The second is
an external-wait declaration the reader accepts only complete: `status:
waiting` must carry all of wait_condition, wait_probe, wait_terminal and
resume_brief, and the block offered none of them, so a worker that parked on a
submitted job produced a manifest the reader found unreadable. The third is
that the record reaches its judge after its author is gone: the promotion gate
reads the manifest hours after the worker's process has ended, when the only
party who can satisfy a refusal is a coordinator editing a record it did not
author — and a repaired field and a fabricated one are the same bytes. So the
composed prompt must tell the worker to run the write-time check against its
own run before it sets a terminal status, and must name the two fields that
refused four promotions on shape alone while the work was sound: a `status
reader finds unreadable when it leaves the closed vocabulary, and a
`negative_control_log` that is empty or carries anything beyond the bare path.
These tests bind the composed prompt to all three.
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


def _prompt(*, role: str = "implement", run_id: str = "") -> str:
    return compose_prompt(
        node=_node(role=role),
        project="proj",
        worktree="/repo/worktrees/manifest-block-run",
        working_directory="/repo/worktrees/manifest-block-run",
        manifest_path="/state/runs/manifest-block-run/manifest.md",
        time_budget="20m",
        needs_help_after_failures=2,
        run_id=run_id,
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


# ── The worker is told to check its own record while it can still fix it ──

CHECK_HEADER = "CONTRACT — CHECK YOUR OWN RECORD BEFORE YOU CLOSE IT"
CHECK_COMMAND = "reckon crew check-manifest"
RUN_ID = "r-20260922T102837159099-manifest-block-node"


def _check_contract_block(prompt: str) -> str:
    """The write-time check contract, from its header to the manifest block.

    Read out of the composed prompt rather than off the constant, because the
    discipline reaches a worker only if it survives composition into the text
    the worker is handed.
    """
    assert CHECK_HEADER in prompt, (
        "the composed prompt never tells the worker to check its own manifest"
    )
    _, rest = prompt.split(CHECK_HEADER, 1)
    body, _ = rest.split("MANIFEST (write exactly these keys", 1)
    return body


def test_prompt_requires_the_worker_to_check_its_own_manifest():
    prompt = _prompt(run_id=RUN_ID)

    assert CHECK_HEADER in prompt
    assert CHECK_COMMAND in prompt


def test_check_contract_names_the_run_the_worker_must_check():
    block = _check_contract_block(_prompt(run_id=RUN_ID))

    assert f"{CHECK_COMMAND} --run {RUN_ID}" in block, (
        "the check is named without the run it must judge, so a worker has to "
        "translate the instruction before it can execute it"
    )


def test_check_contract_names_both_fields_that_refused_promotions():
    block = _check_contract_block(_prompt(run_id=RUN_ID))

    assert "`status`" in block
    assert "`negative_control_log`" in block


def test_check_contract_states_the_closed_status_vocabulary():
    block = _check_contract_block(_prompt(run_id=RUN_ID))

    assert "one of the recognised values" in block
    assert "unreadable" in block


def test_check_contract_states_the_control_log_must_be_the_bare_path():
    block = _check_contract_block(_prompt(run_id=RUN_ID))

    assert "bare path" in block
    assert "names nothing a reader can open" in block


def test_check_contract_states_a_late_repair_is_indistinguishable_from_a_fabrication():
    block = _check_contract_block(_prompt(run_id=RUN_ID))

    assert "indistinguishable from a" in block
    assert "fabricated" in block


def test_check_contract_precedes_the_keys_it_validates():
    prompt = _prompt(run_id=RUN_ID)

    assert prompt.index(CHECK_HEADER) < prompt.index(
        "MANIFEST (write exactly these keys"
    )


def test_check_contract_survives_a_caller_that_supplies_no_run_id():
    block = _check_contract_block(_prompt())

    assert CHECK_COMMAND in block


def test_check_contract_is_removable_and_scoped(monkeypatch):
    """Masking the constant must delete exactly its block, nothing else.

    Presents that the text is a standalone slot rather than woven into the
    manifest block, so a later edit can withdraw it without disturbing the
    instruction it sits beside.
    """
    from reckon.crew import prompts as prompts_mod

    block = prompts_mod.MANIFEST_CHECK_CONTRACT.format(
        command=f"{CHECK_COMMAND} --run {RUN_ID}"
    )
    before = _prompt(run_id=RUN_ID)
    assert before.count(block) == 1

    monkeypatch.setattr(prompts_mod, "MANIFEST_CHECK_CONTRACT", "")
    after = _prompt(run_id=RUN_ID)

    assert after == before.replace(block, "", 1)
