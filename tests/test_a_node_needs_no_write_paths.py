"""A node needs no write paths to be dispatched and queued.

A node's worktree is its scope, so a predicted fence buys nothing at the
queue: a batch entry, a direct dispatch and a plan section that all state no
write paths are dispatched rather than refused, and a node that declares paths
is queued with them exactly as before.

A node that declares no paths has no fence to read its check from, so the check
it writes is the test file its done-when names. It owes the same negative
control a declared node owes for the same done-when.

Every case runs against a temporary repository and crew home.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

import pytest

from reckon.crew import queue_admission, runs
from reckon.crew.node import TaskNode
from tests.test_a_queued_launch_keeps_the_run_record import _lane_document, _launcher
from tests.test_every_path_meets_the_node_contract import (
    CONFIG,
    LANE,
    PROJECT,
    SESSION,
    _entry,
    _pointers,
    _queue,
)
from tests.test_every_path_meets_the_node_contract import (
    crew_home as crew_home,  # noqa: PLC0414 - the shared fixture
)
from tests.test_every_path_meets_the_node_contract import (
    repository as repository,  # noqa: PLC0414 - the shared fixture
)

pytestmark = pytest.mark.timeout(180)

#: The dispatch module is imported through ``importlib`` because this case
#: drives its launch door as one call, as the launch fixtures do.
dispatch_module = importlib.import_module("reckon.crew.dispatch")

#: A measure that names no test file, so what the node writes is read from its
#: write paths rather than from the done-when.
UNTESTED_DONE_WHEN = "run `python -m compileall src` and report 1 module compiled"

#: A measure that names the test the node writes.
TEST_DONE_WHEN = "pytest tests/test_batch_example.py reports 1 passed."


def _write_plan(repository: Path, sections: dict[str, str]) -> None:
    """Replace the fixture plan with the sections a case names."""
    quote = '"'
    body = "".join(
        f"<h2 id={quote}{name}{quote}>§{name[1:]}</h2>{prose}"
        for name, prose in sections.items()
    )
    document = (
        "<!doctype html>\n"
        f"<meta name={quote}docs-project{quote} content={quote}{PROJECT}{quote}>\n"
        f"<meta name={quote}reckon-type{quote} content={quote}plan{quote}>\n"
        f"<meta name={quote}plan-slug{quote} content={quote}plan-a{quote}>\n"
        f"<meta name={quote}plan-status{quote} content={quote}active{quote}>\n"
        f"{body}\n"
    )
    (repository / "docs" / "plans" / "plan-a.html").write_text(
        document, encoding="utf-8"
    )


def _sections_plan(repository: Path) -> None:
    """A plan whose first section states no write-paths sentence.

    The second keeps one, so a case can show both the derived scope and its
    absence in one plan.
    """
    _write_plan(
        repository,
        {
            "s1": "<p>Add the loader.</p>"
            f"<p><strong>Done when</strong> {UNTESTED_DONE_WHEN}.</p>",
            "s2": "<p>Add the parser.</p><p>Write paths: "
            "<code>src/module.py</code>.</p>"
            f"<p><strong>Done when</strong> {TEST_DONE_WHEN}</p>",
        },
    )


def _commit_plan(repository: Path) -> None:
    """A launch reads the plan against its base, so the plan must be committed."""
    subprocess.run(
        ["git", "add", "--", "docs/plans/plan-a.html"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "test: state the sections a case reads"],
        cwd=repository,
        check=True,
        capture_output=True,
    )


def _assert_queued(result: dict, node_id: str) -> dict:
    rows = {row["node"]: row for row in result["results"]}
    assert result["refused"] == 0, result
    assert rows[node_id]["status"] in {"queued", "awaiting-upstream"}, rows[node_id]
    return rows[node_id]


def _config_for_contract():
    def resolve(_pointer):
        return json.loads(json.dumps(CONFIG))

    return resolve


def test_a_batch_entry_with_no_write_paths_is_queued(
    crew_home: Path, repository: Path
) -> None:
    """An entry stating no paths is stored as a node with no write paths."""
    _sections_plan(repository)
    entry = _entry(
        node="no-paths", section="s1", write_paths=[], done_when=UNTESTED_DONE_WHEN
    )

    result = _queue(repository, [entry])

    row = _assert_queued(result, "no-paths")
    assert runs.read_pointer(row["run_id"])["node"]["write_paths"] == []


def test_a_batch_entry_takes_a_section_that_states_no_write_paths(
    crew_home: Path, repository: Path
) -> None:
    """A section with no write-paths sentence is dispatchable from a batch."""
    _sections_plan(repository)
    entry = _entry(node="section-paths", section="s1")
    entry.pop("write_paths")
    entry["done_when"] = UNTESTED_DONE_WHEN

    result = _queue(repository, [entry])

    row = _assert_queued(result, "section-paths")
    assert row["section"] == "s1"
    assert runs.read_pointer(row["run_id"])["node"]["write_paths"] == []


def test_a_queued_node_with_no_write_paths_is_launched(
    crew_home: Path, repository: Path, tmp_path: Path
) -> None:
    _sections_plan(repository)
    _commit_plan(repository)
    entry = _entry(
        node="launch-paths", section="s1", write_paths=[], done_when=UNTESTED_DONE_WHEN
    )
    result = _queue(repository, [entry])
    run_id = _assert_queued(result, "launch-paths")["run_id"]

    CONFIG["backends"][LANE]["lane_document"] = str(
        _lane_document(tmp_path, worker_slots=2.0)
    )
    recorded: list = []

    launched = queue_admission.admit_once(
        config_for=_config_for_contract(), launcher=_launcher(recorded)
    )

    assert launched["launched"] == [run_id], launched
    pointer = runs.read_pointer(run_id)
    assert pointer["phase"] != "queued", pointer
    # Launch grants each node its own landing fragment and names its figure
    # directory; nothing else is added to a node that declared no paths.
    assert all(
        path.startswith(("docs/evidence/fragments/", "docs/figures/"))
        for path in pointer["node"]["write_paths"]
    ), pointer["node"]["write_paths"]
    assert pointer.get("log_path"), pointer
    assert pointer.get("prompt_path"), pointer
    assert "failure" not in pointer, pointer


def test_a_direct_dispatch_with_no_write_paths_is_launched(
    crew_home: Path, repository: Path
) -> None:
    """A direct dispatch hands the admission pass nothing to claim."""
    _sections_plan(repository)
    _commit_plan(repository)
    recorded: list = []

    dispatch_module.dispatch(
        node=TaskNode(
            id="direct-no-paths",
            goal="Add the loader the plan asks for.",
            plan="plan-a",
            section="s1",
            done_when=UNTESTED_DONE_WHEN,
            manifest_path=str(repository / "manifest.md"),
            role="implement",
            spec_level="exact",
            time_budget="20m",
        ),
        project=PROJECT,
        repo=repository,
        config=json.loads(json.dumps(CONFIG)),
        session=SESSION,
        run_id="r-direct-no-paths",
        launcher=_launcher(recorded),
        local=True,
        watch_override=True,
        unreviewed_plan_override=True,
    )

    stored = runs.read_pointer("r-direct-no-paths")
    assert "failure" not in stored, stored
    assert all(
        path.startswith(("docs/evidence/fragments/", "docs/figures/"))
        for path in stored["node"]["write_paths"]
    ), stored["node"]["write_paths"]
    assert stored.get("log_path"), stored
    assert stored.get("prompt_path"), stored


def test_a_no_write_paths_node_with_a_test_done_when_owes_a_negative_control(
    crew_home: Path, repository: Path
) -> None:
    """The done-when names the node's check, so it owes the mutation a declared
    node owes for the same done-when."""
    _sections_plan(repository)
    undeclared = _entry(
        node="no-paths-check-done-when",
        section="s1",
        write_paths=[],
        done_when=TEST_DONE_WHEN,
    )
    declared = _entry(
        node="declared-check-done-when",
        section="s1",
        write_paths=["tests/test_batch_example.py"],
        done_when=TEST_DONE_WHEN,
    )

    result = _queue(repository, [undeclared, declared])

    rows = {row["node"]: row for row in result["results"]}
    assert result["refused"] == 2, result
    for node_id in ("no-paths-check-done-when", "declared-check-done-when"):
        assert rows[node_id]["status"] == "refused", rows[node_id]
        assert "declares no mutation that check must fail against" in str(
            rows[node_id]["reason"]
        ), rows[node_id]
    assert "done-when names a test file" in str(
        rows["no-paths-check-done-when"]["reason"]
    ), rows["no-paths-check-done-when"]
    assert _pointers() == [], "neither refused entry is stored"


def test_a_node_that_declares_write_paths_is_queued_with_them(
    crew_home: Path, repository: Path
) -> None:
    """A declared node is stored with exactly its own fence, as before."""
    entry = _entry(node="declared-paths", write_paths=["src/module.py"])

    result = _queue(repository, [entry])

    row = _assert_queued(result, "declared-paths")
    assert runs.read_pointer(row["run_id"])["node"]["write_paths"] == ["src/module.py"]
