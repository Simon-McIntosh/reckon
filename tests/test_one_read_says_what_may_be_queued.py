"""One read says, section by section, whether a plan's work may be queued.

Three callers need that answer — the whole-plan dispatch, a single-section
dispatch and the admission service — and each piece of it lives in a different
place: the plan-review gate, the roadmap's open decisions, the coordinator
resolution and a stored request's queue hold. The cases below hold the read to
each of those, and hold the two dispatch surfaces to the read's own words, so a
coordinator reading a dry run and a service reading the queue see one
vocabulary rather than three derivations that can drift apart.
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import dispatch_batch, plan_review, recovery, review, runs
from reckon.crew.runs import pointer_path

PROJECT = "proj"
SESSION = "coordinator-a"
OTHER_SESSION = "coordinator-b"

CONFIG = {
    "default_backend": "clive",
    "local_backend": "clive",
    "backends": {
        "clive": {
            "launch": "cli",
            "command": "codex",
            "model": "local-model",
            "effort": "medium",
            "sandbox": "worktree-full",
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    # The read answers a review question only where the gate holds work; an
    # enforcing config is the case that matters, so every case runs on one.
    "plan_review_gate": "enforce",
}

SECTIONS = ("s1", "s2", "s3")


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "--no-pager", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
    )


def _plan_html_text(slug: str, sections=SECTIONS, decisions: str = "") -> str:
    quote = '"'
    declarations = json.dumps(dict.fromkeys(sections, "implementable")).replace(
        quote, "&quot;"
    )
    body = "".join(
        f'<h2 id="{name}">Section {name}</h2>'
        f"<p>Build {name}.</p>"
        f"<p><strong>Done when</strong> the {name} check passes.</p>"
        for name in sections
    )
    return (
        "<!doctype html>\n"
        f'<meta name="docs-project" content="{PROJECT}">\n'
        '<meta name="reckon-type" content="plan">\n'
        f'<meta name="plan-slug" content="{slug}">\n'
        '<meta name="plan-status" content="active">\n'
        '<meta name="plan-version" content="3">\n'
        f'<meta name="plan-section-declarations" content="{declarations}">\n'
        f"{decisions}\n{body}\n"
    )


def _decision_block() -> str:
    """One open decision that names no section, so it holds the whole plan."""
    return (
        '<section data-reckon="decisions">'
        '<div class="r-dec" data-key="storage" data-choice="">'
        '<span class="r-dec-q">Where does the cache live?</span>'
        "</div></section>"
    )


def _seed_checkout(root: Path, plans: dict[str, str] | None = None) -> Path:
    """A checkout the dispatch gates accept, with the plans it is handed."""
    scripts = root / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).parents[1] / "skills" / "reckon-build" / "scripts"
    (scripts / "worktree_fleet.py").write_text(
        (source / "worktree_fleet.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    plans_dir = root / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "docs" / "state" / PROJECT / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n",
        encoding="utf-8",
    )
    for slug, text in (plans or {}).items():
        (plans_dir / f"{slug}.html").write_text(text, encoding="utf-8")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "worker@example.invalid")
    _git(root, "config", "user.name", "Worker")
    _git(root, "add", "--", "docs", "skills")
    _git(
        root,
        "commit",
        "-q",
        "-m",
        "chore: seed checkout",
        "-m",
        "Seed the checkout the dispatch gates read.",
    )
    return root


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_VELOCITY_CACHE", str(config_home / "cache" / "velocity"))
    root = _seed_checkout(
        tmp_path / "checkout",
        {
            "plan-a": _plan_html_text("plan-a"),
            "plan-b": _plan_html_text("plan-b", decisions=_decision_block()),
            "plan-c": _plan_html_text("plan-c"),
        },
    )
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


@pytest.fixture()
def many_plans(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project of three hundred plans, each with three implementable sections."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_VELOCITY_CACHE", str(config_home / "cache" / "velocity"))
    root = _seed_checkout(
        tmp_path / "checkout",
        {
            f"plan-{index:03d}": _plan_html_text(f"plan-{index:03d}")
            for index in range(300)
        },
    )
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _config_for(_project: str) -> dict:
    return json.loads(json.dumps(CONFIG))


def _read(repository: Path, plan: str, session: str = SESSION) -> dict:
    return dispatch_batch.plan_queue_answers(
        PROJECT,
        plan,
        session=session,
        repo_root=repository,
        config=_config_for(PROJECT),
    )


def _whole_plan(repository: Path, plan: str, session: str = SESSION) -> dict:
    return dispatch_batch.queue_plan(
        PROJECT,
        plan,
        session=session,
        config_for=_config_for,
        repo_root=repository,
    )


def _one_section(repository: Path, plan: str, section: str) -> dict:
    result = dispatch_batch.queue_batch(
        [
            {
                "project": PROJECT,
                "plan": plan,
                "section": section,
                "node": f"one-read-{plan}-{section}",
                "goal": f"build {section}",
                "spec_level": "exact",
                "time_budget": "20m",
            }
        ],
        session=SESSION,
        config_for=_config_for,
        repo_root=repository,
    )
    return result["results"][0]


# --- the plan-review fixture -------------------------------------------------


def _report(items) -> str:
    return "".join(f"RUBRIC {item}: pass\n" for item in items)


def _line_of(path: Path, marker: str) -> int:
    """The 1-based line the marker's sentence is on, in the plan as it stands."""
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if marker in line:
            return number
    raise AssertionError(f"{marker!r} is not in the plan")


