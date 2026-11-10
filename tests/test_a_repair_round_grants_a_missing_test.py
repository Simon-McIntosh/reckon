"""A repair round grants the test its finding asks for.

A review that finds a change untested blocks the round on a finding whose
``file`` is the production module. Reading only that field granted the module
and nothing else, so the worker could not commit the regression test the
finding demanded: its manifest blocked on a scope extension and the test it
wrote died with the run's scratch. These cases drive the scope grant and the
resume advice directly, over a temporary crew home holding the reviewed run's
pointer, and read back the three facts a worker depends on — the granted paths,
the reason recorded against each, and the scope line the advice hands it.

The grant follows the finding's own text. A finding that says a test is absent
or uncommitted grants the test module it names, or the node-named
``tests/test_<node>.py`` when it names none, and keeps the production file it
cited. A finding about something else grants its file and no test path at all.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from reckon.crew import recovery_repair_dispatch, repair, runs

PROJECT = "sample"
RUN_ID = "r-reviewed"
NODE_ID = "a-reviewed-node"
NODE_TEST = "tests/test_a_reviewed_node.py"
MERGE = "reckon/crew/merge.py"
NAMED_TEST = "tests/test_merge_requires_promotable_run.py"


def _repo(tmp_path: Path) -> Path:
    """A git repository for the claim check to resolve the run's repo against."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    return repo


def _finding(file: str, text: str, *, line: str = "10") -> dict:
    """A stored finding, carrying the id the review record would give it."""
    raw = {"file": file, "line": line, "text": text, "severity": "blocking"}
    return {
        "id": repair.finding_id(raw),
        "file": file,
        "line": line,
        "text": text,
        "severity": "blocking",
    }


def _pointer(repo: Path) -> dict:
    record = {
        "run_id": RUN_ID,
        "project": PROJECT,
        "repo": str(repo),
        "worktree": str(repo),
        "role": "implement",
        "node": {
            "id": NODE_ID,
            "plan": "fixture",
            "section": "s2",
            "write_paths": [],
        },
        "backend": "alpha",
        "launch": "cli",
        "phase": "starting",
        "process_alive": False,
    }
    runs._write_json(runs.pointer_path(RUN_ID), record)
    return record


def _round(
    repo: Path,
    findings: list[dict],
    scope: list[str],
) -> tuple[list[str], list[dict], str]:
    """Grant a round's scope and compose the advice it resumes the run with."""
    _pointer(repo)
    advised, _granted, conflicts = recovery_repair_dispatch._grant_round_scope(
        RUN_ID, scope, findings
    )
    advice = recovery_repair_dispatch._repair_resume_advice(
        {
            "findings": findings,
            "brief": "Repair the blocking findings.",
            "done_when": "the findings' fixes are committed",
            "negative_control": "remove the fix and the test fails",
        },
        advised,
        "round-1",
        conflicts=conflicts,
    )
    pointer = runs.read_pointer(RUN_ID)
    return advised, list(pointer["node"].get("write_grants") or []), advice


def _grant(grants: list[dict], path: str) -> dict | None:
    for grant in grants:
        if grant["path"] == path:
            return grant
    return None


def _scope_line(advice: str) -> str:
    from reckon.crew.recovery_review_dispatch import REPAIR_ADVICE_SCOPE_LINE

    for line in advice.splitlines():
        if line.startswith(REPAIR_ADVICE_SCOPE_LINE):
            return line
    return ""


def test_a_missing_test_finding_grants_the_node_test_and_its_file(
    tmp_path: Path,
) -> None:
    """The cited module and the node-named test both reach the grant and advice."""
    finding = _finding(MERGE, "the new refusal has no committed regression test")
    advised, grants, advice = _round(_repo(tmp_path), [finding], [MERGE])

    assert MERGE in advised
    assert NODE_TEST in advised
    pointer_paths = [grant["path"] for grant in grants]
    assert MERGE in pointer_paths
    assert NODE_TEST in pointer_paths
    assert _grant(grants, NODE_TEST)["reason"] == finding["id"]
    assert _grant(grants, NODE_TEST)["source"] == "repair-round"
    assert NODE_TEST in _scope_line(advice)


def test_the_test_path_reason_is_the_second_finding_that_asked(
    tmp_path: Path,
) -> None:
    """With two blocking findings, the asker's id is recorded, not the first's."""
    first = _finding(MERGE, "the loop is off by one")
    second = _finding(
        "reckon/crew/promotion.py", "the new refusal's test is uncommitted"
    )
    _advised, grants, _advice = _round(
        _repo(tmp_path), [first, second], [MERGE, "reckon/crew/promotion.py"]
    )

    grant = _grant(grants, NODE_TEST)
    assert grant is not None
    assert grant["reason"] == second["id"]
    assert grant["reason"] != first["id"]


def test_a_finding_naming_its_test_grants_that_module_not_the_node_named_one(
    tmp_path: Path,
) -> None:
    """A named home is the ask's answer; the default name is not also granted."""
    finding = _finding(
        MERGE,
        "the missing regression test belongs in " + NAMED_TEST,
    )
    advised, grants, advice = _round(_repo(tmp_path), [finding], [MERGE])

    assert NAMED_TEST in advised
    assert NODE_TEST not in advised
    assert _grant(grants, NAMED_TEST)["reason"] == finding["id"]
    assert NAMED_TEST in _scope_line(advice)


def test_a_finding_about_anything_else_grants_no_test_path(tmp_path: Path) -> None:
    """A blocked module with no test ask grants its file and nothing under tests/."""
    finding = _finding(MERGE, "the refusal message names the wrong check")
    advised, grants, advice = _round(_repo(tmp_path), [finding], [MERGE])

    assert MERGE in advised
    assert [path for path in advised if path.startswith("tests/")] == []
    assert [grant for grant in grants if grant["path"].startswith("tests/")] == []
    assert "tests/" not in _scope_line(advice)


def test_asks_for_a_test_reads_the_three_phrasings_and_nothing_else() -> None:
    """The three named phrasings answer true; a naming or style finding false."""
    for text in (
        "the new refusal has no committed regression test",
        "the regression test is missing",
        "the regression test is uncommitted",
    ):
        assert recovery_repair_dispatch._asks_for_a_test({"text": text}) is True, text

    for text in (
        "the identifier is named in the wrong style",
        "rename the constant to match the module's naming",
    ):
        assert recovery_repair_dispatch._asks_for_a_test({"text": text}) is False, text
