"""A pick serves a published run-time profile and never rebuilds one.

Building a project's run-time profile decodes its whole ledger. Doing that
inside a pick made the pick's cost grow with the number of live local projects
and pushed picks past their five-second dispatch bound. So the rebuild moves to
the one moment a project's ledger content changes -- its own promotion. A pick
serves the last profile a promotion published, with its age, or serves none.
"""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from reckon import capabilities
from reckon.crew.picker import lane_context

NOW = datetime(2026, 10, 3, 4, 0, tzinfo=UTC)


def _groups(runs: int = 3, median: float = 420.0) -> list[dict]:
    return [
        {
            "backend": "clive",
            "effort": "standard",
            "role": "implement",
            "spec_level": "guided",
            "runs": runs,
            "wall_seconds_median": median,
        }
    ]


def _live_local_row(project: str) -> dict:
    return {
        "project": project,
        "backend": "clive",
        "role": "implement",
        "spec_level": "guided",
        "phase": "working",
        "agent": {"local": True, "effort": "standard"},
        "node": {"role": "implement", "spec_level": "guided"},
    }


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """A temporary crew home and profile cache, never the live ones."""

    home = tmp_path / "crew-home"
    profile_cache = tmp_path / "profile-cache"
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_RUN_TIME_PROFILE_CACHE", str(profile_cache))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr(lane_context, "_ledger_stamp", lambda _p: ["ledger", 1])
    lane_context._PROFILE_CACHE.clear()
    yield profile_cache
    lane_context._PROFILE_CACHE.clear()


@pytest.fixture
def rows():
    """A mutable fixture ledger the profile builder reads."""

    return {"rows": [{"wall_seconds": 420}]}


@pytest.fixture
def built(cache, rows, monkeypatch):
    """A profile builder over ``rows`` that counts its calls."""

    calls: list[str] = []

    def profile(project, **_kwargs):
        calls.append(project)
        return {"groups": _groups(len(rows["rows"]))}

    monkeypatch.setattr(lane_context, "run_time_profile", profile)
    return calls


@pytest.fixture
def refuse_builds(monkeypatch):
    """Make any attempt to rebuild a profile fail loudly."""

    def refuse(project, **_kwargs):
        raise AssertionError(f"the pick rebuilt {project!r}")

    monkeypatch.setattr(lane_context, "run_time_profile", refuse)


def _entry_path(cache, project):
    return cache / f"{project}.json"


def test_a_moved_stamp_serves_the_persisted_profile_and_its_age(
    built, rows, cache, monkeypatch
):
    """A persisted profile survives a ledger move and is served with its age.

    After the refresh persists a profile, a ledger row is appended so the stamp
    moves. The pick must not rebuild: with the loader patched to raise, the read
    still returns the persisted profile and its age, and no error reaches it.
    """
    lane_context.refresh_run_time_profile("alpha", now=NOW)
    assert built == ["alpha"]

    # The ledger moves: a row is appended, so the stamp changes.
    rows["rows"].append({"wall_seconds": 999})

    def refuse(project, **_kwargs):
        raise AssertionError(f"the pick rebuilt {project!r}")

    monkeypatch.setattr(lane_context, "run_time_profile", refuse)
    lane_context._PROFILE_CACHE.clear()
    profile, age = lane_context._cached_run_time_profile("alpha", now=NOW)

    assert profile["groups"][0]["runs"] == 1
    assert age == 0.0


def test_the_refresh_publishes_the_appended_row_and_the_read_is_fresh(
    built, rows, monkeypatch
):
    """The refresh republishes with the new row; the next read is under 60 s.

    After the ledger moved, the refresh rebuilds a profile that includes the
    appended row and persists it. The next pick serves that profile at an age
    under the bound, so a promotion is what carries the new contents to a pick.
    """
    lane_context.refresh_run_time_profile("alpha", now=NOW)
    rows["rows"].append({"wall_seconds": 999})
    # The append moved the ledger's stamp, so the refresh has to rebuild.
    monkeypatch.setattr(lane_context, "_ledger_stamp", lambda _p: ["ledger", 2])

    later = NOW + timedelta(seconds=30)
    lane_context.refresh_run_time_profile("alpha", now=later)
    assert built == ["alpha", "alpha"]

    profile, age = lane_context._cached_run_time_profile("alpha", now=later)
    assert profile["groups"][0]["runs"] == 2
    assert age is not None and age < 60


