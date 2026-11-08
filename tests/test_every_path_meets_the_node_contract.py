"""Every dispatch path judges a node by the whole contract.

A batch entry used to be checked only against the admission domain, so the
contract a single dispatch applies — the node's own properties, the negative
control a check must declare, the gate population its repository must resolve,
and the execution fit of its measure to its role — surfaced later at admission,
where the refusal costs the queue position and reaches nobody. ``validate_node``
is the judge for all of it, and every path that can store or send a node now
calls it with the same inputs.

The tests below drive the batch path through ``queue_batch`` and the single path
through the CLI, and they assert the same detail on both, because two paths that
disagree about what a contract is are two contracts.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import crew_dispatch_commands
from reckon.crew import dispatch_batch, runs

#: Every case here drives queueing or discovery through the real rekon home, so
#: a fixture that blocks would stall an arm instead of failing it. The bound
#: turns a hang into a named failure the arm can report.
pytestmark = pytest.mark.timeout(120)

PROJECT = "proj"
SESSION = "coordinator-a"
PLAN = "plan-a"
LANE = "stub-lane"
TEST_FILE = "tests/test_batch_example.py"
GOOD_DONE_WHEN = f"pytest {TEST_FILE} reports 1 passed."
EXECUTION_DONE_WHEN = f"run the test suite with pytest {TEST_FILE}"
CONJUNCTION_GOAL = "read the config; run the lane"

CONFIG = {
    "default_backend": LANE,
    "local_backend": LANE,
    "backends": {
        LANE: {
            "launch": "in-harness",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        }
    },
    "roles": {
        "implement": {},
        "review": {"execution_capable": False, "sandbox": "read-only"},
        "test": {"execution_capable": True},
    },
    "fences": {"time_budget": "2h", "needs_help_after_failures": 2},
}

#: The sentence each refusal carries, matched on both paths. It is the detail
#: the batch row and the dry-run finding must agree on.
CONJUNCTION_DETAIL = "split it into one node per deliverable"
NEGATIVE_CONTROL_DETAIL = "declares no mutation that check must fail against"
POPULATION_DETAIL = "holds no file matching"
EXECUTION_DETAIL = "declares execution_capable false"


def _plan_document() -> str:
    quote = '"'
    prose = (
        "<p>Judge a batch entry by the whole node contract.</p>"
        f"<p>Write paths: <code>src/module.py</code>.</p>"
        f"<p><strong>Done when</strong> pytest {TEST_FILE} reports the contract "
        "holding.</p>"
    )
    return (
        "<!doctype html>\n"
        f"<meta name={quote}docs-project{quote} content={quote}{PROJECT}{quote}>\n"
        f"<meta name={quote}reckon-type{quote} content={quote}plan{quote}>\n"
        f"<meta name={quote}plan-slug{quote} content={quote}{PLAN}{quote}>\n"
        f"<meta name={quote}plan-status{quote} content={quote}active{quote}>\n"
        f"<h2 id={quote}s1{quote}>§1</h2>{prose}\n"
    )


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        crew_dispatch_commands,
        "_resolved_flight",
        lambda *args, **kwargs: json.loads(json.dumps(CONFIG)),
    )
    return config_home


@pytest.fixture()
def repository(tmp_path: Path, crew_home: Path) -> Path:
    root = tmp_path / "checkout"
    state = root / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n",
        encoding="utf-8",
    )
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / f"{PLAN}.html").write_text(_plan_document(), encoding="utf-8")
    module = root / "src" / "module.py"
    module.parent.mkdir(parents=True)
    module.write_text("def helper():\n    return 1\n", encoding="utf-8")
    example = root / TEST_FILE
    example.parent.mkdir(parents=True)
    example.write_text(
        "def test_the_batch_example_holds():\n    assert 1 == 1\n", encoding="utf-8"
    )
    # The dispatch path resolves its worktree through this script, so the tree
    # it will operate on carries the copy the repository ships.
    fleet = root / "skills" / "reckon-build" / "scripts" / "worktree_fleet.py"
    fleet.parent.mkdir(parents=True)
    fleet.write_text(
        (
            Path(__file__).parents[1]
            / "skills"
            / "reckon-build"
            / "scripts"
            / "worktree_fleet.py"
        ).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "worker@example.invalid")
    _git(root, "config", "user.name", "Worker")
    _git(
        root,
        "add",
        "--",
        "docs",
        "src",
        "tests",
        "skills",
        "seed.txt",
    )
    _git(root, "commit", "-q", "-m", "chore: seed the contract checkout")
    (crew_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _entry(**overrides: object) -> dict:
    entry: dict = {
        "project": PROJECT,
        "plan": PLAN,
        "section": "s1",
        "node": "contract-node",
        "role": "implement",
        "goal": "Add one helper the batch example calls.",
        "time_budget": "20m",
        "spec_level": "exact",
        "done_when": GOOD_DONE_WHEN,
        "write_paths": ["src/module.py"],
    }
    entry.update(overrides)
    return entry


def _queue(repository: Path, entries: list[dict]) -> dict:
    return dispatch_batch.queue_batch(
        entries,
        session=SESSION,
        config_for=lambda _project: json.loads(json.dumps(CONFIG)),
        repo_root=repository,
    )


def _pointers() -> list[str]:
    live = runs.live_dir()
    if not live.is_dir():
        return []
    return sorted(path.name for path in live.glob("*.json"))


def _dry_run(entry: dict, repository: Path) -> dict:
    """The single path's preview of one entry, judged by the same contract."""
    arguments = [
        "crew",
        "dispatch",
        "--dry-run",
        "--project",
        str(entry["project"]),
        "--plan",
        str(entry["plan"]),
        "--section",
        str(entry["section"]),
        "--role",
        str(entry["role"]),
        "--spec-level",
        str(entry["spec_level"]),
        "--node",
        str(entry["node"]),
        "--goal",
        str(entry["goal"]),
        "--done-when",
        str(entry["done_when"]),
        "--time-budget",
        str(entry["time_budget"]),
        "--session",
        SESSION,
        "--repo",
        str(repository),
        "--manifest",
        str(repository / "manifest.md"),
    ]
    for path in entry.get("write_paths") or ():
        arguments += ["--write-path", str(path)]
    if entry.get("negative_control"):
        arguments += ["--negative-control", str(entry["negative_control"])]
    result = CliRunner().invoke(cli_module.main, arguments)
    assert result.output.strip(), result.output
    return json.loads(result.output)


