"""A per-node evidence fragment is optional.

A landing leaves its `landing:` line, its run record and its gate log. Either
carrier presents the fragment as optional and names the two cases it is still
written for, a negative result and a mechanism that does not fit a landing
line. Nothing splices a fragment into a plan's landed record: the record is
synthesized once at closure and links the fragment from the node's row without
reproducing its text, while a fragment already on disk keeps rendering through
the served page because composition reads it rather than rewriting it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import evidence
from reckon.crew.node import TaskNode
from reckon.crew.prompts import compose_prompt
from tests.helpers.git import git as _git

PLAN = "demo-plan"
PROJECT = "sample"
NODE = "s1-node"
FRAGMENT = f"docs/evidence/fragments/{PLAN}/{NODE}.html"
FRAGMENT_MARKER = "FRAGMENT-ONLY-MARKER-3c1"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _prompt(*, plan: str = PLAN) -> str:
    return compose_prompt(
        node=TaskNode(
            id=NODE,
            goal="land without a fragment",
            plan=plan,
            section="s1",
            role="implement",
            done_when="the composed prompt presents the fragment as optional",
            write_paths=["reckon/crew/prompts.py", FRAGMENT],
            time_budget="20m",
        ),
        project=PROJECT,
        worktree="/repo/worktrees/node",
        working_directory="/repo/worktrees/node",
        manifest_path="/state/runs/node/manifest.md",
        time_budget="20m",
        needs_help_after_failures=2,
    )


def _manifest(*, base: str) -> str:
    payload: dict[str, object] = {
        "node": NODE,
        "status": "complete",
        "commits": [base],
        "tests": "pytest -q tests/test_example.py -> 0 failed, 1 passed",
        "orientation_base_sha": base,
        "landing": "the fragment is optional and this landing wrote none",
    }
    return json.dumps(payload, indent=2)


def _findings(text: str, *, worktree: Path, repository: Path) -> list[str]:
    from reckon.crew.reports import audit_manifest

    node = TaskNode(
        id=NODE, goal="land", plan=PLAN, role="implement", write_paths=[FRAGMENT]
    )
    return audit_manifest(text, node, worktree=worktree, repository=repository)[
        "findings"
    ]


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "reckon").mkdir(parents=True)
    (root / "reckon" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "reckon/mod.py"),
        ("commit", "-q", "-m", "chore: seed"),
    ):
        _git(root, *arguments)
    return root


def test_the_composed_presents_the_fragment_as_optional() -> None:
    prompt = _flat(_prompt())

    assert "per-node evidence fragment is optional" in prompt
    assert "negative result" in prompt
    assert "mechanism that does not fit a landing line" in prompt


def test_the_brief_carrier_presents_the_fragment_as_optional_too() -> None:
    prompt = _flat(_prompt(plan=""))

    assert "per-node evidence fragment is optional" in prompt
    assert "mechanism that does not fit a landing line" in prompt


def test_a_landing_line_with_no_fragment_passes_the_manifest_check(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")

    assert _findings(_manifest(base=base), worktree=repo, repository=repo) == []


def test_a_landing_line_with_its_own_fragment_also_passes(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    fragment = repo / FRAGMENT
    fragment.parent.mkdir(parents=True)
    fragment.write_text("<html></html>\n", encoding="utf-8")
    _git(repo, "add", FRAGMENT)
    _git(repo, "commit", "-q", "-m", "docs: land a fragment")

    assert _findings(_manifest(base=base), worktree=repo, repository=repo) == []


def test_the_landing_beat_records_a_landing_with_no_fragment(
    checkout: Path,
) -> None:
    from reckon import _store
    from reckon.crew.queue_landing import _landing_beat

    pointer = {
        "node": {"id": NODE, "plan": PLAN, "section": "s1"},
        "project": PROJECT,
        "repo": str(checkout),
    }

    assert _landing_beat(pointer, "s1-attempt-1") == ""

    state, _version = _store.read_plan(PROJECT, PLAN, str(checkout))
    bodies = " ".join(
        comment.get("body", "") for comment in state["comments"].get("s1", [])
    )
    assert "s1-attempt-1" in bodies
    assert not (checkout / "docs" / "evidence" / "fragments" / PLAN).exists()


def test_a_section_ticked_with_a_close_comment_that_links_no_anchor(
    checkout: Path,
) -> None:
    from reckon import _store
    from reckon.mcp_edit_plan import _edit_plan_tool

    _state, version = _store.read_plan(PROJECT, PLAN, str(checkout))
    result = _edit_plan_tool(
        PROJECT,
        PLAN,
        expected_version=version,
        ops=[
            {"op": "set", "path": "section_declarations.s2", "value": "done"},
            {
                "op": "append",
                "target": "comments",
                "section": "s2",
                "item": {
                    "id": "c-close-s2-no-anchor",
                    "who": "reckon-build",
                    "when": "2026-11-16T00:00:00Z",
                    "body": "<p>Landed on the measured suite pair; evidence is the run record.</p>",
                },
            },
        ],
        checkout_path=str(checkout),
    )

    assert result.get("ok"), result
    state, _version = _store.read_plan(PROJECT, PLAN, str(checkout))
    assert state["section_declarations"]["s2"] == "done"
    bodies = " ".join(
        comment.get("body", "") for comment in state["comments"].get("s2", [])
    )
    assert "evidence is the run record" in bodies
    assert "<a " not in bodies


def test_the_overview_links_a_landed_nodes_fragment_and_reads_none_of_it(
    checkout: Path,
) -> None:
    from bs4 import BeautifulSoup

    from tests.test_a_closing_plan_gets_one_sourced_overview import (
        _record_path,
        _synthesize,
    )

    fragment = checkout / FRAGMENT
    fragment.parent.mkdir(parents=True, exist_ok=True)
    fragment.write_text(
        f"<html><body><main><p>{FRAGMENT_MARKER}</p></main></body></html>",
        encoding="utf-8",
    )

    _synthesize(checkout)
    record = _record_path(checkout).read_text(encoding="utf-8")
    soup = BeautifulSoup(record, "html.parser")
    rows = soup.find("section", id="landed-nodes").find("table").find_all("tr")[1:]
    by_node = {row.find("td").get_text(strip=True): row for row in rows}

    link = by_node[NODE].find("a", class_="node-fragment")
    assert link is not None, by_node[NODE]
    assert link["href"] == f"/{PROJECT}/evidence/fragments/{PLAN}/{NODE}.html"
    assert by_node["s2-node"].find("a", class_="node-fragment") is None
    assert FRAGMENT_MARKER not in record


def test_an_existing_fragment_still_renders_through_the_served_page(
    checkout: Path,
) -> None:
    from tests.test_a_closing_plan_gets_one_sourced_overview import _record_path

    _record_path(checkout).parent.mkdir(parents=True, exist_ok=True)
    _record_path(checkout).write_text(
        "<!doctype html><html><body><main><h1>Landed</h1></main></body></html>",
        encoding="utf-8",
    )
    fragment = checkout / FRAGMENT
    fragment.parent.mkdir(parents=True)
    fragment.write_text(
        f"<html><body><main><p>{FRAGMENT_MARKER}</p></main></body></html>",
        encoding="utf-8",
    )

    composed = evidence.compose_landed_record(
        _record_path(checkout), PLAN, project=PROJECT
    )
    text = composed.decode("utf-8") if isinstance(composed, bytes) else composed

    assert FRAGMENT_MARKER in text