def _store(
    path: Path, *, rubric: str, run_id: str, findings, version: int | None = None
) -> dict:
    """Store one complete review of the plan as it stands now."""
    items = (
        review.PLAN_DESIGN_REVIEW_ITEMS
        if rubric == "design"
        else review.PLAN_REVIEW_ITEMS
    )
    subject = {
        "subject": "plan",
        "project": PROJECT,
        "plan_slug": path.stem,
        "plan_path": str(path),
        "repo": str(path.parents[2]),
        "session": SESSION,
        "rubric": rubric,
        "local": True,
        "run_id": run_id,
    }
    fields = recovery._review_dispatch_fields(subject, rubric=rubric)
    sidecar = json.loads(Path(fields["sidecar"]).read_text(encoding="utf-8"))
    if version is not None:
        sidecar["plan_version"] = version
    Path(sidecar["report_path"]).write_text(_report(items), encoding="utf-8")
    record = {
        **sidecar,
        "findings": list(findings),
        "responses": {},
        "review_run_id": run_id,
    }
    plan_review.store_plan_review(record)
    return record


def _review_with_one_finding(path: Path) -> None:
    """A complete design review and a content review whose §s1 finding is open."""
    _store(path, rubric="design", run_id="review-design", findings=[])
    _store(
        path,
        rubric="content",
        run_id="review-content",
        version=4,
        findings=[
            {
                "id": "reasoning-1",
                "type": "reasoning",
                "anchor": f"docs/plans/{path.name}:{_line_of(path, 'Build s1.')}",
                "text": "§s1's premise names a counter the code cannot observe.",
            },
        ],
    )


def _find_run_for(plan: str, session: str) -> str:
    """Record a stored request that makes ``session`` the plan's coordinator."""
    run_id = f"r-20260101T000000000000-{plan}-s1"
    now = datetime.now(UTC).isoformat()
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": session,
        "plan": plan,
        "section": "s1",
        "node": {"id": f"{plan}-s1", "plan": plan, "section": "s1"},
        "phase": "queued",
        "created_at": now,
        "dispatched_at": now,
        "queued_at": now,
    }
    runs._write_json(pointer_path(run_id), pointer)
    return run_id


# --- cases -------------------------------------------------------------------


