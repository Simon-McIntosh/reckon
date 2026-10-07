"""The gate and the sub-floor obligation read only a hot review.

A finding is a live obligation only while the review that carries it is hot.
The dispatch gate admits a plan whose design review has landed while a content
review covers the content about to be built, and still refuses when the finding
sits on a hot review.
"""

from __future__ import annotations

import json
import subprocess
from importlib import import_module
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import node as node_module
from reckon.crew import plan_review
from reckon.crew import review as review_module
from reckon.crew import review_lifecycle as lifecycle_module
from reckon.crew import runs as runs_module

obligations_module = import_module("reckon.crew.obligations")

DESIGN_FINDING = "design-orphan-finding"

CONFIG = {
    "default_backend": "worker",
    "backends": {
        "worker": {
            "launch": "in-harness",
            "model": "test-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        }
    },
    "roles": {
        role: {"backend": "worker", "execution_capable": True}
        for role in ("implement", "investigate", "review", "test")
    },
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}


def _git(tree: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments], cwd=tree, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


GATE_PROJECT = "hot-set-gate-fixture"
GATE_PLAN = "fixture"

_PLAN_HTML = (
    "<!doctype html>\n<html><head>\n"
    '<meta name="docs-project" content="{project}">\n'
    '<meta name="plan-slug" content="{slug}">\n'
    '<meta name="plan-status" content="active">\n'
    '<meta name="plan-impl" content="0.5">\n'
    '<meta name="plan-version" content="1">\n'
    '<meta name="plan-section-declarations"\n'
    '      content=\'{{"foundation": "done", "delivery": "implementable"}}\'>\n'
    "</head><body>\n"
    '<h2 id="foundation">Foundation</h2><p>Settled.</p>\n'
    '<h2 id="delivery">Delivery</h2><p>Ship one change.</p>\n'
    "</body></html>\n"
)


@pytest.fixture()
def gate_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path]:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repo = tmp_path / "repo"
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    plan_path = plans / f"{GATE_PLAN}.html"
    plan_path.write_text(
        _PLAN_HTML.format(project=GATE_PROJECT, slug=GATE_PLAN), encoding="utf-8"
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", f"docs/plans/{GATE_PLAN}.html"],
        ["commit", "-q", "-m", "chore: seed gate fixture"],
    ):
        _git(repo, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({GATE_PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    return config_home, repo, plan_path


def _review_store_root(config_home: Path) -> Path:
    return config_home / "crew" / "reviews"


def _store_design_review(
    plan_path: Path,
    config_home: Path,
    *,
    section_digests: dict[str, str],
) -> None:
    """Store a design review carrying one unanswered finding.

    The review's ``section_digests`` name the sections it read, which is what
    makes it landed once those sections are declared done. It carries no
    whole-plan fingerprint, so it covers no current content and the gate joins a
    newer content review to the plan instead.
    """
    plan_review.store_plan_review(
        {
            "project": GATE_PROJECT,
            "plan_slug": GATE_PLAN,
            "plan_version": 1,
            "rubric": "plan_design_review",
            "reviewed_blob_sha": "a" * 40,
            "section_digests": section_digests,
            "findings": [
                {"id": DESIGN_FINDING, "type": "reuse", "text": "name the owner"}
            ],
            "responses": {},
            "review_run_id": "r-design-review",
        },
        base_dir=_review_store_root(config_home),
    )


def _store_content_review(plan_path: Path, config_home: Path) -> None:
    """Store the content review whose fingerprint matches the current plan."""
    plan_review.store_plan_review(
        {
            "project": GATE_PROJECT,
            "plan_slug": GATE_PLAN,
            "plan_version": 1,
            "rubric": "plan_review",
            "reviewed_blob_sha": "b" * 40,
            "plan_fingerprint": plan_review.plan_fingerprint(plan_path),
            "findings": [],
            "responses": {},
            "review_run_id": "r-content-review",
        },
        base_dir=_review_store_root(config_home),
    )


def _node(config_home: Path) -> crew.TaskNode:
    return crew.TaskNode(
        id="delivery",
        goal="ship one measured change",
        plan=GATE_PLAN,
        section="delivery",
        role="implement",
        spec_level="exact",
        done_when="pytest reports one passing hot-set gate case",
        write_paths=["src/change.py"],
        time_budget="20m",
        manifest_path=str(config_home / "manifests" / "delivery.md"),
    )


def _plan(node: crew.TaskNode, repo: Path, *, mode: str = "enforce"):
    config = {**CONFIG, "plan_review_gate": mode}
    return crew.plan_dispatch(
        node=node, config=config, project=GATE_PROJECT, repo=repo, base="HEAD"
    )


def test_the_gate_admits_a_landed_design_review_with_an_unanswered_finding(
    gate_project: tuple[Path, Path, Path],
) -> None:
    """A design review whose reviewed sections are done no longer refuses.

    The design review read only ``foundation``, now declared done, so it is
    landed and its unanswered finding is archive; the content review covers the
    content about to be built, so the gate has a hot review to read.
    """
    config_home, repo, plan_path = gate_project
    _store_design_review(plan_path, config_home, section_digests={"foundation": "d"})
    _store_content_review(plan_path, config_home)

    resolution = _plan(_node(config_home), repo)

    assert resolution.validation.ok is True


def test_the_gate_refuses_an_unanswered_finding_of_a_hot_review(
    gate_project: tuple[Path, Path, Path],
) -> None:
    config_home, repo, plan_path = gate_project
    # The design review read ``delivery``, still implementable, so it is not
    # landed: its unanswered finding is a live obligation and the gate refuses.
    _store_design_review(plan_path, config_home, section_digests={"delivery": "d"})
    _store_content_review(plan_path, config_home)

    expected_error = getattr(node_module, "PlanReviewMissingError", crew.CrewError)
    with pytest.raises(expected_error) as excinfo:
        _plan(_node(config_home), repo)

    refusal = str(excinfo.value)
    assert DESIGN_FINDING in refusal
    assert "unanswered" in refusal


def test_the_gate_refuses_a_plan_with_no_design_review(
    gate_project: tuple[Path, Path, Path],
) -> None:
    config_home, repo, plan_path = gate_project
    _store_content_review(plan_path, config_home)

    expected_error = getattr(node_module, "PlanReviewMissingError", crew.CrewError)
    with pytest.raises(expected_error) as excinfo:
        _plan(_node(config_home), repo)

    assert "design" in str(excinfo.value)


# ── The sub-floor obligation ────────────────────────────────────────────────

OBLIGATION_PROJECT = "hot-set-obligation-fixture"
OBLIGATION_SESSION = "coordinator-fixture"
FIXTURE_PLAN = "fixture-plan"
SUB_FLOOR_SCORE = 5
DECLARED_FLOOR = 10

_SUB_FLOOR_SCORES = {
    "goal_fidelity": 19,
    "evidence": 19,
    "scope_discipline": 18,
    "durability": SUB_FLOOR_SCORE,
    "fit": 18,
    "reuse": 18,
}


def _emit(scores: dict[str, int], *, base_sha: str, head_sha: str) -> str:
    lines = [f"reviewed_base_sha: {base_sha}", f"reviewed_head_sha: {head_sha}"]
    lines.extend(f"SCORE {dimension}: {score}" for dimension, score in scores.items())
    return "\n".join(lines) + "\n"


@pytest.fixture()
def obligation_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.delenv("RECKON_FLIGHT_CONFIG", raising=False)

    repository = tmp_path / "repo"
    (repository / "docs" / "state" / OBLIGATION_PROJECT).mkdir(parents=True)
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt"],
        ["commit", "-q", "-m", "test: seed obligation fixture"],
    ):
        _git(repository, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({OBLIGATION_PROJECT: str(repository / "docs")}),
        encoding="utf-8",
    )

    tree = tmp_path / "run-tree"
    tree.mkdir()
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
    ):
        _git(tree, *arguments)
    (tree / "work.txt").write_text("base\n", encoding="utf-8")
    _git(tree, "add", "work.txt")
    _git(tree, "commit", "-q", "-m", "test: base revision")
    base_sha = _git(tree, "rev-parse", "HEAD")
    (tree / "work.txt").write_text("head\n", encoding="utf-8")
    _git(tree, "add", "work.txt")
    _git(tree, "commit", "-q", "-m", "test: head revision")
    head_sha = _git(tree, "rev-parse", "HEAD")

    (config_home / "flight.yaml").write_text(
        f"gates:\n  dimension_floors:\n    durability: {DECLARED_FLOOR}\n",
        encoding="utf-8",
    )
    return {
        "config_home": config_home,
        "repository": repository,
        "tree": tree,
        "base_sha": base_sha,
        "head_sha": head_sha,
    }


def _write_pointer(run_id: str, *, worktree: Path) -> None:
    runs_module._write_json(
        runs_module.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": OBLIGATION_PROJECT,
            "session": OBLIGATION_SESSION,
            "process_alive": False,
            "worktree": str(worktree),
            "node": {
                "id": run_id,
                "plan": FIXTURE_PLAN,
                "section": "fixture-section",
                "time_budget": "20m",
                "write_paths": ["seed.txt"],
            },
        },
    )