def test_a_profile_older_than_the_bound_is_not_served(built):
    """A persisted profile 25 hours old is refused; the pick serves none."""

    lane_context.refresh_run_time_profile("alpha", now=NOW)
    later = NOW + timedelta(hours=25)
    profile, age = lane_context._cached_run_time_profile("alpha", now=later)
    assert profile == {}
    assert age is None


def test_a_value_without_persisted_at_is_not_served(cache, monkeypatch):
    """A value with no ``persisted_at`` reads as older than the bound."""

    path = _entry_path(cache, "alpha")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": capabilities.PICK_INPUT_CACHE_VERSION,
                "key": "whatever",
                "value": {"groups": _groups()},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        lane_context,
        "run_time_profile",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("rebuilt")),
    )
    profile, age = lane_context._cached_run_time_profile("alpha", now=NOW)
    assert profile == {}
    assert age is None


def test_an_entry_under_another_cache_version_is_not_served(cache, monkeypatch):
    """A value written under another cache version reads as none."""

    path = _entry_path(cache, "alpha")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": capabilities.PICK_INPUT_CACHE_VERSION + 1,
                "key": "whatever",
                "value": {"groups": _groups(), "persisted_at": NOW.isoformat()},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        lane_context, "run_time_profile", lambda *a, **kw: pytest.fail("rebuilt")
    )
    profile, age = lane_context._cached_run_time_profile("alpha", now=NOW)
    assert profile == {}
    assert age is None


def test_no_stamp_and_an_unsafe_id_serve_none_without_building(
    cache, monkeypatch, refuse_builds
):
    """A cold cache, no stamp and an unsafe id each serve none without building."""

    profile, age = lane_context._cached_run_time_profile("alpha", now=NOW)
    assert profile == {} and age is None

    monkeypatch.setattr(lane_context, "_ledger_stamp", lambda _p: None)
    profile, age = lane_context._cached_run_time_profile("alpha", now=NOW)
    assert profile == {} and age is None

    profile, age = lane_context._cached_run_time_profile("../escape", now=NOW)
    assert profile == {} and age is None
    assert not list(cache.rglob("*"))

    assert lane_context.refresh_run_time_profile("../escape", now=NOW) is None


def test_a_not_served_foreign_profile_contributes_no_wall(built, monkeypatch):
    """A foreign owner whose profile is not served contributes no wall time."""

    monkeypatch.setattr(lane_context, "list_live", lambda: [_live_local_row("other")])
    wait, age = lane_context._expected_wait(
        project="proj", records=[], local_backend="clive", now=NOW
    )
    assert wait is None
    assert age is None


def test_a_served_foreign_profile_reports_its_age(built, monkeypatch):
    """A served foreign profile two hours old names its age beside the wait."""

    lane_context.refresh_run_time_profile("other", now=NOW)
    monkeypatch.setattr(lane_context, "list_live", lambda: [_live_local_row("other")])
    later = NOW + timedelta(hours=2)
    lane_context._PROFILE_CACHE.clear()
    wait, age = lane_context._expected_wait(
        project="proj", records=[], local_backend="clive", now=later
    )
    assert wait == 420.0
    assert age == pytest.approx(2 * 3600.0)


def test_local_lane_carries_the_profile_age_and_the_glossary_defines_it(
    built, monkeypatch
):
    """``local_lane`` carries the field and the glossary defines it."""

    monkeypatch.setattr(lane_context, "list_live", lambda: [_live_local_row("other")])
    monkeypatch.setattr(lane_context, "local_lane_load", lambda: {})
    lane_context.refresh_run_time_profile("other", now=NOW)
    lane_context._PROFILE_CACHE.clear()
    block = lane_context.local_lane(
        config={"local_backend": "clive"},
        candidates=[],
        project="proj",
        records=[],
        now=NOW,
    )
    assert block["expected_wait_s"] == 420.0
    assert block["expected_wait_profile_age_s"] == 0.0

    from reckon.crew.picker import prompts

    assert "expected_wait_profile_age_s" in prompts._GLOSSARY


