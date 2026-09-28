"""Promotion sizes each run's review from the tier the run changed its way into.

A run that changes no runtime source — tests, plans, evidence, research data,
figures — promotes with the review gate standing down and its tier recorded on
the ledger row as the reason no review exists. A run that changes runtime source
owes a review unless one is already stored at the promoted head, and the tier it
resolves to (light for a small diff at a fixing spec level, full otherwise or
under a declared elevated risk) is what the row records once it lands.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import _plan_html, crew, ledger
from reckon.crew import review as review_module
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "proj"
PLAN = "plan-a"

_SMALL_SOURCE = "def answer():\n    return 42\n"
_LARGE_SOURCE = "".join(f"VALUE_{index} = {index}\n" for index in range(80))
# Thirty changed lines: above the small arm's two and below the large arm's eighty,
# so a ceiling placed on either side of it moves the tier this run resolves to.
_MEDIUM_SOURCE = "".join(
    f"def f_{index}():\n    return {index}\n" for index in range(15)
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


def _write_plan(
    root: Path, *, risk: str | None = None, section_risk: str | None = None
) -> None:
    plan = root / "docs" / "plans" / f"{PLAN}.html"
    plan.parent.mkdir(parents=True, exist_ok=True)
    heading = '<h2 id="s2">Section two</h2>' if section_risk is not None else ""
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        f'</head><body><main class="plan-doc">{heading}</main></body></html>\n'
    )
    state: dict = {
        "type": "plan",
        "slug": PLAN,
        "title": "Plan A",
        "status": "active",
        "version": 0,
        "comments": {},
    }
    if risk is not None:
        state["capability"] = {
            "version": "1.0",
            "class": "orchestrator",
            "requirements": {"risk": risk},
        }
    if section_risk is not None:
        state["sections"] = [
            {
                "id": "s2",
                "status": "implementable",
                "links": [],
                "effort_hours": 1.0,
                "attempts": 0,
                "capability": {
                    "version": "1.0",
                    "class": "orchestrator",
                    "requirements": {
                        "risk": section_risk,
                        "reasoning": "deep",
                        "verification": "strict",
                    },
                },
            }
        ]
    plan.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    _write_plan(root)
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


def _write_flight(repository: Path, body: str) -> None:
    """Write the synthesised project's own flight layer."""
    path = repository / "docs" / "state" / PROJECT / "flight.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _commit_change(repository: Path, relative_path: str, content: str) -> str:
    path = repository / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git(repository, "add", relative_path)
    _git(repository, "commit", "-q", "-m", "feature: change a path")
    return _git(repository, "rev-parse", "HEAD")


def _write_pointer(
    repository: Path,
    tmp_path: Path,
    run_id: str,
    *,
    commit: str,
    changed_paths: tuple[str, ...],
    role: str = "implement",
    spec_level: str = "exact",
    declared_paths: str | None = None,
    write_paths: tuple[str, ...] | None = None,
) -> None:
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    declared = (
        declared_paths if declared_paths is not None else ", ".join(changed_paths)
    )
    manifest.write_text(
        "node: node-a\n"
        "status: complete\n"
        f"commits: {commit}\n"
        f"changed_paths: {declared}\n"
        "tests: focused check passed\n",
        encoding="utf-8",
    )
    node: dict = {
        "id": "node-a",
        "plan": PLAN,
        "section": "s2",
        "time_budget": "25m",
        "write_paths": list(write_paths if write_paths is not None else changed_paths),
    }
    if spec_level:
        node["spec_level"] = spec_level
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "launch": "in-harness",
            "role": role,
            "backend": "native",
            "created_at": "2026-09-21T06:00:00Z",
            "manifest_path": str(manifest),
            "node": node,
        },
    )


def _store_review(run_id: str, *, head: str) -> None:
    """Store a review that names the revision it read.

    The head is named rather than left for the reader to reconstruct from the
    tree, because a promotion of an earlier run in the same repository commits
    the ledger and moves the tree's head; a record left to reconstruction would
    then describe the ledger commit instead of the revision under test.
    """
    emitted = "\n".join(
        f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
    )
    review = review_module.parse_review(emitted)
    review.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": f"review-of-{run_id}",
            review_module.REVIEWED_HEAD_KEY: head,
        }
    )
    review_module.store_review(review)


def _row(repository: Path, run_id: str) -> dict:
    return next(
        row
        for row in ledger.load(PROJECT, repository)[0]["runs"]
        if row["run_id"] == run_id
    )