def _store_running_review(
    run_id: str, *, base_sha: str, head_sha: str, dispatched: str
) -> Path:
    record = review_module.parse_review(
        _emit(_SUB_FLOOR_SCORES, base_sha=base_sha, head_sha=head_sha)
    )
    record.update(
        {
            "project": OBLIGATION_PROJECT,
            "reviewed_run_id": run_id,
            "dispatched_at": dispatched,
            "timestamp": dispatched,
        }
    )
    return review_module.store_review(record)


def _sub_floor_rows() -> list[dict[str, object]]:
    report = obligations_module.obligations(OBLIGATION_PROJECT, OBLIGATION_SESSION)
    return [
        row
        for row in report["obligations"]
        if row["kind"] == obligations_module.SUB_FLOOR_DUTY_KIND
    ]


def test_a_hot_review_raises_a_sub_floor_duty(obligation_project: dict) -> None:
    """The control: a below-floor dimension on a live, superseded-free review."""
    _write_pointer("run-below", worktree=obligation_project["tree"])
    _store_running_review(
        "run-below",
        base_sha=obligation_project["base_sha"],
        head_sha=obligation_project["head_sha"],
        dispatched="2026-09-28T11:00:00+00:00",
    )

    rows = _sub_floor_rows()

    assert [row["run_id"] for row in rows] == ["run-below"]
    assert rows[0]["dimension"] == "durability"
    assert rows[0]["score"] == SUB_FLOOR_SCORE