def test_a_review_owed_holds_only_the_section_its_finding_is_anchored_on(
    repository: Path,
) -> None:
    """The read answers "review owed" for §s1 and "may be queued" beside it."""
    _review_with_one_finding(repository / "docs" / "plans" / "plan-a.html")

    answers = _read(repository, "plan-a")
    assert answers["s1"]["may"] is False, answers
    assert answers["s1"]["reason"] == "review owed", answers
    for section in ("s2", "s3"):
        assert answers[section]["may"] is True, answers
        assert answers[section]["reason"] == "may be queued", answers

    whole = _whole_plan(repository, "plan-a")
    rows = {row["section"]: row for row in whole["results"]}
    for section in SECTIONS:
        assert rows[section]["queue_answer"] == answers[section]["reason"], rows
    assert rows["s1"]["status"] == "skipped", rows
    # The owed section queued no request, and its siblings did: the hold is the
    # review's and not the plan's.
    assert "run_id" not in rows["s1"], rows
    assert rows["s2"]["status"] in ("queued", "awaiting-upstream"), rows
    assert rows["s3"]["status"] in ("queued", "awaiting-upstream"), rows

    one = _one_section(repository, "plan-a", "s1")
    assert one["queue_answer"] == "review owed", one
    assert one["reason"] == "review owed", one


def test_an_open_decision_answers_for_every_section(repository: Path) -> None:
    """A plan-level open decision holds every section, in the read's own words."""
    answers = _read(repository, "plan-b")
    for section in SECTIONS:
        assert answers[section]["may"] is False, answers
        assert answers[section]["reason"] == "an open decision holds the plan"

    whole = _whole_plan(repository, "plan-b")
    rows = {row["section"]: row for row in whole["results"]}
    for section in SECTIONS:
        assert rows[section]["status"] == "skipped", rows
        assert rows[section]["reason"] == "an open decision holds the plan", rows

    one = _one_section(repository, "plan-b", "s1")
    assert one["queue_answer"] == "an open decision holds the plan", one


def test_a_plan_another_session_coordinates_answers_naming_it(
    repository: Path,
) -> None:
    """The read names the coordinating session, and both dispatches agree."""
    _find_run_for("plan-c", OTHER_SESSION)

    answers = _read(repository, "plan-c")
    for section in SECTIONS:
        assert answers[section]["may"] is False, answers
        assert answers[section]["reason"] == (
            f"plan coordinated by session {OTHER_SESSION}"
        ), answers

    whole = _whole_plan(repository, "plan-c")
    rows = {row["section"]: row for row in whole["results"]}
    for section in SECTIONS:
        assert rows[section]["status"] == "skipped", rows
        assert rows[section]["reason"] == (
            f"plan coordinated by session {OTHER_SESSION}"
        ), rows

    one = _one_section(repository, "plan-c", "s2")
    assert one["queue_answer"] == (f"plan coordinated by session {OTHER_SESSION}"), one


def test_a_plan_with_no_hold_may_be_queued(repository: Path) -> None:
    """A reviewed plan with nothing against it answers "may be queued"."""
    path = repository / "docs" / "plans" / "plan-a.html"
    _store(path, rubric="design", run_id="review-design", findings=[])
    _store(path, rubric="content", run_id="review-content", version=4, findings=[])
    answers = _read(repository, "plan-a")
    for section in SECTIONS:
        assert answers[section]["may"] is True, answers
        assert answers[section]["reason"] == "may be queued", answers


def test_the_read_over_three_hundred_plans_completes_under_two_seconds(
    many_plans: Path,
) -> None:
    """The read's cost follows the plans it reads, not a resolution per plan."""
    started = time.perf_counter()
    answers = dispatch_batch.project_queue_answers(
        PROJECT,
        session=SESSION,
        repo_root=many_plans,
        config=_config_for(PROJECT),
    )
    elapsed = time.perf_counter() - started
    assert len(answers) == 300, len(answers)
    print(f"MEASURE: plans=300 elapsed={elapsed:.3f}s")
    assert elapsed < 2.0, (
        f"the read over 300 plans took {elapsed:.2f}s; the fixture is the "
        "project a service pass reads, so the bound is the pass's own"
    )
