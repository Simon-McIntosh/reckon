"""A stated reason admits a budget above the resolved fence, and nothing else does.

The fence is a default the flight layer resolves, not a hard stop. Work that
genuinely needs longer — a gate measured at three hours against a fence written
before it was measured — had no way through: the dispatch refused the budget and
the only alternatives were splitting work that cannot be split or writing a
figure that lies. ``--over-fence REASON`` is that way through, and the reason is
what distinguishes it from the fence simply being wrong.

The arms are the whole contract: an over-fence budget with a recorded reason
passes and carries that reason onto the node; the same budget with no reason is
refused by name, so a caller can act on the refusal; and a budget inside the
fence is untouched by any of it.

Negative control: ignoring the recorded reason (dropping the reason test from
the bounded check) turns the first arm red with the refusal it forbids.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon.crew.node import TaskNode, validate_node

FENCE = "60m"
OVER_BUDGET = "240m"
INSIDE_BUDGET = "45m"
REASON = "H200 gate measured at 3 h in run X"

FLIGHT_LAYER = """\
default_backend: worker
backends:
  worker:
    launch: cli
    command: codex
    model: some-model
    effort: high
    sandbox: worktree-full
fences:
  time_budget: 60m
"""

PLAN_HTML = """\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="docs-project" content="proj">
  <meta name="reckon-type" content="plan">
  <meta name="plan-slug" content="a-fenced-plan">
  <meta name="plan-title" content="A Fenced Plan">
  <meta name="plan-status" content="active">
  <meta name="plan-effort-hours" content="1">
</head>
<body>
  <h2 id="s1">&#167;1 &mdash; One node</h2>
  <section data-reckon="section" data-id="s1" data-effort-hours="1"
           data-capability-version="1.0" data-capability-class="general"
           data-capability-reasoning="standard"
           data-capability-verification="strict" data-capability-risk="low"
           data-status="implementable" data-links=""></section>
