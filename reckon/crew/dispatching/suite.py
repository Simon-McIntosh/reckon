"""Compose the declared suite and its worker instructions."""

from __future__ import annotations

from ._imports import (
    _SUITE_TEMPLATE_SPELLING,
    Any,
    Mapping,
    Path,
    Sequence,
    TaskNode,
    _backends,
    _dispatch_section_routing,
)

_FACADE_BINDINGS = ()


def _recorded_capability(
    resolution: Any,
    *,
    config: Mapping[str, Any],
    node: TaskNode,
    project: str,
    repo_root: Path,
    authority: Mapping[str, Any],
) -> dict[str, Any] | None:
    """The capability requirement the section routing resolved for this run.

    A route that resolved its lane from the section's own record already
    carries the capability, so it is read back rather than resolved a second
    time. An explicit backend bypasses section routing for the lane while the
    plan's declaration still stands, so the section routing is resolved for the
    record alone there; a declaration that cannot be read leaves the field null
    rather than failing a dispatch that has already chosen its lane.
    """
    evidence = getattr(resolution, "section_routing", None)
    if isinstance(evidence, Mapping) and evidence.get("failure") is None:
        capability = evidence.get("capability")
        if capability:
            return dict(capability)
    resolved = _dispatch_section_routing(
        config, node=node, project=project, repo=repo_root, authority=authority
    )
    if isinstance(resolved, Mapping) and resolved.get("failure") is None:
        capability = resolved.get("capability")
        if capability:
            return dict(capability)
    return None


def _resolved_suite_gate(
    suite_command: str | None,
    *,
    node: TaskNode,
    worktree: str,
    census_tests: Sequence[str] = (),
) -> tuple[str, str]:
    """The armed suite command resolved for this node, and whether it is armed.

    Promotion resolves a scoped declaration against the node's own fenced test
    files, so a worker that is told a different command measures a different
    suite and its pair is refused. The resolution is therefore taken from
    promotion's own substitution rather than reimplemented here, over the same
    facts promotion reads: the declared template, the node's write scope within
    the run's tree, and the census tests the project declares.

    A scope holding no test file resolves to nothing, and the gate is then
    reported ``unarmed`` rather than as an empty command, which reads as a
    command no one could run. A project that declares no command at all
    reports no gate state, because an undeclared project is a different fact
    from a declaration this node's scope empties.
    """
    if not suite_command:
        return "", ""
    from reckon.crew.promotion_records import _scoped_suite_command

    resolved = _scoped_suite_command(
        {
            "suite_command": suite_command,
            "census_tests": list(census_tests),
            "worktree": worktree,
            "node": {"write_paths": list(node.write_paths)},
        }
    )
    return (resolved, "armed") if resolved else ("", "unarmed")


def _suite_gate_prompt_body_for(
    declared: str | None, resolved: str, worktree: Mapping[str, Any]
) -> str:
    """The FENCE — SUITE body for the worktree a dispatch has just cut.

    The base revision is read from the worktree payload the launch created
    rather than passed in by the caller, so the one place that knows which
    revision the tree was cut from is the one place the brief reads it. A
    caller free to supply the revision is free to supply none, and the brief
    then names no revision while still reading as composed — which is the
    failure this indirection makes visible to a test that blanks the payload.
    """
    return _suite_gate_prompt_body(
        declared,
        resolved,
        base_revision=str(worktree.get("base_sha") or worktree.get("base") or ""),
    )


def _suite_gate_prompt_body(
    declared: str | None, resolved: str, *, base_revision: str = ""
) -> str:
    """The FENCE — SUITE body for a run's prompt: template, rule, both revisions.

    A worker told only the resolved file list is told a list its own change
    invalidates: a node that deletes a test leaves the head arm naming a file
    no tree of its own holds, and the arm then collects nothing while reading
    as clean. So the prompt states the template and the per-tree rule beside
    the resolution this node's whole scope produces, and the worker resolves
    the template over each tree it runs against.

    The brief is composed at dispatch, when the worktree's head is the base
    revision, so the resolution above is the baseline arm's command and the
    base revision is the one that arm records. The after arm cannot be resolved
    here — its tree does not exist yet — so the brief names it as the rule that
    the arm is recorded at the run's head, with the command resolved there. A
    worker that reconstructs both resolutions from one statement of the
    template records an arm at a revision promotion never asked for, and the
    landing is refused for it.

    An unscoped declaration has nothing to resolve, so it is stated as the one
    command both arms run.
    """
    from reckon.crew.promotion_records import SCOPED_SUITE_PLACEHOLDER

    template = " ".join(str(declared or "").split())
    revision_line = (
        f"  Base revision: {base_revision}. Record the baseline arm at this\n"
        "  revision, which is the worktree's head as this brief is composed.\n"
        if base_revision
        else ""
    )
    if SCOPED_SUITE_PLACEHOLDER not in template:
        return (
            revision_line + f"  {resolved}\n"
            "  Run exactly this command, unedited, for both arms: its\n"
            "  arguments are declared rather than scoped, so the base and the\n"
            "  head revision resolve the same command.\n"
            "  Record the after arm at the run's head once your work is\n"
            "  complete, and record `revision` on each arm as the revision it\n"
            "  ran at.\n"
        )
    shown = template.replace(SCOPED_SUITE_PLACEHOLDER, _SUITE_TEMPLATE_SPELLING)
    return (
        revision_line + "  Template, as the project arms it, with "
        f"{_SUITE_TEMPLATE_SPELLING} standing for the test files in this node's\n"
        "  write scope that pytest collects (`test_*.py` and `*_test.py`) and\n"
        "  that exist in the tree the command runs against:\n"
        f"    {shown}\n"
        "  Resolve it over the tree you run it against: a write path absent from\n"
        "  that tree is dropped, so a test your change adds is not in the base\n"
        "  arm and one it deletes is not in the head arm. Run the template once\n"
        "  per arm, resolved over that arm's own tree, and record in each arm's\n"
        "  `command` the string you actually ran.\n"
        "  With this node's whole write scope substituted it reads:\n"
        f"    {resolved}\n"
        "  Record the after arm at the run's head once your work is complete,\n"
        "  with the command resolved over that head. A test file your change\n"
        "  adds or deletes makes the two arms' commands differ, so each arm\n"
        "  records the command resolved at its own revision.\n"
        "  Promotion compares each arm's command with this template resolved\n"
        "  over that arm's own tree, and refuses an after arm left naming no\n"
        "  test file, or an arm that exited having collected none.\n"
    )


def _prompt_file_text(plan: _backends.LaunchPlan) -> str:
    """The brief a launched run's ``prompt.txt`` holds, whichever channel carries it.

    A dialect that feeds the prompt on stdin carries it in ``stdin_text``; one
    that puts it on the command line carries it only on the plan. Writing the
    stdin field alone therefore emptied ``prompt.txt`` for every argv-prompt
    run, losing the record of what the worker was told.
    """
    return plan.stdin_text or plan.prompt
