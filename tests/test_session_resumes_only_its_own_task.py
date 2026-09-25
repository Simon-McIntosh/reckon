"""A dispatch continues a session only when it is the same task's.

The session a run may continue belongs to a piece of work, not to the member
that happened to carry it. Keyed to the member, a new node sent to a member
that last ran a different node resumed that node's conversation — the member
being the key made two unrelated tasks share one context. The task is what a
session may be continued for: `(project, plan, node id)` for an implement,
test or investigate run, and the reviewed run id for a review.

Every case asserts on the argv the real `reckon.crew.dispatch.dispatch` path
composes, never on a helper's return value: the resumed-conversation defect
stayed invisible precisely because a helper answered the question the caller
did not ask. The prior session is read back from committed run records and
live pointers of the task, so the roster's `sessions` map is not the authority
even when it still holds an entry.
"""

from __future__ import annotations

import itertools
import json
import subprocess
from collections.abc import Mapping
from importlib import import_module
from pathlib import Path

import pytest

from reckon import crew, ledger

# `crew` re-exports a `dispatch` function under that name, so the module is
# reached by import rather than by attribute.
dispatch_module = import_module("reckon.crew.dispatch")

CONFIG = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "medium",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}, "review": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}
FIXTURE = Path(__file__).parent / "fixtures" / "backends" / "codex-turn.jsonl"
FIXTURE_SESSION = "019ff509-8a60-7723-94fd-65942a6d8faa"

ALPHA_SESSION = "066f04b2-75c1-43f0-aa27-0d72a67b340f"
BETA_SESSION = "166f04b2-75c1-43f0-aa27-0d72a67b340f"
REVIEW_SESSION = "266f04b2-75c1-43f0-aa27-0d72a67b340f"

_SESSIONS = itertools.count(1)

PLAN = "plan-a"
PLAN_B = "plan-b"
SECTION = "session-routing"


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


@pytest.fixture()
def repo(tmp_path: Path, home: Path) -> Path:
    root = tmp_path / "repo"
    (root / "skills" / "reckon-build" / "scripts").mkdir(parents=True)
    (root / "docs" / "plans").mkdir(parents=True)
    fleet_script = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (root / "skills" / "reckon-build" / "scripts" / "worktree_fleet.py").write_text(
        fleet_script.read_text()
    )
    for slug in (PLAN, PLAN_B):
        (root / "docs" / "plans" / f"{slug}.html").write_text(
            f"""<!doctype html>
<html><head>
<meta name="docs-project" content="proj">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="{slug}">
</head><body><h2 id="session-routing">Session routing</h2></body></html>
"""
        )
    (root / "seed.txt").write_text("seed\n")
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        [
            "add",
            "seed.txt",
            "skills",
            "docs/plans/plan-a.html",
            "docs/plans/plan-b.html",
        ],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    (home / "mounts.json").write_text(json.dumps({"proj": str(root / "docs")}))
    ledger.register_member("proj", "worker-a", harness="alpha", root=root)
    return root


def _node(
    node_id: str, home: Path, *, role: str = "implement", plan: str = PLAN
) -> object:
    return crew.TaskNode(
        id=node_id,
        goal=f"verify that {node_id} continues only its own task's session",
        plan=plan,
        section=SECTION,
        role=role,
        spec_level="guided",
        done_when="pytest tests/test_session_resumes_only_its_own_task.py passes",
        write_paths=[f"reckon/session-task/{node_id}.py"],
        time_budget="20m",
        manifest_path=str(home / f"manifest-{node_id}.md"),
    )


def _dispatch(
    home: Path,
    repo: Path,
    node_id: str,
    *,
    role: str = "implement",
    plan: str = PLAN,
    member: str = "worker-a",
    session: str | None = None,
    config: Mapping[str, object] = CONFIG,
) -> dict[str, object]:
    # Two dispatches share a coordinator session only where the case must be
    # comparable against the roster's ownership of that session; every other
    # case takes a fresh one, which the worktree path is keyed on as well.
    return crew.dispatch(
        node=_node(node_id, home, role=role, plan=plan),
        project="proj",
        repo=repo,
        config=config,
        session=session or f"coordinator-{next(_SESSIONS)}",
        member=member,
        launcher=lambda *args, **kwargs: 999991,
    )


def _complete(record: Mapping[str, object], session_id: str) -> None:
    Path(str(record["log_path"])).write_text(
        FIXTURE.read_text().replace(FIXTURE_SESSION, session_id)
    )
    observed = crew.observe(str(record["run_id"]))
    assert observed["phase"] == "complete"
    assert observed["session_id"] == session_id


def _resume_carried(argv: list[str]) -> str | None:
    """The session a composed argv resumes, or None when it starts fresh."""
    if "resume" not in argv:
        return None
    return argv[argv.index("resume") + 1]


# ── case 1: a new node on a member whose session belongs to another node ─────


def test_a_new_node_does_not_continue_the_members_other_node_session(
    home: Path,
    repo: Path,
) -> None:
    """The defect: the member's last session, keyed by member, not by task."""
    shared = "coordinator-one"
    roster_path = repo / "docs" / "state" / "proj" / "crew.json"
    roster_before = roster_path.read_bytes()
    first = _dispatch(home, repo, "task-alpha", session=shared)
    _complete(first, ALPHA_SESSION)
    # A real captured session exists on the other task's run. The next
    # dispatch must withhold it even though both tasks share a member.
    recorded = crew.read_pointer(str(first["run_id"]))
    assert recorded["session_id"] == ALPHA_SESSION
    assert recorded["session_harness"] == "codex"
    assert recorded["session_model"] == "some-model"
    assert roster_path.read_bytes() == roster_before

    dispatched = _dispatch(home, repo, "task-beta", session=shared)

    assert dispatched["session_id"] is None
    assert _resume_carried(list(dispatched["argv"])) is None
    assert roster_path.read_bytes() == roster_before


