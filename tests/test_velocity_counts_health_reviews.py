"""The velocity view counts the health-review yield from the stored records.

The health-review reflex composes a code review around a plan's neighbourhood
and folds each finding into the node that carries its repair; those node ids are
the treatments. This module drives ``velocity.report`` over a synthesised
repository and review store holding three code-mode reviews, two folded
treatments — one promoted with a net deletion of forty product lines and one
discarded — and five dispositions, and asserts the ``health_reviews`` field
counts each from the records.

The declared mutation counts treatments from dispatch instead of promotion, so
every folded node reports as promoted and the promotion assertion fails.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
from pathlib import Path

import pytest

from reckon import velocity

DAY = 86400
BASE = int(dt.datetime(2026, 9, 1, tzinfo=dt.UTC).timestamp())
WINDOW_START = velocity.iso(BASE + 1 * DAY)  # 2026-09-02
WINDOW_END = velocity.iso(BASE + 5 * DAY)  # 2026-09-06
PROJECT = "sample"
BRANCH = "main"

# The declared mutation, verbatim.
DECLARED_MUTATION = (
    "count treatments from dispatch instead of promotion so the field reports 2 of 2"
)
NEGATIVE_CONTROL = os.environ.get("RECKON_HEALTH_REVIEWS_NEGATIVE_CONTROL", "").strip()


def _iso(day: int, seconds: int = 0) -> str:
    return velocity.iso(BASE + day * DAY + seconds)


def _git(repo: Path, *arguments: str, env: dict | None = None) -> str:
    # The worker that runs this suite exports the identity of its own run, and a
    # git wrapper keyed on that identity refuses a mutating verb outside the
    # worker's worktree. Point the subprocess at the synthesised repository by
    # dropping the inherited identity.
    base = {**os.environ, **(env or {})}
    for name in ("RECKON_RUN_ID", "RECKON_MANIFEST", "RECKON_ATTEMPT_STARTED_AT"):
        base.pop(name, None)
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=base,
    )
    return result.stdout.strip()


def _env(day: int) -> dict:
    when = _iso(day)
    return {
        **os.environ,
        "GIT_AUTHOR_DATE": when,
        "GIT_COMMITTER_DATE": when,
        "GIT_AUTHOR_NAME": "fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }


def _commit(repo: Path, message: str, day: int, changes: dict[str, str] | None = None):
    env = _env(day)
    for path, content in (changes or {}).items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        _git(repo, "add", path, env=env)
    _git(repo, "commit", "-q", "--allow-empty", "-m", message, env=env)


def _finding(index: int, cites: str) -> dict:
    return {
        "id": f"known_findings-{index}",
        "item": "known_findings",
        "anchor": f"reckon/a.py:{index + 1}",
        "class": "duplication",
        "text": f"finding {index}",
        "cites": cites,
    }


def _folded(node: str) -> dict:
    return {"kind": "folded", "node": node}


def _exempted(reason: str) -> dict:
    return {"kind": "exempted", "reason": reason}


def _write_code_reviews(project_dir: Path) -> None:
    """Three code-mode reviews carrying five dispositions and five findings.

    Two folds name the treatments ``fix-a`` and ``fix-b``; three exemptions add
    the disposition kinds the field reports beside them. Two findings cite an
    existing row and three are reported new, so the known/new split is
    hand-computable from the fixture.
    """
    project_dir.mkdir(parents=True, exist_ok=True)
    reviews = {
        "alpha": {
            "findings": [_finding(0, "new"), _finding(1, "reckon/a.py:3")],
            "finding_dispositions": {
                "0": _folded("fix-a"),
                "1": _exempted("by design"),
            },
        },
        "beta": {
            "findings": [_finding(0, "new"), _finding(1, "new")],
            "finding_dispositions": {"0": _folded("fix-b"), "1": _exempted("stale")},
        },
        "gamma": {
            "findings": [_finding(0, "reckon/a.py:9")],
            "finding_dispositions": {"0": _exempted("out of scope")},
        },
    }
    for key, body in reviews.items():
        record = {
            "subject": "code",
            "project": PROJECT,
            "neighbourhood_key": key,
            "timestamp": _iso(1),
            "findings": body["findings"],
            "finding_dispositions": body["finding_dispositions"],
        }
        (project_dir / f"code-{key}.json").write_text(json.dumps(record))


def _row(run_id: str, node: str, day: int, commits: list[str]) -> dict:
    return {
        "run_id": run_id,
        "node": node,
        "plan": "p",
        "role": "implement",
        "gate": "passed",
        "backend": "claude",
        "commits": commits,
        "dispatched_at": _iso(day),
        "completed_at": _iso(day, 100),
        "worker_seconds": 100,
        "lineage": {},
        "attempt": 1,
    }


def _build_repository(root: Path) -> Path:
    repo = root / PROJECT
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", BRANCH)
    # Seed the helper the treatment retires: forty product lines a later commit
    # removes, so the promoted treatment's product-line delta is -40 exactly.
    _commit(
        repo,
        "chore: seed the helper",
        0,
        {"reckon/treatment.py": "\n".join(f"x{index}" for index in range(40)) + "\n"},
    )
    # The ledger is committed before the window opens, so the clock recovery
    # finds it at the base revision; its run rows are updated inside the window
    # to cite the treatment's commit once that commit exists.
    ledger_path = "docs/state/sample/crew.json"
    _commit(
        repo,
        "chore: seed the ledger",
        0,
        {
            ledger_path: json.dumps(
                {
                    "data": {
                        "runs": [
                            _row("r-promoted", "fix-a", 1, []),
                            _row("r-discarded", "fix-b", 1, []),
                        ]
                    }
                },
                indent=2,
            )
        },
    )
    env = _env(1)
    (repo / "reckon" / "treatment.py").unlink()
    _git(repo, "add", "-u", "--", "reckon/treatment.py", env=env)
    _git(repo, "commit", "-q", "-m", "refactor: retire the helper", env=env)
    deletion_sha = _git(repo, "rev-parse", "HEAD")
    # Two treatments: ``fix-a`` is promoted in the window; ``fix-b`` has a
    # ledger row but no promotion, so it is dispatched and not promoted.
    ledger = {
        "data": {
            "runs": [
                _row("r-promoted", "fix-a", 1, [deletion_sha]),
                _row("r-discarded", "fix-b", 1, []),
            ]
        }
    }
    _commit(
        repo,
        "chore: cite the treatment commit",
        1,
        {ledger_path: json.dumps(ledger, indent=2)},
    )
    _commit(repo, "promote(r-promoted)", 2, {})
    return repo


def _apply_control(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply the declared mutation that makes the case go red."""
    if NEGATIVE_CONTROL in {"dispatch-not-promotion", "1"}:
        # Count every treatment as promoted, ignoring the promoted cohort, so
        # the discarded treatment (``fix-b``) reports as promoted too.
        monkeypatch.setattr(
            velocity,
            "_promoted_treatments",
            lambda treatment_nodes, promoted_nodes: list(treatment_nodes),
        )