</body>
</html>
"""

GOAL = "land the fenced section's change"
DONE_WHEN = (
    "uv run pytest tests/test_a_budget_may_exceed_its_fence_with_a_reason.py "
    "reports passed"
)


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temp crew home whose flight layer resolves a one-hour fence."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    (home / "flight.yaml").write_text(FLIGHT_LAYER, encoding="utf-8")
    return home


@pytest.fixture()
def plan_repo(tmp_path: Path, crew_home: Path) -> Path:
    """A committed throwaway repository carrying one plan of one section."""
    root = tmp_path / "repo"
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "docs" / "plans" / "a-fenced-plan.html").write_text(
        PLAN_HTML, encoding="utf-8"
    )
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "docs/plans/a-fenced-plan.html"],
        ["commit", "-q", "-m", "chore: seed fixture"],
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)
    (crew_home / "mounts.json").write_text(
        json.dumps({"proj": str(root / "docs")}), encoding="utf-8"
    )
    return root


def _dispatch(repo: Path, *, time_budget: str, over_fence: str = ""):
    """Preview the dispatch a coordinator would issue for one plan section."""
    arguments = [
        "crew",
        "dispatch",
        "--project",
        "proj",
        "--plan",
        "a-fenced-plan",
        "--section",
        "s1",
        "--node",
        "a-fenced-node",
        "--goal",
        GOAL,
        "--done-when",
        DONE_WHEN,
        "--write-path",
        "result.json",
        "--spec-level",
        "guided",
        "--time-budget",
        time_budget,
        "--session",
        "session-a-fenced-node",
        "--repo",
        str(repo),
        "--dry-run",
    ]
    if over_fence:
        arguments += ["--over-fence", over_fence]
    return CliRunner().invoke(cli_module.main, arguments)


def _document(result) -> dict[str, Any]:
    return json.loads(result.stdout)


def _bounded_findings(document: dict[str, Any]) -> list[str]:
    return [
        finding["detail"]
        for finding in document["validation"]["findings"]
        if finding["property"] == "bounded"
    ]


def test_a_node_records_the_reason_it_carries(tmp_path: Path) -> None:
    """The reason rides the node, which is what the run record is built from."""
    node = TaskNode(
        id="a-fenced-node",
        goal=GOAL,
        plan="a-fenced-plan",
        section="s1",
        spec_level="guided",
        done_when=DONE_WHEN,
        write_paths=["result.json"],
        time_budget=OVER_BUDGET,
        over_fence_reason=REASON,
        manifest_path=str(tmp_path / "manifest.md"),
    )

    assert node.as_dict()["over_fence_reason"] == REASON


def test_an_over_fence_budget_with_a_reason_passes_the_bounded_check(
    tmp_path: Path,
) -> None:
    node = TaskNode(
        id="a-fenced-node",
        goal=GOAL,
        plan="a-fenced-plan",
        section="s1",
        spec_level="guided",
        done_when=DONE_WHEN,
        write_paths=["result.json"],
        time_budget=OVER_BUDGET,
        over_fence_reason=REASON,
        manifest_path=str(tmp_path / "manifest.md"),
    )

    verdict = validate_node(node, budget_ceiling=FENCE)

    assert verdict.ok, verdict.findings


def test_an_over_fence_budget_with_no_reason_is_refused_by_name(
    tmp_path: Path,
) -> None:
    node = TaskNode(
        id="a-fenced-node",
        goal=GOAL,
        plan="a-fenced-plan",
        section="s1",
        spec_level="guided",
        done_when=DONE_WHEN,
        write_paths=["result.json"],
        time_budget=OVER_BUDGET,
        manifest_path=str(tmp_path / "manifest.md"),
    )

    verdict = validate_node(node, budget_ceiling=FENCE)

    assert not verdict.ok
    details = [f["detail"] for f in verdict.findings if f["property"] == "bounded"]
    assert details, verdict.findings
    assert "--over-fence" in details[0]


def test_a_budget_inside_the_fence_is_unaffected(tmp_path: Path) -> None:
    node = TaskNode(
        id="a-fenced-node",
        goal=GOAL,
        plan="a-fenced-plan",
        section="s1",
        spec_level="guided",
        done_when=DONE_WHEN,
        write_paths=["result.json"],
        time_budget=INSIDE_BUDGET,
        manifest_path=str(tmp_path / "manifest.md"),
    )

    verdict = validate_node(node, budget_ceiling=FENCE)

    assert verdict.ok, verdict.findings
    assert "bounded" not in verdict.failed_properties


def test_dispatch_carries_the_reason_onto_the_node(
    plan_repo: Path, crew_home: Path
) -> None:
    result = _dispatch(plan_repo, time_budget=OVER_BUDGET, over_fence=REASON)

    assert result.exit_code == 0, result.output
    document = _document(result)
    assert document["node"]["time_budget"] == OVER_BUDGET
    assert document["node"]["over_fence_reason"] == REASON
    assert document["validation"]["ok"] is True, document["validation"]


def test_dispatch_refuses_an_over_fence_budget_with_no_reason(
    plan_repo: Path, crew_home: Path
) -> None:
    result = _dispatch(plan_repo, time_budget=OVER_BUDGET)

    assert result.exit_code != 0, result.output
    document = _document(result)
    assert document["error"] == "contract-validation"
    assert "--over-fence" in " ".join(_bounded_findings(document))


def test_dispatch_leaves_a_budget_inside_the_fence_alone(
    plan_repo: Path, crew_home: Path
) -> None:
    result = _dispatch(plan_repo, time_budget=INSIDE_BUDGET)

    assert result.exit_code == 0, result.output
    document = _document(result)
    assert document["validation"]["ok"] is True, document["validation"]
    assert _bounded_findings(document) == []
    assert document["node"]["over_fence_reason"] == ""


def test_a_blank_reason_admits_nothing(plan_repo: Path, crew_home: Path) -> None:
    """Whitespace is not a reason: the check reads the stripped text."""
    result = _dispatch(plan_repo, time_budget=OVER_BUDGET, over_fence="   ")

    assert result.exit_code != 0, result.output
    document = _document(result)
    assert document["error"] == "contract-validation"
    assert "--over-fence" in " ".join(_bounded_findings(document))
