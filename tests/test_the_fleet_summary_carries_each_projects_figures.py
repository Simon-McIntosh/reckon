"""The fleet summary carries every field the home view reads.

The summary served at ``/_projects/index.json`` is one payload per project: its
plan counts by status, its progress, its activity series and its active sprint.
This file builds a temporary two-project fleet — one project whose plans author
unequal effort hours and one plan that authors none — and asserts each field,
so the roll-up's imputation is read from the served summary rather than from a
function's return value.

The negative control replaces the roll-up with an unweighted mean: because the
present hours differ, the two figures disagree, and the progress assertion is
what fails.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from reckon import crew as crew_module
from reckon import serve
from reckon._plan_html import derive_fleet_impl_imputing_effort

# Alpha authors unequal hours, so an effort-weighted mean and an unweighted one
# disagree: (1*1 + 0*9 + 0*5) / 15 = 1/15 against (1 + 0 + 0) / 3. The third
# plan authors no hours, so it weighs the mean of the two that do.
_ALPHA_WEIGHTED = 1.0 / 15.0
_ALPHA_UNWEIGHTED = 1.0 / 3.0
_BETA_PROGRESS = 0.5


def _plan_html(
    slug: str,
    *,
    project: str,
    status: str,
    impl: float,
    effort_hours: float | None,
) -> str:
    meta = [
        f'<meta name="docs-project" content="{project}">',
        '<meta name="reckon-type" content="plan">',
        f'<meta name="plan-slug" content="{slug}">',
        f'<meta name="plan-title" content="{slug}">',
        f'<meta name="plan-status" content="{status}">',
        f'<meta name="plan-impl" content="{impl}">',
    ]
    if effort_hours is not None:
        meta.append(f'<meta name="plan-effort-hours" content="{effort_hours}">')
    return (
        "<!doctype html><html><head>"
        + "".join(meta)
        + f"<title>{slug}</title></head><body>"
        '<main class="plan-doc"></main></body></html>'
    )


def _git(repo: Path, *args: str, env: dict | None = None) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def _seed_repo(repo: Path, plans: dict[str, str], committed_at: datetime) -> Path:
    """Write a project's plans and commit them at one backdated stamp."""

    docs = repo / "docs"
    plan_dir = docs / "plans"
    plan_dir.mkdir(parents=True)
    _git(repo, "init", "-q")
    for slug, body in plans.items():
        (plan_dir / f"{slug}.html").write_text(body, encoding="utf-8")
    stamp = committed_at.strftime("%Y-%m-%dT%H:%M:%S")
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": stamp,
        "GIT_COMMITTER_DATE": stamp,
        "GIT_AUTHOR_NAME": "tester",
        "GIT_AUTHOR_EMAIL": "tester@example.com",
        "GIT_COMMITTER_NAME": "tester",
        "GIT_COMMITTER_EMAIL": "tester@example.com",
    }
    _git(repo, "add", "docs/plans", env=env)
    _git(repo, "commit", "-q", "-m", "seed", env=env)
    return docs