# ── case 2: a second dispatch of the same task continues its session ────────


def test_a_second_dispatch_of_the_same_node_continues_its_session(
    home: Path,
    repo: Path,
) -> None:
    first = _dispatch(home, repo, "task-alpha")
    _complete(first, ALPHA_SESSION)

    second = _dispatch(home, repo, "task-alpha")

    assert second["session_id"] == ALPHA_SESSION
    assert _resume_carried(list(second["argv"])) == ALPHA_SESSION


def test_the_same_node_id_under_another_plan_is_a_different_task(
    home: Path,
    repo: Path,
) -> None:
    """Task identity carries the plan, so a same-named node elsewhere is new."""
    first = _dispatch(home, repo, "task-alpha", plan=PLAN)
    _complete(first, ALPHA_SESSION)

    elsewhere = crew.dispatch(
        node=_node("task-alpha", home, plan=PLAN_B),
        project="proj",
        repo=repo,
        config=CONFIG,
        session=f"coordinator-{next(_SESSIONS)}",
        member="worker-a",
        launcher=lambda *args, **kwargs: 999991,
    )

    assert elsewhere["session_id"] is None
    assert _resume_carried(list(elsewhere["argv"])) is None


# ── case 3: a re-review continues the earlier review of the same run ────────


def test_a_re_review_continues_the_earlier_review_of_the_same_run(
    home: Path,
    repo: Path,
) -> None:
    _complete(_dispatch(home, repo, "subject-node"), BETA_SESSION)

    first_review = _dispatch(home, repo, "review-of-subject-node", role="review")
    assert first_review["session_id"] is None
    _complete(first_review, REVIEW_SESSION)

    again = _dispatch(home, repo, "review-of-subject-node", role="review")

    assert again["session_id"] == REVIEW_SESSION
    assert _resume_carried(list(again["argv"])) == REVIEW_SESSION


def test_a_review_of_a_different_run_starts_fresh(home: Path, repo: Path) -> None:
    _complete(_dispatch(home, repo, "subject-node"), BETA_SESSION)
    _complete(_dispatch(home, repo, "other-subject"), ALPHA_SESSION)
    first_review = _dispatch(home, repo, "review-of-subject-node", role="review")
    _complete(first_review, REVIEW_SESSION)

    other_review = _dispatch(home, repo, "review-of-other-subject", role="review")

    assert other_review["session_id"] is None
    assert _resume_carried(list(other_review["argv"])) is None


# ── case 4: a session too large to continue is not composed ─────────────────


REFUSAL_LINES = {
    "prompt-too-long": json.dumps(
        {"type": "result", "is_error": True, "result": "Prompt is too long"}
    ),
    "blocking-limit": json.dumps(
        {"type": "result", "is_error": True, "terminal_reason": "blocking_limit"}
    ),
    "unfinished-compaction": json.dumps(
        {"type": "system", "subtype": "status", "status": "compacting"}
    ),
}


@pytest.mark.parametrize("refusal", sorted(REFUSAL_LINES))
def test_a_session_too_large_to_continue_is_not_composed(
    home: Path, repo: Path, refusal: str
) -> None:
    """A run promoted on success still leaves a session the endpoint refuses."""
    first = _dispatch(home, repo, "task-alpha")
    _complete(first, ALPHA_SESSION)
    stream = Path(str(first["log_path"]))
    with stream.open("a", encoding="utf-8") as handle:
        handle.write("\n" + REFUSAL_LINES[refusal] + "\n")

    second = _dispatch(home, repo, "task-alpha")

    assert second["session_id"] is None
    assert _resume_carried(list(second["argv"])) is None
    withheld = second["session_withheld"]
    assert withheld is not None
    assert withheld["session_id"] == ALPHA_SESSION


def test_a_completed_compaction_boundary_leaves_the_session_continuable(
    home: Path,
    repo: Path,
) -> None:
    """The disqualifier is an unfinished compaction, not compaction itself."""
    first = _dispatch(home, repo, "task-alpha")
    _complete(first, ALPHA_SESSION)
    stream = Path(str(first["log_path"]))
    with stream.open("a", encoding="utf-8") as handle:
        handle.write(
            "\n"
            + json.dumps(
                {"type": "system", "subtype": "status", "status": "compacting"}
            )
            + "\n"
            + json.dumps(
                {
                    "type": "system",
                    "subtype": "compact_boundary",
                    "compact_metadata": {"pre_tokens": 180000},
                }
            )
            + "\n"
        )

    second = _dispatch(home, repo, "task-alpha")

    assert second["session_id"] == ALPHA_SESSION
    assert _resume_carried(list(second["argv"])) == ALPHA_SESSION


# ── case 5: a resume still continues the resumed run's own session ──────────


def test_a_resume_of_a_run_continues_that_runs_own_session(
    home: Path, repo: Path
) -> None:
    dispatched = _dispatch(home, repo, "task-alpha")
    _complete(dispatched, ALPHA_SESSION)

    plan = dispatch_module.resume_plan(
        str(dispatched["run_id"]),
        "the limit has reset; continue",
        config=CONFIG,
    )

    argv = list(plan.as_dict().get("argv") or [])
    assert _resume_carried(argv) == ALPHA_SESSION