def test_a_run_with_a_ledger_row_raises_no_sub_floor_duty(
    obligation_project: dict,
) -> None:
    run_id = "run-promoted"
    _write_pointer(run_id, worktree=obligation_project["tree"])
    _store_running_review(
        run_id,
        base_sha=obligation_project["base_sha"],
        head_sha=obligation_project["head_sha"],
        dispatched="2026-09-28T11:00:00+00:00",
    )
    # The review exists and is below floor, so the duty's absence is the guard
    # firing rather than a missing record.
    assert review_module.read_review(OBLIGATION_PROJECT, run_id) is not None

    state_runs = (
        obligation_project["config_home"] / "state" / OBLIGATION_PROJECT / "runs"
    )
    state_runs.mkdir(parents=True, exist_ok=True)
    (state_runs / f"{run_id}.json").write_text(
        json.dumps({"run_id": run_id, "project": OBLIGATION_PROJECT}),
        encoding="utf-8",
    )

    assert _sub_floor_rows() == []


def test_a_superseded_review_raises_no_sub_floor_duty(
    obligation_project: dict,
) -> None:
    """A review a later round of the same run has superseded raises no duty.

    The committed tree answers the by-head selection first, so the selected
    record is the older-dispatched of the run's rounds while a later staging
    round exists; the lifecycle reads it superseded, not open.
    """
    run_id = "run-superseded"
    base_sha = obligation_project["base_sha"]
    head_sha = obligation_project["head_sha"]
    _write_pointer(run_id, worktree=obligation_project["tree"])
    _store_running_review(
        run_id,
        base_sha=base_sha,
        head_sha=head_sha,
        dispatched="2026-09-28T12:00:00+00:00",
    )
    committed_dir = (
        obligation_project["repository"]
        / "docs"
        / "state"
        / OBLIGATION_PROJECT
        / "reviews"
        / "run"
        / run_id
    )
    committed_dir.mkdir(parents=True, exist_ok=True)
    older = review_module.parse_review(
        _emit(_SUB_FLOOR_SCORES, base_sha=base_sha, head_sha=head_sha)
    )
    older.update(
        {
            "project": OBLIGATION_PROJECT,
            "reviewed_run_id": run_id,
            "dispatched_at": "2026-09-28T11:00:00+00:00",
            "timestamp": "2026-09-28T11:00:00+00:00",
        }
    )
    (committed_dir / "c-older-round.json").write_text(
        json.dumps(older), encoding="utf-8"
    )

    selected = review_module.read_review(OBLIGATION_PROJECT, run_id)
    assert selected is not None
    assert selected["dispatched_at"] == "2026-09-28T11:00:00+00:00"

    assert _sub_floor_rows() == []


