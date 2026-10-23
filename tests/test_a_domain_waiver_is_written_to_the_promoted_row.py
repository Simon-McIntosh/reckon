"""A waived domain check keeps its name and reason on the promoted row.

The promotion guard returns the waiver it applied, and until that record
reaches the committed ledger row it survives only until the live pointer is
deleted. A reviewer auditing a landed run then cannot see that a declared
invariant failed and was waived, or why — while every other waiver a promotion
accepts rides the row.

The fixture declares two invariants. One matches the node's write paths and
fails, so a promotion of that node is refused until a reason is stated; the
other matches a second node's paths and passes, so the same reader can see the
field empty on a promotion whose check ran and held. The negative control is
the absent key: on the base revision the row carries no such field at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import reckon.mcp as mcp_module
from reckon import ledger
from reckon.crew import promotion as promotion_module
from reckon.crew.routing import run_directory_of
from reckon.crew.runs import _write_json, pointer_path
from tests.helpers.git import git

PROJECT = "sample"

#: The declared invariants the guard replays. The failing one is the waiver's
#: subject; the passing one is what an unwed row reads against.
DOMAIN_FLIGHT = """\
domain:
  invariants:
    - id: convergence
      rule: the section count must match the converged count
      applies_to: ["models/**"]
      check: "exit 3"
    - id: notation
      rule: every module keeps its declared public names
      applies_to: ["tools/**"]
      check: "exit 0"
"""

WAIVER_REASON = "the record predates the convergence fix"


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthetic config home and repository, isolated from the real fleet."""
    config_home = tmp_path / "config"
    (config_home / "crew" / "live").mkdir(parents=True)
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    # The run identity a crew worker exports is left out of this test's own
    # process: the git shim refuses a mutating verb whose run's pointer it
    # cannot read, and a throwaway fixture repository is exactly the state that
    # guard is not for. Promotion stages the ledger row with git.
    monkeypatch.delenv("RECKON_RUN_ID", raising=False)
    root = tmp_path / "repo"
    state = root / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "flight.yaml").write_text(DOMAIN_FLIGHT, encoding="utf-8")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "chore: seed"),
    ):
        git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _pointer(repository: Path, run_id: str, manifest: Path, write_path: str) -> None:
    """A run whose write paths decide which declared invariant applies."""
    base = git(repository, "rev-parse", "HEAD")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: domain-waiver\nstatus: complete\ntests: not applicable\n",
        encoding="utf-8",
    )
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": base,
            "launch": "in-harness",
            "role": "implement",
            "member": "worker-domain-waiver",
            "backend": "native",
            "created_at": "2026-10-22T05:00:00Z",
            "manifest_path": str(manifest),
            "node": {
                "id": "domain-waiver",
                "plan": "fixture",
                "section": "row",
                "time_budget": "40m",
                "write_paths": [write_path],
            },
        },
    )


def _rows(repository: Path) -> dict[str, dict]:
    """Read the committed rows through the same reader a coordinator uses."""
    payload = mcp_module._crew(PROJECT, view="records", checkout_path=str(repository))
    assert payload["ok"] is True, payload
    return {row["run_id"]: row for row in payload["runs"]}


def test_a_waived_domain_check_lands_on_the_promoted_row(
    repository: Path, tmp_path: Path
) -> None:
    """The waived check and its reason survive promotion, in the committed row."""
    run_id = "run-waived"
    _pointer(
        repository, run_id, tmp_path / "manifests" / f"{run_id}.md", "models/tape.py"
    )

    answer = mcp_module._crew(
        action="complete",
        run_id=run_id,
        gate="not-run",
        outcome="landed with a declared invariant waived",
        waive_domain_check=WAIVER_REASON,
    )
    assert answer.get("ok") is True, answer
    # The check really ran over the node's paths, so the waiver stood in for a
    # failing check rather than for an inert guard.
    log = run_directory_of({"run_id": run_id}) / "domain-check-convergence.log"
    log_text = log.read_text(encoding="utf-8") if log.is_file() else ""
    assert log.is_file() and "EXIT=3" in log_text, (log, log_text)

    row = _rows(repository)[run_id]
    assert "domain_waiver" in ledger.RECORD_FIELDS
    assert set(ledger.RECORD_FIELDS) <= set(row)
    waiver = row.get("domain_waiver")
    assert waiver is not None, row
    assert waiver["waived_invariants"] == ["convergence"], waiver
    assert waiver["reason"] == WAIVER_REASON, waiver


def test_a_promotion_without_a_waiver_carries_the_field_empty(
    repository: Path, tmp_path: Path
) -> None:
    """A row whose declared check held records no waiver rather than a blank one."""
    run_id = "run-unwaived"
    _pointer(
        repository, run_id, tmp_path / "manifests" / f"{run_id}.md", "tools/notes.md"
    )

    answer = mcp_module._crew(
        action="complete",
        run_id=run_id,
        gate="not-run",
        outcome="landed with every declared invariant holding",
    )
    assert answer.get("ok") is True, answer

    row = _rows(repository)[run_id]
    assert "domain_waiver" in row, row
    assert row["domain_waiver"] is None, row


def test_the_waiver_the_guard_applied_is_the_one_on_the_row(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row's record is the guard's own product, not a second reading of the CLI.

    The guard is called with the stated reason and its returned waiver is the
    value the row carries, so a later refactor cannot write a reason the guard
    never applied.
    """
    run_id = "run-captured"
    _pointer(
        repository, run_id, tmp_path / "manifests" / f"{run_id}.md", "models/tape.py"
    )
    seen: dict = {}
    real = promotion_module._require_domain_invariant_checks

    def _spy(*args, **kwargs):
        product = real(*args, **kwargs)
        seen["product"] = product
        return product

    monkeypatch.setattr(
        promotion_module, "_require_domain_invariant_checks", _spy, raising=True
    )

    answer = mcp_module._crew(
        action="complete",
        run_id=run_id,
        gate="not-run",
        outcome="landed with a declared invariant waived",
        waive_domain_check=WAIVER_REASON,
    )
    assert answer.get("ok") is True, answer
    row = _rows(repository)[run_id]
    assert row["domain_waiver"] == seen["product"], (row["domain_waiver"], seen)
    # Stated here as the words themselves: a row's reason compared against the
    # constant the test handed the tool could agree with the guard's input while
    # the row carried something else entirely.
    assert (
        row["domain_waiver"]["reason"] == "the record predates the convergence fix"
    ), row["domain_waiver"]