@pytest.fixture(name="fleet")
def fleet_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A two-project fleet: alpha imputes hours, beta carries a sprint."""

    # A naive local stamp: git parses it as local and the activity series reads
    # it back in local time, so the day it lands on is the one asserted below
    # rather than one shifted by the host's zone.
    committed = datetime.now() - timedelta(days=3)  # noqa: DTZ005 — naive local, pairs with the git stamp below

    alpha_docs = _seed_repo(
        tmp_path / "alpha",
        {
            "alpha-done": _plan_html(
                "alpha-done",
                project="alpha",
                status="active",
                impl=1.0,
                effort_hours=1.0,
            ),
            "alpha-open": _plan_html(
                "alpha-open",
                project="alpha",
                status="pending",
                impl=0.0,
                effort_hours=9.0,
            ),
            "alpha-uncalibrated": _plan_html(
                "alpha-uncalibrated",
                project="alpha",
                status="pending",
                impl=0.0,
                effort_hours=None,
            ),
        },
        committed,
    )
    beta_docs = _seed_repo(
        tmp_path / "beta",
        {
            "beta-shipped": _plan_html(
                "beta-shipped",
                project="beta",
                status="shipped",
                impl=1.0,
                effort_hours=4.0,
            ),
            "beta-active": _plan_html(
                "beta-active",
                project="beta",
                status="active",
                impl=0.0,
                effort_hours=4.0,
            ),
        },
        committed,
    )

    state_root = tmp_path / "state"
    (state_root / "beta").mkdir(parents=True)
    (state_root / "beta" / "index.json").write_text(
        json.dumps(
            {
                "updated": "2026-03-01T00:00:00",
                "project": "beta",
                "doc": "index",
                "data": {
                    "active_sprint_id": "current",
                    "projects": [],
                    "sprints": [
                        {
                            "id": "current",
                            "theme": "Present work",
                            "status": "active",
                            "items": [],
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    mounts = tmp_path / "mounts.json"
    mounts.write_text(
        json.dumps({"alpha": str(alpha_docs), "beta": str(beta_docs)}),
        encoding="utf-8",
    )
    monkeypatch.setattr(serve, "_MOUNTS_FILE", mounts)
    monkeypatch.setattr(serve, "_STATE_ROOT", state_root)
    monkeypatch.setattr(crew_module, "list_live", lambda **_kwargs: [])
    serve._DISC_CACHE.clear()
    return {"mounts": mounts, "state_root": state_root, "commit_day": committed.date()}


def _activity_index(commit_day) -> int:
    """The 30-day series slot a commit on ``commit_day`` lands in."""

    window_start = datetime.now().date() - timedelta(days=29)  # noqa: DTZ005 — the series the row derives is local
    return (commit_day - window_start).days


def _served_summary() -> dict[str, dict]:
    mounted = serve.load_mounts()
    payload = serve.collect_projects(mounted)
    rows = {}
    for entry in payload["projects"]:
        for row in entry["data"]["projects"]:
            rows[row["project"]] = row
    return rows


def test_each_project_row_carries_counts_progress_activity_and_sprint(fleet) -> None:
    rows = _served_summary()

    alpha = rows["alpha"]
    # Counts by status, excluding nothing this fixture archives.
    assert alpha["plans_count"] == 3
    assert (alpha["active"], alpha["blocked"], alpha["pending"], alpha["shipped"]) == (
        1,
        0,
        2,
        0,
    )
    # The roll-up: effort-weighted, with the plan that authors no hours weighing
    # the mean of the hours the others state. Unweighted it would read 1/3.
    assert alpha["progress"] == pytest.approx(_ALPHA_WEIGHTED, abs=1e-9)
    # Activity: the seeding commit lands in the 30-day series, at the slot its
    # own date derives rather than at a literal index.
    assert len(alpha["activity30"]) == 30
    assert alpha["activity30"][_activity_index(fleet["commit_day"])] == 1
    assert sum(alpha["activity30"]) == 1
    # No sprint state for alpha, so the field is null rather than absent.
    assert alpha["active_sprint"] is None

    beta = rows["beta"]
    assert beta["plans_count"] == 2
    assert (beta["active"], beta["blocked"], beta["pending"], beta["shipped"]) == (
        1,
        0,
        0,
        1,
    )
    assert beta["progress"] == pytest.approx(_BETA_PROGRESS, abs=1e-9)
    assert len(beta["activity30"]) == 30
    assert beta["activity30"][_activity_index(fleet["commit_day"])] == 1
    assert beta["active_sprint"] == {"id": "current", "theme": "Present work"}


def test_the_roll_up_imputes_a_missing_plan_with_the_mean_of_the_hours_present() -> (
    None
):
    rows = [
        {"slug": "a", "impl": 1.0, "effort_hours": 1.0},
        {"slug": "b", "impl": 0.0, "effort_hours": 9.0},
        {"slug": "c", "impl": 0.0, "effort_hours": None},
    ]

    # Present hours average 5, so the plan stating none weighs 5; the weighted
    # figure is 1/15 where a plain mean over the three would be 1/3.
    assert derive_fleet_impl_imputing_effort(rows) == pytest.approx(
        _ALPHA_WEIGHTED, abs=1e-9
    )
    # A fleet authoring no hours anywhere weighs every plan the same.
    assert derive_fleet_impl_imputing_effort(
        [{"slug": "x", "impl": 1.0}, {"slug": "y", "impl": 0.0}]
    ) == pytest.approx(0.5)


def test_the_negative_control_replaces_the_roll_up_with_an_unweighted_mean(
    fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shipped assertion fails once the roll-up drops its weighting."""

    def unweighted(plans) -> float | None:
        figures = [float(plan.get("impl") or 0.0) for plan in plans]
        return sum(figures) / len(figures) if figures else None

    from reckon import fleet_index

    monkeypatch.setattr(fleet_index, "derive_fleet_impl_imputing_effort", unweighted)

    alpha = _served_summary()["alpha"]

    assert alpha["progress"] == pytest.approx(_ALPHA_UNWEIGHTED, abs=1e-9)
    with pytest.raises(AssertionError):
        assert alpha["progress"] == pytest.approx(_ALPHA_WEIGHTED, abs=1e-9)
