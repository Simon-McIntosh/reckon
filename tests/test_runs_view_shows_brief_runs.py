"""The runs view shows a brief run's missing plan beside its brief digest.

A brief run takes a stored brief in place of a plan section, so its runs-view
row names no plan. Promotion writes the brief's digest onto the ledger row, and
the runs view carries that digest beside the null plan, so a reader tells a
brief run from a plan run by the row itself rather than by inference. The skill
that names the brief carrier no longer states the retired rule that no flag
exists for passing prose to a worker.

Every case is hermetic: ``RECKON_HOME`` moves the transient crew directory into
a temp tree and the repository is a throwaway git repo, so no real ledger or
live pointer directory is read or written.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from reckon import crew, mcp
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "proj"
BRIEF_SHA = "b" * 64
ROOT = Path(__file__).parents[1]

# The phrase the skill used to state, and the carrier that replaces it.
RETIRED_RULE = "no flag for passing prose to a worker"
BRIEF_CARRIER = "reckon crew dispatch --brief <file>"

# The pointer records the promoting process as the run's worker, so a promotion
# of one of these fixtures is admitted only with the live-run waiver an operator
# would give. What each case asserts is the runs-view row a promotion produces.
_PLUMBING_WAIVER = (
    "the pytest process stands in for the worker; this fixture exercises "
    "promotion plumbing"
)


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt", "docs")
    _git(root, "commit", "-q", "-m", "test: seed repository")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _write_brief_pointer(repository: Path, run_id: str, *, role: str) -> None:
    record: dict = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repository),
        "worktree": str(repository),
        "launch": "in-harness",
        "role": role,
        "backend": "native",
        "pid": os.getpid(),
        "created_at": "2026-09-30T08:00:00Z",
        "node": {
            "id": "node-brief",
            "plan": "",
            "section": "",
            "brief": "brief.md",
            "brief_sha256": BRIEF_SHA,
            "brief_path": "",
            "time_budget": "25m",
            "write_paths": [],
        },
    }
    _write_json(pointer_path(run_id), record)


def _promote(repository: Path, run_id: str):
    return crew.complete(
        run_id,
        root=repository,
        live_run_waiver=_PLUMBING_WAIVER,
        gate="passed",
        outcome="the probe ran",
    )


def test_a_promoted_brief_run_shows_a_null_plan_and_its_digest(
    repository: Path,
) -> None:
    run_id = "r-20260930T130000000000-node-brief"
    _write_brief_pointer(repository, run_id, role="investigate")
    _promote(repository, run_id)

    # A plan run in the same ledger, so the row for a brief run is contrasted
    # with the row a plan run produces rather than only read on its own.
    crew.ledger.append_run(
        PROJECT,
        crew.ledger.build_record(
            run_id="r-20260930T125900000000-node-plan",
            plan="plan-a",
            section="§2",
            node="node-plan",
            gate="passed",
            member_id="member-a",
        ),
        root=repository,
    )

    result = mcp._crew(
        PROJECT,
        view="runs",
        checkout_path=str(repository),
        source="ledger",
    )

    rows = {row["run_id"]: row for row in result["rows"]}
    brief_row = rows[run_id]
    assert brief_row["plan"] is None
    # Read through ``get`` so a dropped digest reads as a wrong value rather
    # than a KeyError, which is the failure the declared mutation produces.
    assert brief_row.get("brief_sha256") == BRIEF_SHA

    plan_row = rows["r-20260930T125900000000-node-plan"]
    assert plan_row["plan"] == "plan-a"
    assert "brief_sha256" not in plan_row


def test_a_live_brief_run_shows_a_null_plan_and_its_digest(
    repository: Path,
) -> None:
    run_id = "r-20260930T131500000000-node-brief"
    _write_brief_pointer(repository, run_id, role="investigate")

    # Nothing is promoted here: the run is read while its live pointer is the
    # only record, which is the state it spends its whole life in. The digest
    # reaches the row from the node block dispatch writes, not the ledger.
    result = mcp._crew(
        PROJECT,
        view="runs",
        checkout_path=str(repository),
        source="live",
        run_id=run_id,
    )

    rows = [row for row in result["rows"] if row["run_id"] == run_id]
    assert len(rows) == 1
    live_row = rows[0]
    assert live_row["plan"] is None
    assert live_row.get("brief_sha256") == BRIEF_SHA


def test_the_skill_retires_the_rule_and_names_the_brief_carrier() -> None:
    # The build skill keeps its core in SKILL.md and its detail in references/,
    # so the rule is read across both, as a coordinator loading the skill does.
    skill_dir = ROOT / "skills" / "reckon-build"
    skill = " ".join(
        "\n".join(
            [skill_dir.joinpath("SKILL.md").read_text()]
            + [
                path.read_text()
                for path in sorted((skill_dir / "references").glob("*.md"))
            ]
        ).split()
    )

    assert RETIRED_RULE not in skill
    assert "deliberately no flag for passing prose to a worker" not in skill
    # The replacement says which carrier replaces a plan for which work.
    assert "may instead carry a **brief**" in skill
    assert "changes a plan's product carries its plan" in skill
    assert "same eight node properties" in skill
    assert "done-when is still the specification" in skill
    assert BRIEF_CARRIER in skill