def _dry_run_details(payload: dict) -> str:
    findings = (payload.get("validation") or {}).get("findings") or []
    return " || ".join(str(finding.get("detail") or "") for finding in findings)


# ── the batch path carries every refusal ────────────────────────────────────


def test_a_compound_goal_is_refused_while_its_sibling_is_stored(
    crew_home: Path, repository: Path
) -> None:
    before = _pointers()
    compound = _entry(node="compound", goal=CONJUNCTION_GOAL)
    sibling = _entry(node="sibling")

    result = _queue(repository, [compound, sibling])

    assert result["refused"] == 1, result
    assert result["queued"] == 1, result
    refused_row, stored_row = result["results"]
    assert refused_row["status"] == "refused", refused_row
    assert CONJUNCTION_DETAIL in str(refused_row["reason"]), refused_row
    assert stored_row["status"] in {"queued", "awaiting-upstream"}, stored_row
    after = _pointers()
    assert len(after) == len(before) + 1, "one pointer, for the stored entry"
    assert f"{stored_row['run_id']}.json" in after


def test_a_check_with_no_negative_control_is_refused(
    crew_home: Path, repository: Path
) -> None:
    before = _pointers()
    entry = _entry(node="no-negative", write_paths=[TEST_FILE])

    result = _queue(repository, [entry])

    assert result["refused"] == 1, result
    assert NEGATIVE_CONTROL_DETAIL in str(result["results"][0]["reason"]), result
    assert _pointers() == before, "a refused entry writes no pointer"


def test_a_gate_population_matching_nothing_is_refused(
    crew_home: Path, repository: Path
) -> None:
    before = _pointers()
    entry = _entry(
        node="missing-glob",
        done_when="pytest tests/test_missing_*.py reports 1 passed.",
    )

    result = _queue(repository, [entry])

    assert result["refused"] == 1, result
    assert POPULATION_DETAIL in str(result["results"][0]["reason"]), result
    assert _pointers() == before, "a refused entry writes no pointer"


def test_an_execution_measure_on_an_incapable_role_is_refused(
    crew_home: Path, repository: Path
) -> None:
    before = _pointers()
    entry = _entry(node="incapable", role="review", done_when=EXECUTION_DONE_WHEN)

    result = _queue(repository, [entry])

    assert result["refused"] == 1, result
    assert EXECUTION_DETAIL in str(result["results"][0]["reason"]), result
    assert _pointers() == before, "a refused entry writes no pointer"


