"""A landing keeps its measurement when main moves outside the measured inputs.

A landing measures a delivery against the primary revision it merges onto. Every
commit to main used to discard that measurement, so a main kept busy by plan
edits and review records deferred each reviewed run on every pass and re-ran the
suite pair: a livelock that left reviewed work unlanded for as long as main
moved. A commit that leaves the measured inputs unchanged cannot change what was
measured, so the measurement is reused and judged against the primary as it now
stands.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import _plan_html
from reckon.crew import landing_measure, queue_landing, runs
from reckon.crew import review as review_module
from reckon.crew.node import CrewError

PROJECT = "landing-fixture"
PLAN = "delivery-plan"
SESSION = "owner"
RUN = "r-measured-delivery"
OTHER = "r-waiting-delivery"
SUITE = "python -m pytest -q tests/test_alpha.py"


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


class _Yield:
    """A context manager handing back a prepared tree."""

    def __init__(self, tree: Path) -> None:
        self._tree = tree

    def __enter__(self) -> Path:
        return self._tree

    def __exit__(self, *_exc) -> None:
        return None


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


def _register_run(
    run_id: str, *, repo: Path, worktree: Path, base: str, head: str
) -> None:
    manifest = runs.run_dir(run_id) / runs.DEFAULT_MANIFEST_NAME
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: implementation\nstatus: complete\ncommits: [{head}]\n",
        encoding="utf-8",
    )
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": SESSION,
            "repo": str(repo),
            "worktree": str(worktree),
            "base_sha": base,
            "suite_command": SUITE,
            "process_alive": False,
            "launch": "in-harness",
            "role": "implement",
            "manifest_path": str(manifest),
            "node": {
                "id": f"implementation-{run_id}",
                "plan": PLAN,
                "section": "s1",
                "role": "implement",
                "write_paths": ["reckon/crew/queue_landing.py", "tests/test_alpha.py"],
            },
        },
    )


def _accept(*, base: str, head: str, run_id: str = RUN) -> None:
    parsed = review_module.parse_review(
        "\n".join(
            f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
        )
    )
    parsed.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": f"review-of-{run_id}",
            "reviewed_base_sha": base,
            "reviewed_head_sha": head,
        }
    )
    review_module.store_review(parsed)


def _record(run_id: str, *, head: str, main_sha: str) -> None:
    queue_landing._record_suite_evidence(
        run_id,
        landing_measure.Measurement(
            command=SUITE,
            suite={"command": SUITE, "base": {}, "head": {}},
            population={
                "files": [],
                "base_sha": main_sha,
                "main_sha": main_sha,
                "candidate_sha": head,
            },
            head=head,
            project=PROJECT,
        ),
    )


def _delivery_tree(tmp_path: Path) -> tuple[Path, Path, str, str]:
    """A checkout whose primary is at a seed, and a worktree carrying a delivery.

    The delivery is committed in its own worktree, so the shared checkout stays
    on ``main`` and can move past the delivery without carrying it: exactly the
    shape a live run has, where the primary and the run's tree are separate.
    """
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
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_alpha.py").write_text(
        "def test_real():\n    pass\n", encoding="utf-8"
    )
    module = repo / "reckon" / "crew"
    module.mkdir(parents=True)
    (module / "__init__.py").write_text("", encoding="utf-8")
    (module / "queue_landing.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "docs", "tests", "reckon")
    _git(repo, "commit", "-q", "-m", "chore: seed fixture")
    base = _git(repo, "rev-parse", "HEAD")
    worktree = tmp_path / "delivery-tree"
    _git(repo, "worktree", "add", "-q", "-b", "delivery", str(worktree))
    (worktree / "reckon" / "crew" / "queue_landing.py").write_text(
        "VALUE = 2\n", encoding="utf-8"
    )
    _git(worktree, "add", "reckon")
    _git(worktree, "commit", "-q", "-m", "feat: change the module")
    head = _git(worktree, "rev-parse", "HEAD")
    return repo, worktree, base, head


def _deliver_in(tree: Path, path: str, addition: str, subject: str) -> str:
    """Commit ``addition`` to ``path`` in the run's worktree and return its head."""
    target = tree / path
    target.write_text(target.read_text(encoding="utf-8") + addition, encoding="utf-8")
    _git(tree, "add", path)
    _git(tree, "commit", "-q", "-m", subject)
    return _git(tree, "rev-parse", "HEAD")


