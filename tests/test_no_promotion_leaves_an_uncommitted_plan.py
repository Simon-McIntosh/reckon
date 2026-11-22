"""No promotion leaves uncommitted bytes in a plan file.

A promotion writes a landing comment into the shared checkout's plan file and
then commits it. When two promotions write to one plan, or one lands while the
file already carries another's comment, the second writer used to find bytes it
did not own, keep them and stop: the file stayed modified in the shared
checkout's plan file and every later landing on that plan was refused for those
bytes until a coordinator committed the file by hand.

Each case here ends the same way: the plan file is byte-equal to the committed
revision of the checkout, and the comments it carries are committed and still
attributed to the run that wrote them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import _plan_html, _store, crew
from reckon.crew import promotion
from reckon.crew.runs import _write_json, pointer_path
from tests.helpers.git import git as _git

PROJECT = "no-uncommitted-plan-fixture"
PLAN = "landing-target"
PLAN_PATH = "docs/plans/landing-target.html"
RUN_ONE = "r-20261122T090000000001-first-promotion-one"
RUN_TWO = "r-20261122T090000000002-second-promotion-two"
RUN_THREE = "r-20261122T090000000003-gate-refused-three"
# Synthetic promotions whose subject is the plan write itself carry no stored
# review; the landing comment is what these cases measure.
REVIEW_WAIVER = "the landing write is the subject; this synthetic run stores no review"
LANDING_ONE = "the first promotion records its landing comment"
LANDING_TWO = "the second promotion records its landing comment"
LANDING_THREE = "the later landing records its own comment"
AUTHOR = "reckon-build"
WHEN_ONE = "2026-11-22T09:00:00Z"

REAL_LIVE = Path.home() / ".config" / "reckon" / "crew" / "live"


@pytest.fixture(autouse=True)
def the_real_stores_are_not_fixture_targets() -> None:
    for run_id in (RUN_ONE, RUN_TWO, RUN_THREE):
        assert not (REAL_LIVE / (run_id + ".json")).exists()
    yield
    for run_id in (RUN_ONE, RUN_TWO, RUN_THREE):
        assert not (REAL_LIVE / (run_id + ".json")).exists()


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    path = root / PLAN_PATH
    path.parent.mkdir(parents=True)
    bare = (
        "<!doctype html><html><head>"
        "<title>" + PLAN + "</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(
        _plan_html.write_state(
            bare,
            {
                "type": "plan",
                "slug": PLAN,
                "title": "Landing target",
                "status": "active",
                "version": 0,
                "comments": {},
            },
        ),
        encoding="utf-8",
    )
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs"),
        ("commit", "-q", "-m", "test: seed repository"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _dispatch(root: Path, run_id: str, tmp_path: Path, landing: str) -> Path:
    """Write the manifest and live pointer a delivered run is promoted from."""
    manifest = tmp_path / (run_id + "-manifest.md")
    manifest.write_text(
        "node: " + run_id + "\nstatus: complete\nlanding: " + landing + "\n",
        encoding="utf-8",
    )
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(root),
            "worktree": str(root),
            "launch": "in-harness",
            "role": "implement",
            "member": "worker-a",
            "backend": "native",
            "created_at": "2026-11-22T09:00:00Z",
            "manifest_path": str(manifest),
            "node": {
                "id": run_id,
                "plan": PLAN,
                "section": "§3",
                "time_budget": "25m",
                "write_paths": [],
            },
        },
    )
    return manifest


def _promote(run_id: str, root: Path) -> dict:
    return crew.complete(
        run_id,
        gate="passed",
        outcome="",
        root=root,
        review_waiver=REVIEW_WAIVER,
    )


def _comments(root: Path) -> dict[str, dict]:
    state, _version = _store.read_plan(PROJECT, PLAN, root, artifact_type="plan")
    found: dict[str, dict] = {}
    for entries in (state.get("comments") or {}).values():
        for entry in entries:
            found[str(entry.get("id"))] = dict(entry)
    return found


def _plan_state(root: Path) -> str:
    """The plan's working-tree bytes against HEAD, and its porcelain status."""
    path = root / PLAN_PATH
    status = _git(root, "status", "--porcelain", "--", PLAN_PATH).strip()
    head = _git(root, "show", "HEAD:" + PLAN_PATH)
    return "status=" + repr(status) + " equal=" + repr(path.read_text().strip() == head)


