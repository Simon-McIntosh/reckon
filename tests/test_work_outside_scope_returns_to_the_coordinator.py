"""Work outside a worker's fence returns to its coordinator.

A worker records what it found beyond its write scope under the manifest field
``outside_scope``. The entries reach the dispatching coordinator — on the
promotion report and on the run's ledger row — and the coordinator answers each
one with a plan section or with an instruction in the next dispatch's prompt.
Reckon no longer converts them into plan followups: the conversion step and its
public export are gone, so a manifest entry cannot become a bucket no roadmap
reads. A manifest written under the retired spelling still reads the same way,
mapped on the way in and never written back.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import _plan_html, crew, ledger
from reckon.crew.node import TaskNode
from reckon.crew.prompts import compose_prompt
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "proj"
PLAN = "plan-a"
ENTRY = "reckon/crew/reports.py is outside my fence"

CENSUS = Path(__file__).resolve().parents[1]
WORKER_PROTOCOL = (
    CENSUS / "skills" / "reckon-build" / "references" / "worker-protocol.md"
)
COORDINATOR_REFERENCE = (
    CENSUS / "skills" / "reckon-build" / "references" / "coordinator-reference.md"
)

RULE = "a plan section or"


def _write_resource(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'\n<meta name="docs-project" content="{PROJECT}">'
        f"\n<title>{state['slug']}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


def _seed_git_repository(root: Path) -> None:
    """A checkout promotion can host a landing commit in.

    Promotion refuses a checkout that cannot carry the landing commit, and the
    ledger reads an absent crew.json as a deletion to recover unless git can
    report the path was never tracked — a question it answers only from a
    checkout carrying at least one commit.
    """
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs"),
        ("commit", "-q", "-m", "seed repository"),
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "plans").mkdir(parents=True)
    _write_resource(
        root / "docs" / "plans" / f"{PLAN}.html",
        {
            "type": "plan",
            "slug": PLAN,
            "title": "Plan A",
            "status": "active",
            "version": 0,
            "comments": {},
        },
    )
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    _seed_git_repository(root)
    return root


def _manifest(field: str) -> str:
    return f"node: node-a\nstatus: complete\n{field}: {ENTRY}\nblockers: none\n"


def _write_pointer(repository: Path, run_id: str, manifest_text: str) -> None:
    manifests = repository.parent / "manifests"
    manifests.mkdir(exist_ok=True)
    manifest_path = manifests / f"{run_id}.md"
    manifest_path.write_text(manifest_text, encoding="utf-8")
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repository),
        "launch": "in-harness",
        "role": "implement",
        "member": "worker-a",
        "backend": "native",
        "created_at": "2026-10-23T12:00:00Z",
        "manifest_path": str(manifest_path),
        "closure_disposition": None,
        "node": {
            "id": "outside-scope-node",
            "plan": PLAN,
            "section": "§3",
            "time_budget": "25m",
            "write_paths": [],
        },
    }
    _write_json(pointer_path(run_id), pointer)


def _promote(repository: Path, run_id: str) -> dict:
    # The review gate is not this node's subject, and its verdict depends on the
    # tree the promotion resolves the run's revision in — a fixture whose
    # pointer names no worktree resolves that tree from the working directory,
    # so a review record would make this test pass or fail by where pytest was
    # started rather than by what the report carries. The waiver says that.
    return crew.complete(
        run_id,
        gate="passed",
        no_commit="test: the report and the row are the deliverable",
        review_waiver="test: the outside-scope entry is the subject, not the review gate",
        root=repository,
    )


def _row(repository: Path, run_id: str) -> dict:
    runs = ledger.load(PROJECT, repository)[0]["runs"]
    return next(item for item in runs if item["run_id"] == run_id)


# ── the deleted conversion ──────────────────────────────────────────────────


def test_the_manifest_conversion_and_its_export_are_gone() -> None:
    assert not hasattr(crew, "followup_ops_from_manifest")
    source = (CENSUS / "reckon" / "crew" / "reports.py").read_text(encoding="utf-8")
    assert "followup_ops_from_manifest" not in source


# ── the entries reach the coordinator ───────────────────────────────────────


def test_outside_scope_entries_reach_the_users_promotion_report_and_ledger_row(
    repository: Path,
) -> None:
    run_id = "r-20261023T120000000001-outside-scope"
    _write_pointer(repository, run_id, _manifest("outside_scope"))

    report = _promote(repository, run_id)

    assert report["outside_scope"] == [ENTRY]
    row = _row(repository, run_id)
    assert row["outside_scope_paths"] == ["reckon/crew/reports.py"]
    assert "follow_on_paths" not in row


def test_a_manifest_written_under_the_retired_key_reads_the_same(
    repository: Path,
) -> None:
    run_id = "r-20261023T120000000002-retired-key"
    _write_pointer(repository, run_id, _manifest("follow_ons"))

    report = _promote(repository, run_id)

    assert report["outside_scope"] == [ENTRY]
    assert _row(repository, run_id)["outside_scope_paths"] == ["reckon/crew/reports.py"]


def test_the_parser_maps_the_retired_key_and_never_writes_it_back() -> None:
    parsed = crew.parse_manifest(_manifest("follow_ons"))
    assert parsed["outside_scope"] == [ENTRY]
    assert "follow_ons" not in parsed

    assert crew.parse_manifest(_manifest("outside_scope"))["outside_scope"] == [ENTRY]


# ── the prompt and the references state the rule ────────────────────────────


def test_the_worker_prompt_carries_the_field_and_the_rule() -> None:
    node = TaskNode(
        id="outside-scope-node",
        goal="carry the outside-scope field and the rule",
        plan=PLAN,
        section="s3",
        role="implement",
        done_when="the composed prompt carries the field and the rule",
        write_paths=["reckon/crew/prompts.py"],
        time_budget="20m",
    )
    prompt = compose_prompt(
        node=node,
        project=PROJECT,
        worktree="/repo/worktree",
        working_directory="/repo/worktree",
        manifest_path="/state/runs/outside-scope-node/manifest.md",
        time_budget="20m",
        needs_help_after_failures=2,
        can_write_worktree=True,
    )

    assert "outside_scope:" in prompt
    assert "follow_ons" not in prompt
    assert "next dispatch's prompt" in prompt


def test_neither_reference_converts_the_entries_into_followups() -> None:
    for path in (WORKER_PROTOCOL, COORDINATOR_REFERENCE):
        text = path.read_text(encoding="utf-8")
        assert "followup_ops_from_manifest" not in text, path
        assert "follow_ons" not in text, path
        assert "outside_scope" in text, path
        assert RULE in text, path