def _move_main_with_a_plan_edit(repo: Path) -> str:
    plan = repo / "docs" / "plans" / f"{PLAN}.html"
    plan.write_text(plan.read_text(encoding="utf-8") + "<!-- a later edit -->\n")
    _git(repo, "add", "docs")
    _git(repo, "commit", "-q", "-m", "docs(plan): record an outcome")
    return _git(repo, "rev-parse", "HEAD")


def _move_main_with_a_product_edit(repo: Path) -> str:
    module = repo / "reckon" / "crew" / "landing_measure.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("OTHER = 3\n", encoding="utf-8")
    _git(repo, "add", "reckon")
    _git(repo, "commit", "-q", "-m", "fix: change the product")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def delivery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    scratch_root = tmp_path / "scratch-root"
    scratch_root.mkdir()
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(scratch_root))
    repo, worktree, base, head = _delivery_tree(tmp_path)
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    _register_run(RUN, repo=repo, worktree=worktree, base=base, head=head)
    _accept(base=base, head=head)
    prepared = tmp_path / "prepared-tree"
    prepared.mkdir()
    monkeypatch.setattr(
        landing_measure,
        "base_checkout",
        lambda repo, sha, timeout=0.0: _Yield(prepared),
    )
    monkeypatch.setattr(
        landing_measure,
        "candidate_checkout",
        lambda repo, primary, head, timeout=0.0: _Yield((head, prepared)),
    )
    return {
        "repo": repo,
        "worktree": worktree,
        "base": base,
        "head": head,
        "prepared": prepared,
    }


def _land(monkeypatch: pytest.MonkeyPatch):
    landed: list[str] = []

    def fake(run_id: str) -> tuple[bool, str]:
        landed.append(run_id)
        return True, ""

    monkeypatch.setattr(queue_landing, "_land", fake)
    return landed


def _measurement(**overrides) -> landing_measure.Measurement:
    main_sha = overrides.pop("main_sha", "")
    population = {
        "files": [],
        "base_sha": main_sha,
        "main_sha": main_sha,
        "candidate_sha": overrides.pop("candidate_sha", ""),
    }
    return landing_measure.Measurement(
        command=SUITE,
        suite={"command": SUITE, "base": {}, "head": {}},
        population=population,
        head=overrides.pop("head", ""),
        project=PROJECT,
        **overrides,
    )


