"""The lander waits for every review tier the run owes at its head.

The landing pass used to ask the review store for the run's review with no
rubric, and every text-review reader excludes figure records by design. So a
run that changed figure images — and therefore owed a figure review — was
judged on its code review alone: the figure review could be missing, queued or
never dispatched, and the pass saw a run with nothing outstanding.

These cases drive the pass itself. A run owing both tiers is delivered with
only the code review stored and the pass must not land it; its record names the
figure tier it waits for. Once that review is stored and complete the same run
lands, including from a refusal recorded while the figure review was missing —
a refusal holds only while the review records it read are unchanged, so storing
the missing tier has to lift it. A run owing only a code review still lands on
that review alone.

Every repository, store and live pointer resolves inside a temporary
configuration home, so nothing here reads or writes the operator's own fleet.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import _plan_html
from reckon.crew import queue_landing, runs
from reckon.crew import review as review_module

PROJECT = "landing-fixture"
PLAN = "delivery-plan"
SESSION = "owner"
RUN = "r-delivered-implementation"
FIGURE = "docs/figures/delivery-plan/implementation/panel.svg"


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _write_plan(repo: Path) -> Path:
    path = repo / "docs" / "plans" / f"{PLAN}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    authored = (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        '<title>Delivery plan</title></head><body><main class="plan-doc">'
        '<h2 id="s1">One section</h2><p>The section body.</p>'
        "</main></body></html>"
    )
    path.write_text(
        _plan_html.write_state(
            authored,
            {
                "project": PROJECT,
                "type": "plan",
                "slug": PLAN,
                "title": "Delivery plan",
                "status": "active",
                "modified": "2026-10-27",
                "version": 0,
                "section_declarations": {"s1": "implementable"},
                "sections": [
                    {
                        "id": "s1",
                        "effort_hours": 1.0,
                        "capability": {
                            "version": "1.0",
                            "class": "general",
                            "requirements": {
                                "reasoning": "standard",
                                "verification": "strict",
                                "risk": "low",
                            },
                        },
                        "attempts": 0,
                        "status": "implementable",
                        "links": [],
                    }
                ],
            },
        ),
        encoding="utf-8",
    )
    return path


def _pointer(repo: Path, *, base: str, manifest: Path) -> dict:
    return {
        "run_id": RUN,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(repo),
        "worktree": str(repo),
        "base_sha": base,
        "process_alive": False,
        "launch": "in-harness",
        "role": "implement",
        "manifest_path": str(manifest),
        "node": {
            "id": "implementation",
            "plan": PLAN,
            "section": "s1",
            "role": "implement",
            "write_paths": [
                "reckon/crew/queue_landing.py",
                FIGURE,
            ],
        },
    }


@pytest.fixture()
def delivery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A delivered run whose commits change runtime source and a figure image."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    scratch_root = tmp_path / "scratch-root"
    scratch_root.mkdir()
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(scratch_root))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "worker@example.invalid")
    _git(repo, "config", "user.name", "Worker")
    _write_plan(repo)
    state = repo / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}), encoding="utf-8"
    )
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    module = repo / "reckon" / "crew"
    module.mkdir(parents=True)
    (module / "__init__.py").write_text("", encoding="utf-8")
    (module / "queue_landing.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "docs", "reckon")
    _git(repo, "commit", "-q", "-m", "chore: seed fixture")
    base = _git(repo, "rev-parse", "HEAD")
    (module / "queue_landing.py").write_text("VALUE = 2\n", encoding="utf-8")
    figure = repo / FIGURE
    figure.parent.mkdir(parents=True)
    figure.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>\n", encoding="utf-8")
    _git(repo, "add", "reckon", "docs/figures")
    _git(repo, "commit", "-q", "-m", "feat: change the module and its figure")
    head = _git(repo, "rev-parse", "HEAD")
    manifest = runs.run_dir(RUN) / runs.DEFAULT_MANIFEST_NAME
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        f"node: implementation\nstatus: complete\ncommits: [{head}]\n",
        encoding="utf-8",
    )
    runs._write_json(
        runs.pointer_path(RUN), _pointer(repo, base=base, manifest=manifest)
    )
    return {"repo": repo, "base": base, "head": head, "manifest": manifest}


def _store_code_review(base: str, head: str) -> dict:
    """Store the run's text review: every dimension scored, no finding."""
    parsed = review_module.parse_review(
        "\n".join(
            f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
        )
    )
    parsed.update(
        {
            "project": PROJECT,
            "reviewed_run_id": RUN,
            "review_run_id": f"review-of-{RUN}",
            "reviewed_base_sha": base,
            "reviewed_head_sha": head,
        }
    )
    review_module.store_review(parsed)
    return parsed


def _store_figure_review(base: str, head: str, *, complete: bool = True) -> dict:
    """Store a figure review of the delivered head.

    ``complete`` writes the record the store's delivery contract accepts. An
    incomplete record is the same review mid-write: it names the revision it
    read and no failure pair, which the contract refuses.
    """
    record: dict = {
        "project": PROJECT,
        "reviewed_run_id": RUN,
        "review_run_id": f"review-of-{RUN}-figure",
        "reviewed_base_sha": base,
        "reviewed_head_sha": head,
        "rubric": review_module.FIGURE_REVIEW_RUBRIC,
        "status": "parsed",
        "findings": [],
    }
    if complete:
        record["added_failure_count"] = 0
        record["added_failure_ids"] = []
    review_module.store_review(record)
    return record