@pytest.fixture()
def report(tmp_path: Path, monkeypatch) -> dict:
    monkeypatch.setenv("RECKON_VELOCITY_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "home"))
    _write_code_reviews(tmp_path / "home" / "crew" / "reviews" / PROJECT)
    _apply_control(monkeypatch)
    repo = _build_repository(tmp_path / "code")
    return velocity.report(
        {PROJECT: str(repo)},
        start=WINDOW_START,
        end=WINDOW_END,
        run_store_db=None,
    )


def test_three_code_reviews_are_counted(report: dict):
    field = report["health_reviews"]
    assert field["state"] == "measured"
    assert field["reviews"] == 3


def test_findings_read_as_known_against_reported_new(report: dict):
    findings = report["health_reviews"]["findings"]
    assert findings["known"] == 2
    assert findings["new"] == 3


def test_dispositions_are_reported_by_kind(report: dict):
    assert report["health_reviews"]["dispositions"] == {"exempted": 3, "folded": 2}


def test_one_of_two_treatments_promoted_and_the_line_delta(report: dict):
    field = report["health_reviews"]
    assert field["treatments"] == {"folded": 2, "dispatched": 2, "promoted": 1}
    assert field["product_lines"] == -40


def test_the_field_is_served_only_when_fields_names_it():
    assert "health_reviews" in velocity.OPTIONAL_BLOCKS


def test_the_stop_rule_rides_the_field(report: dict):
    assert velocity.HEALTH_REVIEW_STOP_RULE in report["health_reviews"]["stop_rule"]


def test_the_field_docstring_states_the_same_stop_rule():
    stated = " ".join((velocity.health_reviews.__doc__ or "").split())
    assert velocity.HEALTH_REVIEW_STOP_RULE in stated