def test_tests_only_run_promotes_unreviewed_with_tier_none(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260928T140000000000-tests-only"
    commit = _commit_change(
        repository, "tests/test_thing.py", "def test_it():\n    pass\n"
    )
    _write_pointer(
        repository,
        tmp_path,
        run_id,
        commit=commit,
        changed_paths=("tests/test_thing.py",),
        role="implement",
    )

    crew.complete(run_id, gate="passed", commits=[commit], root=repository)

    row = _row(repository, run_id)
    assert row["review_tier"] == "none"
    assert not row.get("review")


def test_small_runtime_source_run_is_light_and_refused_without_review(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260928T140100000000-light"
    commit = _commit_change(repository, "reckon/thing.py", _SMALL_SOURCE)
    _write_pointer(
        repository,
        tmp_path,
        run_id,
        commit=commit,
        changed_paths=("reckon/thing.py",),
        spec_level="exact",
    )

    with pytest.raises(crew.CrewError):
        crew.complete(run_id, gate="passed", commits=[commit], root=repository)

    _store_review(run_id, head=commit)
    crew.complete(run_id, gate="passed", commits=[commit], root=repository)
    assert _row(repository, run_id)["review_tier"] == "light"


def test_large_runtime_source_run_is_full_and_refused_without_review(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260928T140200000000-full"
    commit = _commit_change(repository, "reckon/big.py", _LARGE_SOURCE)
    _write_pointer(
        repository,
        tmp_path,
        run_id,
        commit=commit,
        changed_paths=("reckon/big.py",),
        spec_level="exact",
    )

    with pytest.raises(crew.CrewError):
        crew.complete(run_id, gate="passed", commits=[commit], root=repository)

    _store_review(run_id, head=commit)
    crew.complete(run_id, gate="passed", commits=[commit], root=repository)
    assert _row(repository, run_id)["review_tier"] == "full"


def test_elevated_plan_risk_makes_a_small_source_run_full(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260928T140300000000-elevated"
    _write_plan(repository, risk="elevated")
    _git(repository, "add", f"docs/plans/{PLAN}.html")
    _git(repository, "commit", "-q", "-m", "docs: declare elevated risk")
    commit = _commit_change(repository, "reckon/thing.py", _SMALL_SOURCE)
    _write_pointer(
        repository,
        tmp_path,
        run_id,
        commit=commit,
        changed_paths=("reckon/thing.py",),
        spec_level="exact",
    )

    with pytest.raises(crew.CrewError):
        crew.complete(run_id, gate="passed", commits=[commit], root=repository)

    _store_review(run_id, head=commit)
    crew.complete(run_id, gate="passed", commits=[commit], root=repository)
    assert _row(repository, run_id)["review_tier"] == "full"


def test_open_spec_level_forces_a_full_review_on_a_small_diff(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260928T140400000000-open"
    commit = _commit_change(repository, "reckon/thing.py", _SMALL_SOURCE)
    _write_pointer(
        repository,
        tmp_path,
        run_id,
        commit=commit,
        changed_paths=("reckon/thing.py",),
        spec_level="open",
    )

    with pytest.raises(crew.CrewError):
        crew.complete(run_id, gate="passed", commits=[commit], root=repository)

    _store_review(run_id, head=commit)
    crew.complete(run_id, gate="passed", commits=[commit], root=repository)
    assert _row(repository, run_id)["review_tier"] == "full"


def test_a_run_declaring_no_scope_is_refused_rather_than_read_as_none(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-20260928T140500000000-silent"
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: node-a\nstatus: complete\ncommits: []\nchanged_paths: []\n",
        encoding="utf-8",
    )
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "launch": "in-harness",
            "role": "implement",
            "backend": "native",
            "created_at": "2026-09-21T06:00:00Z",
            "manifest_path": str(manifest),
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "s2",
                "time_budget": "25m",
            },
        },
    )

    with pytest.raises(crew.CrewError):
        crew.complete(run_id, gate="passed", root=repository)


def test_a_section_declaring_elevated_risk_forces_full_without_plan_risk(
    repository: Path, tmp_path: Path
) -> None:
    """The run's review is sized to the risk of the section it landed against.

    The plan declares no risk of its own, so the fuller tier can only come from
    the typed record on the section the run's node names.
    """
    run_id = "r-20260928T140600000000-section-elevated"
    _write_plan(repository, section_risk="elevated")
    _git(repository, "add", f"docs/plans/{PLAN}.html")
    _git(repository, "commit", "-q", "-m", "docs: declare elevated section risk")
    commit = _commit_change(repository, "reckon/thing.py", _SMALL_SOURCE)
    _write_pointer(
        repository,
        tmp_path,
        run_id,
        commit=commit,
        changed_paths=("reckon/thing.py",),
        spec_level="exact",
    )

    with pytest.raises(crew.CrewError):
        crew.complete(run_id, gate="passed", commits=[commit], root=repository)

    _store_review(run_id, head=commit)
    crew.complete(run_id, gate="passed", commits=[commit], root=repository)
    assert _row(repository, run_id)["review_tier"] == "full"


def test_the_light_ceiling_follows_the_project_flight_layer(
    repository: Path, tmp_path: Path
) -> None:
    """The light ceiling is the resolved flight value, not a literal.

    A thirty-line run is light under a ceiling of forty and full under one of
    twenty, and the only thing that changed between the two is the project's
    own ``review.tiers.light_changed_lines``.
    """
    commit = _commit_change(repository, "reckon/medium.py", _MEDIUM_SOURCE)

    _write_flight(
        repository,
        "review:\n  tiers:\n    light_changed_lines: 40\n    light_time_budget: 10m\n",
    )
    light_run = "r-20260928T140700000000-ceiling-forty"
    _write_pointer(
        repository,
        tmp_path,
        light_run,
        commit=commit,
        changed_paths=("reckon/medium.py",),
        spec_level="exact",
    )
    with pytest.raises(crew.CrewError):
        crew.complete(light_run, gate="passed", commits=[commit], root=repository)
    _store_review(light_run, head=commit)
    crew.complete(light_run, gate="passed", commits=[commit], root=repository)
    assert _row(repository, light_run)["review_tier"] == "light"

    _write_flight(
        repository,
        "review:\n  tiers:\n    light_changed_lines: 20\n    light_time_budget: 10m\n",
    )
    full_run = "r-20260928T140800000000-ceiling-twenty"
    _write_pointer(
        repository,
        tmp_path,
        full_run,
        commit=commit,
        changed_paths=("reckon/medium.py",),
        spec_level="exact",
    )
    with pytest.raises(crew.CrewError):
        crew.complete(full_run, gate="passed", commits=[commit], root=repository)
    _store_review(full_run, head=commit)
    crew.complete(full_run, gate="passed", commits=[commit], root=repository)
    assert _row(repository, full_run)["review_tier"] == "full"


def test_an_unreadable_light_ceiling_falls_back_to_the_shipped_one(
    repository: Path, tmp_path: Path
) -> None:
    """A flight layer that will not resolve leaves the shipped ceiling in force.

    The key's declared shape is an integer; a string is refused by the schema, so
    the ceiling read falls back to the shipped fifty and a two-line source run is
    still light rather than failing the promotion over a lookup.
    """
    _write_flight(
        repository,
        "review:\n  tiers:\n    light_changed_lines: many\n    light_time_budget: 10m\n",
    )
    run_id = "r-20260928T140900000000-unreadable-ceiling"
    commit = _commit_change(repository, "reckon/thing.py", _SMALL_SOURCE)
    _write_pointer(
        repository,
        tmp_path,
        run_id,
        commit=commit,
        changed_paths=("reckon/thing.py",),
        spec_level="exact",
    )

    with pytest.raises(crew.CrewError):
        crew.complete(run_id, gate="passed", commits=[commit], root=repository)

    _store_review(run_id, head=commit)
    crew.complete(run_id, gate="passed", commits=[commit], root=repository)
    assert _row(repository, run_id)["review_tier"] == "light"


def test_a_silent_non_implement_run_changing_runtime_source_is_refused(
    repository: Path, tmp_path: Path
) -> None:
    """A silent manifest does not exempt a run that changed runtime source.

    The manifest declares no changed path and the role neither implement nor a
    reviewer, so a gate that read the role name and the declared paths alone
    promoted the run: ``documentation`` is not implement, and an empty declared
    scope is not a tracked path. The cited commit is what carries the change,
    and it is runtime source, so the tier gate refuses it until a review is
    stored -- a case the role gate's own expression promotes and the tier gate
    refuses, so it reddens when the tier check is reverted.
    """
    run_id = "r-20260928T141000000000-silent-source"
    commit = _commit_change(repository, "reckon/silent.py", _SMALL_SOURCE)
    _write_pointer(
        repository,
        tmp_path,
        run_id,
        commit=commit,
        changed_paths=(),
        declared_paths="[]",
        write_paths=("reckon/silent.py",),
        role="documentation",
        spec_level="exact",
    )

    with pytest.raises(crew.CrewError):
        crew.complete(run_id, gate="passed", commits=[commit], root=repository)

    _store_review(run_id, head=commit)
    crew.complete(run_id, gate="passed", commits=[commit], root=repository)
    assert _row(repository, run_id)["review_tier"] == "light"