# --- promotion publishes the profile -----------------------------------------

from pathlib import Path  # noqa: E402

from reckon import _plan_html, crew, ledger  # noqa: E402
from reckon.crew.runs import _write_json, pointer_path  # noqa: E402
from tests.conftest import EXECUTABLE_GATE_COMMAND  # noqa: E402
from tests.helpers.git import git as _git  # noqa: E402

PROMOTION_PROJECT = "profile-refresh-fixture"
PROMOTION_PLAN = "profile-refresh-target"


def _write_plan(root: Path) -> None:
    path = root / "docs" / "plans" / f"{PROMOTION_PLAN}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROMOTION_PROJECT}">'
        f"<title>{PROMOTION_PLAN}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    state = {
        "type": "plan",
        "slug": PROMOTION_PLAN,
        "title": "Profile refresh target",
        "status": "active",
        "version": 0,
        "comments": {},
    }
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


@pytest.fixture
def promotion_repo(tmp_path, monkeypatch):
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    _write_plan(root)
    (root / "docs" / "state" / PROMOTION_PROJECT).mkdir(parents=True)
    (root / "candidate.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs", "candidate.txt"),
        ("commit", "-q", "-m", "chore: seed"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROMOTION_PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _promotion_pointer(repo, tmp_path, run_id):
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "elsewhere" / "review.json"
    manifest.write_text(
        "node: profile-refresh\n"
        "status: complete\n"
        f"changed_paths: {outside}\n"
        f"tests: {EXECUTABLE_GATE_COMMAND}\n",
        encoding="utf-8",
    )
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROMOTION_PROJECT,
            "repo": str(repo),
            "worktree": str(repo),
            "base_sha": _git(repo, "rev-parse", "HEAD"),
            "launch": "in-harness",
            "role": "review",
            "backend": "native",
            "created_at": "2026-09-17T12:00:00Z",
            "manifest_path": str(manifest),
            "node": {
                "id": "profile-refresh",
                "plan": PROMOTION_PLAN,
                "section": "s",
                "time_budget": "25m",
                "write_paths": ["candidate.txt"],
            },
        },
    )


def test_promotion_refreshes_the_runs_project_after_the_append(
    promotion_repo, tmp_path, monkeypatch
):
    """Promotion publishes the run's profile once, after the ledger append."""
    order: list[str] = []
    promotion_module = importlib.import_module("reckon.crew.promotion")
    real_append = promotion_module.ledger.append_run

    def append(project, run, **kwargs):
        written = real_append(project, run, **kwargs)
        order.append(f"append:{project}")
        return written

    monkeypatch.setattr(promotion_module.ledger, "append_run", append)

    def refresh(project, **_kwargs):
        order.append(f"refresh:{project}")

    monkeypatch.setattr(lane_context, "refresh_run_time_profile", refresh)

    run_id = "r-20261016T120000000001-profile-refresh"
    _promotion_pointer(promotion_repo, tmp_path, run_id)
    promoted = crew.complete(run_id, gate="passed", root=promotion_repo)

    assert promoted["project"] == PROMOTION_PROJECT
    assert order == [f"append:{PROMOTION_PROJECT}", f"refresh:{PROMOTION_PROJECT}"]


def test_a_failing_refresh_never_fails_the_promotion(
    promotion_repo, tmp_path, monkeypatch
):
    """A refresh that raises leaves promotion's own result unchanged.

    The profile is an operational reading, so a refresh that fails must not
    undo a landed run: the promotion still returns its record and the ledger
    still carries the appended row.
    """

    def refuse(project, **_kwargs):
        raise RuntimeError(f"the refresh of {project!r} failed")

    monkeypatch.setattr(lane_context, "refresh_run_time_profile", refuse)

    run_id = "r-20261016T120000000002-profile-refresh"
    _promotion_pointer(promotion_repo, tmp_path, run_id)
    promoted = crew.complete(run_id, gate="passed", root=promotion_repo)

    assert promoted["project"] == PROMOTION_PROJECT
    carried = ledger.load(PROMOTION_PROJECT, root=promotion_repo)[0]["runs"]
    assert [item["run_id"] for item in carried] == [run_id], (
        "the appended run did not survive the failing refresh"
    )