def test_a_gate_refused_promotion_commits_the_comment_it_met(
    repository: Path, tmp_path: Path
) -> None:
    """A refusal must end with the plan file equal to the committed revision."""
    _dispatch(repository, RUN_ONE, tmp_path, LANDING_ONE)
    promotion._record_landing_comment(
        project=PROJECT,
        plan=PLAN,
        section="§3",
        run_id=RUN_ONE,
        narrative=LANDING_ONE,
        author=AUTHOR,
        when=WHEN_ONE,
        root=repository,
    )
    met_before = _comments(repository)["c-run-" + RUN_ONE]
    before = _plan_state(repository)
    assert "equal=False" in before, before
    assert "c-run-" + RUN_ONE in _comments(repository)

    _dispatch(repository, RUN_THREE, tmp_path, LANDING_THREE)
    with pytest.raises(Exception) as refused:  # noqa: PT011 - the text is checked
        crew.complete(
            RUN_THREE,
            gate="passed",
            outcome="",
            root=repository,
            review_waiver=REVIEW_WAIVER,
            require_gate_check=True,
            gate_check={},
        )
    assert "requires the check" in str(refused.value), str(refused.value)

    after = _plan_state(repository)
    assert after.endswith("equal=True"), after
    assert "status=''" in after, after
    met = _comments(repository)
    assert "c-run-" + RUN_ONE in met, sorted(met)
    assert met["c-run-" + RUN_ONE] == met_before, met["c-run-" + RUN_ONE]


def test_a_refusal_after_the_plan_write_commits_the_comment_it_met(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal raised inside the promotion settles the plan it found dirty.

    The gate-check refusal stops before the promotion's own write, so its
    restore target is the file as it stands. A refusal raised after the write
    began restores to a preimage that already carried the other promotion's
    uncommitted comment, and restoring is not enough: those bytes are still
    uncommitted when the restore succeeds.
    """
    _dispatch(repository, RUN_ONE, tmp_path, LANDING_ONE)
    promotion._record_landing_comment(
        project=PROJECT,
        plan=PLAN,
        section="§3",
        run_id=RUN_ONE,
        narrative=LANDING_ONE,
        author=AUTHOR,
        when=WHEN_ONE,
        root=repository,
    )
    met_before = _comments(repository)["c-run-" + RUN_ONE]
    assert "equal=False" in _plan_state(repository)

    def refuse(*args: object, **kwargs: object) -> dict:
        raise promotion.CrewError("the ledger row could not be written")

    monkeypatch.setattr(promotion, "_record_landing_comment", refuse)
    _dispatch(repository, RUN_TWO, tmp_path, LANDING_TWO)
    with pytest.raises(Exception) as refused:  # noqa: PT011 - the text is checked
        _promote(RUN_TWO, repository)
    assert "the ledger row could not be written" in str(refused.value)
    assert "committed the landing comment of" in str(refused.value), str(refused.value)

    state = _plan_state(repository)
    assert state.endswith("equal=True"), state
    assert "status=''" in state, state
    met = _comments(repository)
    assert met["c-run-" + RUN_ONE] == met_before, met["c-run-" + RUN_ONE]
    assert "c-run-" + RUN_TWO not in met, sorted(met)


def test_a_landing_over_an_uncommitted_comment_commits_it(
    repository: Path, tmp_path: Path
) -> None:
    """A landing meeting another promotion's comment is not refused for it."""
    _dispatch(repository, RUN_ONE, tmp_path, LANDING_ONE)
    promotion._record_landing_comment(
        project=PROJECT,
        plan=PLAN,
        section="§3",
        run_id=RUN_ONE,
        narrative=LANDING_ONE,
        author=AUTHOR,
        when=WHEN_ONE,
        root=repository,
    )
    met_before = _comments(repository)["c-run-" + RUN_ONE]
    assert "equal=False" in _plan_state(repository)

    _dispatch(repository, RUN_TWO, tmp_path, LANDING_TWO)
    _promote(RUN_TWO, repository)

    comments = _comments(repository)
    assert "c-run-" + RUN_ONE in comments, sorted(comments)
    assert "c-run-" + RUN_TWO in comments, sorted(comments)
    state = _plan_state(repository)
    assert state.endswith("equal=True"), state
    assert "status=''" in state, state
    assert comments["c-run-" + RUN_ONE] == met_before, comments["c-run-" + RUN_ONE]
