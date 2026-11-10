"""A batch entry is judged by the node contract when it is queued.

A batch used to store an entry that a single dispatch would refuse, so the
refusal surfaced later at the launch that first tried it — in the entry's
pointer ``failure`` field, where nobody reads it — after the queue position had
been spent. The contract the single path applies is now judged while the request
is still a request, and this file pins the three facts that makes true: an
invalid entry beside a valid one is refused and its sibling stored, a batch of
valid entries is stored whole, and a single dispatch refuses the same node for
the same property.

The node contract is one function, ``node.validate_node``, called by both
``dispatch_plan.plan_dispatch`` and ``dispatch.queue_requests``; these tests
drive the two public paths rather than the function, because what a contract
means is what the paths do with it.
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

#: Each case drives queueing through the real reckon home and a real checkout,
#: so a fixture that blocks would stall an arm rather than fail it. The bound
#: turns a hang into a named failure the arm reports.
pytestmark = pytest.mark.timeout(120)

PROJECT = "proj"
SESSION = "coordinator-a"
PLAN = "plan-a"
LANE = "stub-lane"
TEST_FILE = "tests/test_batch_example.py"
DONE_WHEN = f"pytest {TEST_FILE} reports 1 passed."

#: The refusal names the field a dispatcher must fill, which is the whole point
#: of judging it early: the batch row has to say what to change.
NEGATIVE_CONTROL_DETAIL = "negative_control"

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


def _plan_document() -> str:
    quote = '"'
    prose = (
        "<p>Judge a batch entry by the whole node contract.</p>"
        "<p>Write paths: <code>src/module.py</code>.</p>"
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
    fleet = root / "skills" / "reckon-build" / "scripts" / "worktree_fleet.py"
    fleet.parent.mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
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
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "worker@example.invalid")
    _git(root, "config", "user.name", "Worker")
    _git(root, "add", "--", "docs", "src", "tests", "skills", "seed.txt")
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
        "done_when": DONE_WHEN,
        "write_paths": ["src/module.py"],
    }
    entry.update(overrides)
    return entry


def _invalid_entry() -> dict:
    """A node whose write paths include a test file and declare no mutation."""
    return _entry(node="no-negative-control", write_paths=[TEST_FILE])


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


def _stored_entries(result: dict) -> list[dict]:
    return [row for row in result["results"] if row.get("run_id")]


def test_a_batch_stores_the_valid_entry_and_refuses_the_invalid_one(
    crew_home: Path, repository: Path
) -> None:
    before = _pointers()

    result = _queue(repository, [_invalid_entry(), _entry(node="valid-entry")])

    assert result["refused"] == 1, result
    assert result["queued"] == 1, result
    refused_row, stored_row = result["results"]
    assert refused_row["status"] == "refused", refused_row
    assert NEGATIVE_CONTROL_DETAIL in str(refused_row["reason"]), refused_row
    assert stored_row["status"] in {"queued", "awaiting-upstream"}, stored_row
    # Exactly one pointer was written, and it belongs to the stored entry: the
    # refused entry never becomes a request, so no launch can spend a queue
    # position on it and no obligation reports it as waiting.
    after = _pointers()
    assert len(after) == len(before) + 1, after
    assert f"{stored_row['run_id']}.json" in after


def test_a_batch_of_valid_entries_is_queued_whole(
    crew_home: Path, repository: Path
) -> None:
    before = _pointers()

    result = _queue(
        repository,
        [_entry(node="first-entry"), _entry(node="second-entry")],
    )

    assert result["refused"] == 0, result
    assert result["queued"] == 2, result
    stored = _stored_entries(result)
    assert len(stored) == 2, stored
    assert len({row["run_id"] for row in stored}) == 2, stored
    after = _pointers()
    assert len(after) == len(before) + 2, after
    for row in stored:
        assert f"{row['run_id']}.json" in after, row


def test_a_single_dispatch_refuses_the_same_node_for_the_same_property(
    crew_home: Path, repository: Path
) -> None:
    entry = _invalid_entry()

    payload = _dry_run(entry, repository)

    assert (payload.get("validation") or {}).get("ok") is False, payload
    assert NEGATIVE_CONTROL_DETAIL in _dry_run_details(payload), payload


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
    return " || ".join(
        f"{finding.get('property') or ''}: {finding.get('detail') or ''}"
        for finding in findings
    )