# ── the single path reports the same detail ─────────────────────────────────


@pytest.mark.parametrize(
    ("entry", "detail"),
    [
        (_entry(node="compound", goal=CONJUNCTION_GOAL), CONJUNCTION_DETAIL),
        (
            _entry(node="no-negative", write_paths=[TEST_FILE]),
            NEGATIVE_CONTROL_DETAIL,
        ),
        (
            _entry(
                node="missing-glob",
                done_when="pytest tests/test_missing_*.py reports 1 passed.",
            ),
            POPULATION_DETAIL,
        ),
        (
            _entry(node="incapable", role="review", done_when=EXECUTION_DONE_WHEN),
            EXECUTION_DETAIL,
        ),
    ],
)
def test_the_single_path_reports_the_same_detail(
    crew_home: Path, repository: Path, entry: dict, detail: str
) -> None:
    batch_row = _queue(repository, [entry])["results"][0]
    assert detail in str(batch_row["reason"]), batch_row

    payload = _dry_run(entry, repository)

    assert (payload.get("validation") or {}).get("ok") is False, payload
    assert detail in _dry_run_details(payload), payload


# ── the override still lets the dispatch through, and is recorded ───────────


def test_an_execution_override_dispatches_and_records_the_label(
    crew_home: Path, repository: Path
) -> None:
    manifest = crew_home / "crew" / "runs" / "override.md"
    arguments = [
        "crew",
        "dispatch",
        "--project",
        PROJECT,
        "--plan",
        PLAN,
        "--section",
        "s1",
        "--role",
        "review",
        "--spec-level",
        "exact",
        "--node",
        "override-node",
        "--goal",
        "Verify one runtime route the batch example exercises.",
        "--done-when",
        EXECUTION_DONE_WHEN,
        "--write-path",
        str(manifest),
        "--time-budget",
        "20m",
        "--manifest",
        str(manifest),
        "--session",
        "override-session",
        "--repo",
        str(repository),
        "--allow-execution-mismatch",
    ]

    result = CliRunner().invoke(cli_module.main, arguments)

    assert result.exit_code == 0, result.output
    record = json.loads(result.output)
    assert record["execution_fit"] == {
        "allowed": True,
        "execution_capable": False,
        "matched_measure": "run the test suite",
        "override": True,
        "role": "review",
        "status": "overridden",
    }
    persisted = json.loads(runs.pointer_path(record["run_id"]).read_text())
    assert persisted["execution_fit"] == record["execution_fit"]


# ── a queued request keeps the execution override its dispatch recorded ─────


def _queued_record(
    repository: Path, *, allow_execution_mismatch: bool
) -> tuple[object, dict]:
    """A request as a dispatch that passed the override would have stored it."""
    from reckon.crew.node import TaskNode

    node = TaskNode(
        id="queued-override",
        goal="Verify one runtime route the batch example exercises.",
        plan=PLAN,
        section="s1",
        role="review",
        spec_level="exact",
        done_when=EXECUTION_DONE_WHEN,
        write_paths=["src/module.py"],
        time_budget="20m",
        manifest_path=str(repository / "manifest.md"),
    )
    record = {
        "run_id": "r-queued-override",
        "project": PROJECT,
        "repo": str(repository),
        "backend": LANE,
        "node": node.as_dict(),
        "dispatch_options": {"allow_execution_mismatch": allow_execution_mismatch},
    }
    return node, record


def test_an_edit_keeps_the_overridden_requests_measure(
    crew_home: Path, repository: Path
) -> None:
    """A request stored with the override is not refused by every edit."""
    from reckon.crew import queue_edit

    node, record = _queued_record(repository, allow_execution_mismatch=True)

    findings = queue_edit._contract_findings(node, record)

    assert not [
        finding
        for finding in findings
        if EXECUTION_DETAIL in str(finding.get("detail") or "")
    ], findings


def test_an_edit_refuses_a_request_that_recorded_no_override(
    crew_home: Path, repository: Path
) -> None:
    """The control: the same measure without the recorded override is refused."""
    from reckon.crew import queue_edit

    node, record = _queued_record(repository, allow_execution_mismatch=False)

    findings = queue_edit._contract_findings(node, record)

    assert [
        finding
        for finding in findings
        if EXECUTION_DETAIL in str(finding.get("detail") or "")
    ], findings