# ── The loader's public reader ──────────────────────────────────────────────

LOADER_PROJECT = "hot-set-loader-fixture"
LOADER_REVIEWED_RUN = "r-20261006T120000000000-reviewed-run"
LOADER_RUN_REVIEW = "r-20261006T130000000000-run-review"


def _loader_run_record() -> dict:
    return {
        "project": LOADER_PROJECT,
        "reviewed_run_id": LOADER_REVIEWED_RUN,
        "review_run_id": LOADER_RUN_REVIEW,
        "dispatched_at": "2026-10-06T13:00:00+00:00",
        "findings": [{"id": "f1", "severity": "follow-on"}],
        "responses": {},
    }


@pytest.fixture()
def loader_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    (tmp_path / "config").mkdir()
    root = tmp_path / "repo"
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "docs" / "state" / LOADER_PROJECT).mkdir(parents=True)
    (root / "docs" / "plans" / "demo.html").write_text(
        "<html><head></head><body></body></html>", encoding="utf-8"
    )
    return root


def test_the_loader_enumerates_run_reviews_through_the_public_reader(
    loader_checkout: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "store"
    run_path = review_module.store_review(_loader_run_record(), base_dir=store)

    calls: list[str] = []
    real = review_module.reviewed_run_ids

    def _spy(project: str, *, base_dir=None) -> list[str]:
        calls.append(project)
        return real(project, base_dir=base_dir)

    monkeypatch.setattr(review_module, "reviewed_run_ids", _spy)

    states = lifecycle_module.review_lifecycles(
        LOADER_PROJECT, base_dir=store, root=loader_checkout
    )

    assert str(run_path) in states
    assert calls == [LOADER_PROJECT]


def test_the_public_reader_skips_the_plan_review_bucket(tmp_path: Path) -> None:
    store = tmp_path / "store"
    plan_module = import_module("reckon.crew.plan_review")
    plan_path = plan_module.store_plan_review(
        {
            "project": LOADER_PROJECT,
            "plan_slug": "demo",
            "plan_version": 1,
            "rubric": "plan_review",
            "findings": [{"id": "wiring-1", "would_change": True}],
        },
        base_dir=store,
    )
    review_module.store_review(_loader_run_record(), base_dir=store)

    # The plan review sits in the same directory the reader scans, so a reader
    # that did not drop the empty run-id bucket would list it as a run.
    assert plan_path.parent == Path(store).resolve() / LOADER_PROJECT
    assert review_module.reviewed_run_ids(LOADER_PROJECT, base_dir=store) == [
        LOADER_REVIEWED_RUN
    ]