def _landed_without_landing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        queue_landing, "_land", lambda run_id: pytest.fail("held run landed")
    )


def test_a_run_owing_a_figure_review_is_not_landed_on_its_code_review(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The figure review is missing, so the pass records which tier it waits for."""
    _store_code_review(str(delivery["base"]), str(delivery["head"]))
    _landed_without_landing(monkeypatch)

    result = queue_landing.land_once()

    assert result["landed"] == [], result
    landing = runs.read_pointer(RUN)["admission_landing"]
    assert landing["status"] == "awaiting-review", landing
    assert landing["review_tier"] == queue_landing.FIGURE_REVIEW_TIER, landing
    assert queue_landing.recovery.same_revision(landing["head"], delivery["head"])


def _seed_refusal_for_the_missing_figure_review(delivery) -> dict:
    """Record the run refused while its figure review was still missing.

    The refusal names the code stamp it was judged under and the identity of
    every review record the rule read at that moment — the code review, and the
    figure review's absence. That pair is what makes the refusal a judgement
    about the state it saw, and what storing the missing review has to change.
    """
    code_review, _ = queue_landing._review_for_tier(
        PROJECT,
        RUN,
        str(delivery["head"]),
        delivery["repo"],
        queue_landing.CODE_REVIEW_TIER,
    )
    assert code_review is not None, "the code review must be stored first"
    record = {
        "head": str(delivery["head"]),
        "guard": "a passing gate requires the check that produced it",
        "status": "refused",
        "code_stamp": queue_landing._landing_code_stamp(),
        "review_stamp": queue_landing._owed_review_stamp(
            {
                queue_landing.CODE_REVIEW_TIER: code_review,
                queue_landing.FIGURE_REVIEW_TIER: None,
            }
        ),
    }
    pointer = runs.read_pointer(RUN)
    pointer["admission_landing"] = record
    runs._write_json(runs.pointer_path(RUN), pointer)
    return record


def test_storing_the_owed_figure_review_lands_the_held_run(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal for the missing figure review lifts when that review is stored."""
    _store_code_review(str(delivery["base"]), str(delivery["head"]))
    seeded = _seed_refusal_for_the_missing_figure_review(delivery)
    _landed_without_landing(monkeypatch)

    held = queue_landing.land_once()

    # The refusal still stands with the figure review missing: the pass reads
    # the record and leaves it alone rather than judging the head again.
    assert held["landed"] == [], held
    assert runs.read_pointer(RUN)["admission_landing"] == seeded

    landed: list[str] = []
    monkeypatch.setattr(
        queue_landing,
        "_land",
        lambda run_id: (landed.append(run_id), (True, ""))[1],
    )
    _store_figure_review(str(delivery["base"]), str(delivery["head"]))

    result = queue_landing.land_once()

    assert result["landed"] == [RUN], result
    assert landed == [RUN]


def test_a_run_owing_only_a_code_review_lands_on_that_review_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: a run that changed no figure image owes no figure review."""
    delivery = _delivered_without_a_figure(tmp_path, monkeypatch)
    _store_code_review(str(delivery["base"]), str(delivery["head"]))
    landed: list[str] = []
    monkeypatch.setattr(
        queue_landing,
        "_land",
        lambda run_id: (landed.append(run_id), (True, ""))[1],
    )

    result = queue_landing.land_once()

    assert result["landed"] == [RUN], result
    assert landed == [RUN]


def _delivered_without_a_figure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict:
    """The same delivery with no figure image in its commits.

    Built beside the main fixture rather than by editing it, so the control
    differs from the case it controls in one fact: whether a figure review is
    owed.
    """
    home = tmp_path / "control-config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    scratch_root = tmp_path / "control-scratch"
    scratch_root.mkdir()
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(scratch_root))
    repo = tmp_path / "control-repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "worker@example.invalid")
    _git(repo, "config", "user.name", "Worker")
    _write_plan(repo)
    state = repo / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}), encoding="utf-8"
    )
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    module = repo / "reckon" / "crew"
    module.mkdir(parents=True)
    (module / "__init__.py").write_text("", encoding="utf-8")
    (module / "queue_landing.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "docs", "reckon")
    _git(repo, "commit", "-q", "-m", "chore: seed fixture")
    base = _git(repo, "rev-parse", "HEAD")
    (module / "queue_landing.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(repo, "add", "reckon")
    _git(repo, "commit", "-q", "-m", "feat: change the module")
    head = _git(repo, "rev-parse", "HEAD")
    manifest = runs.run_dir(RUN) / runs.DEFAULT_MANIFEST_NAME
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        f"node: implementation\nstatus: complete\ncommits: [{head}]\n",
        encoding="utf-8",
    )
    runs._write_json(
        runs.pointer_path(RUN), _pointer(repo, base=base, manifest=manifest)
    )
    return {"repo": repo, "base": base, "head": head, "manifest": manifest}
