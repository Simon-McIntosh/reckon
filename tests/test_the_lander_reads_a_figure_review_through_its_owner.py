"""The lander reads a figure review through its owner, and through it alone.

A run that changes a figure image owes a figure review, and the lander must
judge that review by the same reading promotion does. Two defects followed from
reading it any other way. Read through the text-review store the figure tier
answered ``None`` for a record that could not be parsed, so an unreadable
figure review read as no figure review and the run was landed on evidence
nobody had read. And judged by the store's list-only completeness check a
figure review carrying an undisposed blocking finding read as complete, so the
lander attempted the land and promotion refused one step later, naming a land
failure rather than the finding.

These cases drive the pass itself, in a temporary configuration home. A figure
review with a blocking finding holds the run, and the record names the finding;
an unparsable figure record is recorded as unreadable rather than absent; the
awaiting record names its tier under a key of its own, never the run record's
sweep tier; and a store read that raises records the error in its own field
with ``stale_head`` left empty.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import _plan_html
from reckon.crew import promotion_checks, queue_landing, runs
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
        "suite_command": "true",
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


def _store_figure_review(base: str, head: str, *, findings: list | None = None) -> dict:
    """Store the run's figure review of the delivered head."""
    record: dict = {
        "project": PROJECT,
        "reviewed_run_id": RUN,
        "review_run_id": f"review-of-{RUN}-figure",
        "reviewed_base_sha": base,
        "reviewed_head_sha": head,
        "rubric": review_module.FIGURE_REVIEW_RUBRIC,
        "status": "parsed",
        "findings": list(findings or []),
        "added_failure_count": 0,
        "added_failure_ids": [],
    }
    review_module.store_review(record)
    return record


def _figures_review_store() -> Path:
    return review_module.review_store_root() / PROJECT


def _landed_without_landing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        queue_landing, "_land", lambda run_id: pytest.fail("held run landed")
    )


def test_a_figure_review_with_an_undisposed_blocking_finding_holds_the_run(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The finding is the reason the run waits, and the record names it."""
    _store_code_review(str(delivery["base"]), str(delivery["head"]))
    _store_figure_review(
        str(delivery["base"]),
        str(delivery["head"]),
        findings=[
            {
                "file": FIGURE,
                "line": 1,
                "text": "the axis label is clipped at the canvas edge",
                "severity": "blocking",
            }
        ],
    )
    _landed_without_landing(monkeypatch)

    result = queue_landing.land_once()

    assert result["landed"] == [], result
    landing = runs.read_pointer(RUN)["admission_landing"]
    assert landing["status"] == "awaiting-review", landing
    assert landing["awaiting_tier"] == queue_landing.FIGURE_REVIEW_TIER, landing
    assert FIGURE in landing["review_findings"], landing
    assert "axis label is clipped" in landing["review_findings"], landing


def test_an_unparsable_figure_record_is_recorded_as_unreadable(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable figure record is not the same fact as no figure record."""
    _store_code_review(str(delivery["base"]), str(delivery["head"]))
    store = _figures_review_store()
    store.mkdir(parents=True, exist_ok=True)
    unreadable_path = store / f"{RUN}.figure.mid-write.json"
    unreadable_path.write_text('{"project": "landing-fixture", "find', encoding="utf-8")
    _landed_without_landing(monkeypatch)

    result = queue_landing.land_once()

    assert result["landed"] == [], result
    landing = runs.read_pointer(RUN)["admission_landing"]
    assert landing["status"] == "awaiting-review", landing
    assert landing["awaiting_tier"] == queue_landing.FIGURE_REVIEW_TIER, landing
    unreadable = landing["unreadable_reviews"]
    assert isinstance(unreadable, list) and unreadable, landing
    assert unreadable_path.name in json.dumps(unreadable), landing


def test_the_awaiting_record_names_its_tier_under_its_own_key(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``review_tier`` belongs to the run record's sweep tier, not to landing."""
    _store_code_review(str(delivery["base"]), str(delivery["head"]))
    _landed_without_landing(monkeypatch)

    queue_landing.land_once()

    landing = runs.read_pointer(RUN)["admission_landing"]
    assert landing["status"] == "awaiting-review", landing
    assert landing["awaiting_tier"] == queue_landing.FIGURE_REVIEW_TIER, landing
    assert "review_tier" not in landing, landing


def test_a_store_read_that_raises_records_its_error_apart_from_stale_head(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A revision field never carries an exception message."""
    _store_code_review(str(delivery["base"]), str(delivery["head"]))

    def _raise(*_args, **_kwargs):
        raise OSError("the review store is not readable")

    monkeypatch.setattr(promotion_checks, "_newest_figure_review", _raise)
    _landed_without_landing(monkeypatch)

    result = queue_landing.land_once()

    assert result["landed"] == [], result
    landing = runs.read_pointer(RUN)["admission_landing"]
    assert landing["status"] == "awaiting-review", landing
    assert "stale_head" not in landing, landing
    assert "the review store is not readable" in landing["review_read_error"], landing


def test_a_clean_figure_review_still_lands(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: the reading above does not hold a review with nothing open."""
    _store_code_review(str(delivery["base"]), str(delivery["head"]))
    _store_figure_review(str(delivery["base"]), str(delivery["head"]))
    landed: list[str] = []
    monkeypatch.setattr(
        queue_landing,
        "_land",
        lambda run_id: (landed.append(run_id), (True, ""))[1],
    )

    result = queue_landing.land_once()

    assert result["landed"] == [RUN], result
    assert landed == [RUN]