def test_a_docs_only_move_on_main_reuses_the_stored_measurement(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The measured delivery lands without its suite pair being run again."""
    _record(RUN, head=delivery["head"], main_sha=delivery["base"])
    moved = _move_main_with_a_plan_edit(delivery["repo"])
    assert moved != delivery["base"]
    landed = _land(monkeypatch)

    def refuse(*_args, **_kwargs):
        raise AssertionError(
            "a measurement a docs-only move cannot change was repeated"
        )

    monkeypatch.setattr(landing_measure, "measure_run", refuse)

    result = queue_landing.land_once()

    assert result["landed"] == [RUN], result
    assert landed == [RUN]
    assert "deferred" not in result or not result["deferred"], result


def test_a_docs_only_move_is_judged_on_the_merge_onto_the_current_primary(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verification accepts the stored measurement and reads the live merge."""
    _record(RUN, head=delivery["head"], main_sha=delivery["base"])
    _move_main_with_a_plan_edit(delivery["repo"])

    verified = queue_landing._verified_measurement(RUN)

    assert verified.head == delivery["head"]
    assert verified.population["main_sha"] == delivery["base"]


def test_a_product_move_on_main_measures_the_delivery_again(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commit under ``reckon/`` changes what the pair measured, so it is rerun."""
    _record(RUN, head=delivery["head"], main_sha=delivery["base"])
    _move_main_with_a_product_edit(delivery["repo"])
    _land(monkeypatch)
    calls: list[dict] = []

    def measured(**kwargs):
        calls.append(kwargs)
        return _measurement(
            main_sha=kwargs["base_sha"],
            candidate_sha=kwargs["head_sha"],
            head=kwargs["head_sha"],
        )

    monkeypatch.setattr(landing_measure, "measure_run", measured)

    result = queue_landing.land_once()

    assert result["landed"] == [RUN], result
    assert calls, "a product move reused a measurement it invalidated"


def test_a_delivery_that_no_longer_merges_onto_primary_is_not_landed(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale measurement does not carry a head the primary now conflicts with."""
    repo = delivery["repo"]
    delivery["head"] = _deliver_in(
        delivery["worktree"],
        f"docs/plans/{PLAN}.html",
        "<!-- delivered -->\n",
        "docs: a delivered plan edit",
    )
    _register_run(
        RUN,
        repo=repo,
        worktree=delivery["worktree"],
        base=delivery["base"],
        head=delivery["head"],
    )
    _accept(base=delivery["base"], head=delivery["head"])
    _record(RUN, head=delivery["head"], main_sha=delivery["base"])
    # The same line moves on both sides, so the merge has a real conflict.
    plan = repo / "docs" / "plans" / f"{PLAN}.html"
    plan.write_text(plan.read_text(encoding="utf-8") + "<!-- primary -->\n")
    _git(repo, "add", "docs")
    _git(repo, "commit", "-q", "-m", "docs: the same plan edit on main")
    landed = _land(monkeypatch)

    result = queue_landing.land_once()

    assert result["landed"] == [], result
    assert landed == []
    guard = result["obligations"][0]["guard"]
    assert "conflict" in guard.lower(), guard
    with pytest.raises(CrewError, match="no longer merges cleanly"):
        queue_landing._verified_measurement(RUN)


def test_a_docs_only_move_during_a_measurement_does_not_defer(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Main moving under the measurement no longer sends the run round again."""
    landed = _land(monkeypatch)
    moved: dict[str, str] = {}

    def measured(**kwargs):
        moved["primary"] = _move_main_with_a_plan_edit(delivery["repo"])
        return _measurement(
            main_sha=kwargs["base_sha"],
            candidate_sha=kwargs["head_sha"],
            head=kwargs["head_sha"],
        )

    monkeypatch.setattr(landing_measure, "measure_run", measured)

    result = queue_landing.land_once()

    assert moved["primary"] != delivery["base"], "the fixture did not move main"
    assert result["landed"] == [RUN], result
    assert landed == [RUN]
    assert not result.get("deferred"), result


def test_the_pass_records_a_reason_beside_each_deferred_id(
    delivery, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deferred run's reason sits in a sibling key; the id list is unchanged."""
    _register_run(
        OTHER,
        repo=delivery["repo"],
        worktree=delivery["worktree"],
        base=delivery["base"],
        head=delivery["head"],
    )
    _accept(base=delivery["base"], head=delivery["head"], run_id=OTHER)
    _land(monkeypatch)

    def measured(**kwargs):
        return _measurement(
            main_sha=kwargs["base_sha"],
            candidate_sha=kwargs["head_sha"],
            head=kwargs["head_sha"],
        )

    monkeypatch.setattr(landing_measure, "measure_run", measured)

    result = queue_landing.land_once()

    assert len(result["landed"]) == 1, result
    deferred = result["deferred"]
    assert deferred and all(isinstance(run_id, str) for run_id in deferred), result
    reasons = result["deferred_reasons"]
    assert set(reasons) == set(deferred), result
    assert all(reasons[run_id].strip() for run_id in deferred), reasons
