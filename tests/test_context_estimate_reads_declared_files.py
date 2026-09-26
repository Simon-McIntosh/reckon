"""The context estimate charges declared reads, not every path a brief names.

A done-when that merely named the project's run ledger made a one-worker-hour
node estimate 6,150,700 tokens against a 480,000 window and be refused, and the
refusal's own top-level detail was empty while the informative reason sat under
``context``. These pin both repairs: a path merely named is not a read, and a
genuine refusal answers on its top level.
"""

from __future__ import annotations

import math
from pathlib import Path

from reckon import crew
from reckon.crew.dispatch import DispatchPlan
from reckon.crew.routing import (
    _competence_verdict,
    _context_file_inputs,
    _context_fit_verdict,
)

PROJECT = "sample"
PLAN = "context-reads"
PLAN_PATH = f"docs/plans/{PLAN}.html"
LEDGER_PATH = "docs/ledger/crew.json"
LARGE_PATH = "targets/large_module.py"
WINDOW = 1_000
LEDGER_BYTES = 4_000_000
LARGE_BYTES = 2_000_000

# The estimate rule the router documents: ceil(utf8-bytes / 3.5). Expectations
# are computed from the fixture's own byte counts under this rule so they do not
# move with the function under test.
BYTES_PER_TOKEN = 3.5


def _estimated_tokens(byte_count: int) -> int:
    return math.ceil(byte_count / BYTES_PER_TOKEN)


def _node(*, done_when: str, write_paths: list[str]) -> crew.TaskNode:
    return crew.TaskNode(
        id="context-reads",
        goal="measure a declared context input",
        plan=PLAN,
        section="s1",
        role="implement",
        spec_level="exact",
        done_when=done_when,
        write_paths=write_paths,
        time_budget="10m",
        estimated_hours=1.0,
    )


def _authority(repo: Path) -> dict[str, object]:
    return {
        "plan": {
            "project": PROJECT,
            "repository": str(repo),
            "docs": str(repo / "docs"),
        }
    }


def _seed(repo: Path) -> None:
    plan = repo / PLAN_PATH
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{PLAN}">'
        '</head><body><h2 id="s1">Context</h2></body></html>',
        encoding="utf-8",
    )
    ledger = repo / LEDGER_PATH
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_bytes(b"x" * LEDGER_BYTES)
    large = repo / LARGE_PATH
    large.parent.mkdir(parents=True, exist_ok=True)
    large.write_bytes(b"y" * LARGE_BYTES)


def _plan(node: crew.TaskNode, repo: Path, *, window: int = WINDOW) -> DispatchPlan:
    return DispatchPlan(
        run_id="r-context-reads",
        backend="worker",
        launch="cli",
        backend_settings={"usable_input_window": window},
        node=node,
        budget_ceiling="20m",
        validation=None,
        execution_fit=None,
        authority=_authority(repo),
    )


def _file_tokens(repo: Path, node: crew.TaskNode) -> int:
    tokens, _inputs = _context_file_inputs(repo, node, _authority(repo))
    return tokens


def test_a_merely_named_file_is_not_charged_to_context(tmp_path: Path) -> None:
    """A done-when that names a large ledger must not buy its bytes."""
    _seed(tmp_path)
    named = _node(
        done_when=f"the gate is green; the run ledger lives at {LEDGER_PATH}",
        write_paths=[],
    )
    unmentioned = _node(done_when="the gate is green", write_paths=[])

    assert _file_tokens(tmp_path, named) == 0
    assert _file_tokens(tmp_path, unmentioned) == 0
    _tokens, inputs = _context_file_inputs(tmp_path, named, _authority(tmp_path))
    assert inputs["named_files"] == []


def test_a_declared_write_path_is_charged_at_full(tmp_path: Path) -> None:
    """The same file declared as a write path is a load the node pays for."""
    _seed(tmp_path)
    declared = _node(done_when="the module is edited", write_paths=[LARGE_PATH])
    unmentioned = _node(done_when="the module is edited", write_paths=[])
    expected = _estimated_tokens(LARGE_BYTES)

    assert _file_tokens(tmp_path, declared) == expected
    assert _file_tokens(tmp_path, unmentioned) == 0
    _tokens, inputs = _context_file_inputs(tmp_path, declared, _authority(tmp_path))
    records = {str(r["declared"]): r for r in inputs["write_paths"]}
    assert records[LARGE_PATH]["counted"] is True
    assert records[LARGE_PATH]["estimated_tokens"] == expected


def test_an_explicitly_declared_input_is_charged(tmp_path: Path) -> None:
    """A clause declaring the path an input charges it; a bare mention does not."""
    _seed(tmp_path)
    declared = _node(
        done_when=f"the ledger {LEDGER_PATH} is a declared input to this node",
        write_paths=[],
    )
    mentioned = _node(
        done_when=f"the gate is green; the run ledger lives at {LEDGER_PATH}",
        write_paths=[],
    )

    assert _file_tokens(tmp_path, declared) == _estimated_tokens(LEDGER_BYTES)
    assert _file_tokens(tmp_path, mentioned) == 0


def test_a_context_refusal_names_its_figures_on_the_top_level(tmp_path: Path) -> None:
    """The refusal answers where a caller reads, naming estimate, window, files."""
    _seed(tmp_path)
    node = _node(done_when="the module is edited", write_paths=[LARGE_PATH])
    plan = _plan(node, tmp_path, window=WINDOW)

    fit = _context_fit_verdict(resolution=plan, repo=tmp_path)
    assert fit is not None
    assert fit["allowed"] is False
    assert fit["estimated_tokens"] > WINDOW

    verdict = _competence_verdict(resolution=plan, project=PROJECT, repo=tmp_path)

    assert verdict["allowed"] is False
    assert verdict["reason"] == "context-window-exceeded"
    detail = verdict["detail"]
    assert detail
    assert str(verdict["estimated_tokens"]) in detail
    assert str(verdict["window_tokens"]) in detail
    assert LARGE_PATH in detail
