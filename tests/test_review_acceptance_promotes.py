"""A clean review promotes the run it read without a coordinator command.

The promotion refusal that stops a run landing without a review exists so a
source change is read by an independent reviewer. The review's own deliverable
is a stored record scoring all five dimensions, and a record that is clean — a
total at or above the acceptance floor and no finding at all — is the state a
coordinator would otherwise promote by hand. This node adds the accepting
branch: when a clean-review run is promotable, the sweep confirms the run's own
recorded gate passed, re-runs that gate against the repository's merged head,
and promotes with ``promoted_by: review-acceptance`` on the ledger row.

Both halves are asserted, because an accepting branch on its own is
indistinguishable from one that promotes everything: a run promotable with a
clean stored review is promoted with no coordinator command; a review carrying
one finding is not; and a run whose recorded gate was green but whose merged
tree no longer satisfies the same command is not.

The fixture asserts afterwards that the workstation's real crew pointer
directory is untouched, because an isolated read does not prove an isolated
write.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import _plan_html, crew, ledger
from reckon.crew import recovery
from reckon.crew import review as review_module
from reckon.crew.runs import _write_json, crew_home, pointer_path

PROJECT = "review-acceptance-fixture"
PLAN = "acceptance-target"
RUN_ID = "r-20261002T120000000000-acceptance-target"
GATE_COMMAND = "sh gate.sh"


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write_resource(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{state['slug']}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


@pytest.fixture(autouse=True)
def real_crew_home_is_not_a_fixture_target() -> None:
    """No fixture may reach the workstation's real crew pointer directory."""
    real_pointer = (
        Path.home() / ".config" / "reckon" / "crew" / "live" / f"{RUN_ID}.json"
    )
    assert not real_pointer.exists()
    yield
    assert not real_pointer.exists()


@pytest.fixture()
def repository(isolated_reckon_home: Path, tmp_path: Path) -> Path:
    """A committed repository whose docs directory is the project's mount.

    The checkout is seeded with a green gate (``sh gate.sh`` greps ``node.txt``
    for ``satisfied``), so the re-run at the merged head has a real command to
    execute. Each test lands its own run commit onto the same main branch, which
    is what "the run merged" means by the time acceptance runs.
    """
    assert crew_home().is_relative_to(isolated_reckon_home)
    root = tmp_path / "repo"
    _write_resource(
        root / "docs" / "plans" / f"{PLAN}.html",
        {
            "type": "plan",
            "slug": PLAN,
            "title": "Acceptance Target",
            "status": "active",
            "version": 0,
            "comments": {},
        },
    )
    (root / "docs" / "state" / PROJECT).mkdir(parents=True, exist_ok=True)
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    (root / "gate.sh").write_text(
        '#!/bin/sh\ngrep -q "satisfied" node.txt\n', encoding="utf-8"
    )
    (root / "node.txt").write_text("satisfied\n", encoding="utf-8")
    _git(root, "add", "seed.txt", "gate.sh", "node.txt", "docs")
    _git(root, "commit", "-q", "-m", "chore: seed repository")
    (isolated_reckon_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}),
        encoding="utf-8",
    )
    return root


def _land_run_commit(repository: Path, *, green_gate: bool = True) -> str:
    """Land the run's deliverable on main, as a merged run leaves it.

    ``green_gate`` stays out of the signature — ``green_gate=False`` makes the
    run's own commit tighten the gate so the same command that was recorded
    green no longer passes at the merged head, which is the state the
    merged-head test measures.
    """
    (repository / "candidate.txt").write_text("delivered\n", encoding="utf-8")
    paths = ["candidate.txt"]
    if not green_gate:
        (repository / "gate.sh").write_text(
            '#!/bin/sh\ngrep -q "satisfied" node.txt '
            '&& grep -q "contract-marker" node.txt\n',
            encoding="utf-8",
        )
        paths.append("gate.sh")
    _git(repository, "add", *paths)
    _git(repository, "commit", "-q", "-m", "feat: land the run deliverable")
    return _git(repository, "rev-parse", "HEAD")


def _gate_log(tmp_path: Path) -> Path:
    log = tmp_path / "gate.log"
    log.write_text("gate ran\nEXIT=0\n", encoding="utf-8")
    return log


def _store_review(
    values: dict[str, int],
    *,
    base: str,
    head: str,
    findings: list[dict[str, str]] | None = None,
) -> None:
    """Store a review keyed to the exact revision it read.

    Keying the base/head pair explicitly keeps selection off the time-based
    reconstruction a legacy record would use, so which record the sweep selects
    cannot depend on which commit the review's timestamp happens to resolve to.
    """
    emitted = "\n".join(
        f"SCORE {dimension}: {values[dimension]}"
        for dimension in review_module.REVIEW_DIMENSIONS
    )
    record = review_module.parse_review(emitted)
    if findings:
        record["findings"] = [dict(finding) for finding in findings]
    record.update(
        {
            "project": PROJECT,
            "reviewed_run_id": RUN_ID,
            "reviewed_base_sha": base,
            "reviewed_head_sha": head,
            "review_run_id": f"review-of-{RUN_ID}",
        }
    )
    review_module.store_review(record)


def _write_run(
    repository: Path,
    tmp_path: Path,
    *,
    base: str,
    commit: str,
) -> None:
    manifest = tmp_path / "manifests" / f"{RUN_ID}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: acceptance-target\n"
        "status: complete\n"
        f"commits: {commit}\n"
        "changed_paths: candidate.txt\n"
        f"tests: {GATE_COMMAND}\n"
        f"test_logs:\n  - {_gate_log(tmp_path)}\n",
        encoding="utf-8",
    )
    _write_json(
        pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": base,
            "launch": "in-harness",
            "role": "implement",
            "backend": "native",
            "created_at": "2026-01-01T00:00:00Z",
            "manifest_path": str(manifest),
            "node": {
                "id": "acceptance-target",
                "plan": PLAN,
                "section": "s2",
                "time_budget": "25m",
                "role": "implement",
                "write_paths": ["candidate.txt"],
            },
        },
    )


def _sweep() -> dict:
    """Run the reflex sweep as a hand-run caller does, with no session filter."""
    return recovery.dispatch_awaiting_reviews(
        project=PROJECT, launcher=lambda *args, **kwargs: 0
    )


def _land_and_review(
    repository: Path, tmp_path: Path, *, green_gate: bool = True
) -> None:
    base = _git(repository, "rev-parse", "HEAD")
    commit = _land_run_commit(repository, green_gate=green_gate)
    _write_run(repository, tmp_path, base=base, commit=commit)
    _store_review(
        dict.fromkeys(review_module.REVIEW_DIMENSIONS, 20), base=base, head=commit
    )


def test_a_clean_review_promotes_its_run_without_a_coordinator(
    repository: Path, tmp_path: Path
) -> None:
    """The accepting branch: promotable + clean review + green merged gate."""
    _land_and_review(repository, tmp_path)

    report = _sweep()

    assert report["accepted"] == [RUN_ID], report
    assert not pointer_path(RUN_ID).exists()
    rows = ledger.runs(PROJECT, root=repository)
    assert [row["run_id"] for row in rows] == [RUN_ID]
    assert rows[0]["promoted_by"] == "review-acceptance"


def test_a_review_with_one_finding_does_not_promote(
    repository: Path, tmp_path: Path
) -> None:
    """A single finding returns the run to its coordinator, as before."""
    base = _git(repository, "rev-parse", "HEAD")
    commit = _land_run_commit(repository)
    _write_run(repository, tmp_path, base=base, commit=commit)
    _store_review(
        dict.fromkeys(review_module.REVIEW_DIMENSIONS, 20),
        base=base,
        head=commit,
        findings=[{"file": "candidate.txt", "line": "1", "text": "one finding"}],
    )

    report = _sweep()

    assert report["accepted"] == []
    assert pointer_path(RUN_ID).is_file()
    assert ledger.runs(PROJECT, root=repository) == []


def test_a_gate_failing_at_the_merged_head_does_not_promote(
    repository: Path, tmp_path: Path
) -> None:
    """The recorded gate passed, but the merged tree no longer satisfies it."""
    _land_and_review(repository, tmp_path, green_gate=False)

    report = _sweep()

    assert report["accepted"] == []
    refusal = next(
        entry
        for entry in report["reports"]
        if entry.get("run_id") == RUN_ID and "accepted" in entry
    )
    assert refusal["accepted"] is False
    # The refusal is the re-run's verdict, not an earlier guard: the recorded
    # gate read green, and the same command failed on the tree that ships.
    assert refusal["gate_report"]["integrated_verdict"] == "failed"
    assert pointer_path(RUN_ID).is_file()
    assert ledger.runs(PROJECT, root=repository) == []


def test_promoted_by_is_present_on_every_ledger_row(
    repository: Path, tmp_path: Path
) -> None:
    """The field is declared, so an absent value reads null, never missing."""
    _land_and_review(repository, tmp_path)
    _sweep()

    [row] = ledger.runs(PROJECT, root=repository)
    assert "promoted_by" in row
    assert row["promoted_by"] == "review-acceptance"


def test_a_coordinator_promoted_row_carries_a_null_promoted_by(
    repository: Path, tmp_path: Path
) -> None:
    """A row no review accepted is distinguishable from one it did."""
    base = _git(repository, "rev-parse", "HEAD")
    commit = _land_run_commit(repository)
    _write_run(repository, tmp_path, base=base, commit=commit)
    _store_review(
        dict.fromkeys(review_module.REVIEW_DIMENSIONS, 20), base=base, head=commit
    )

    crew.complete(RUN_ID, gate="passed", commits=[commit], root=repository)

    [row] = ledger.runs(PROJECT, root=repository)
    assert row["promoted_by"] is None
