"""Every single-run crew verb takes one identity grammar.

``--run`` names the run and ``--project`` is an optional cross-check accepted
beside it, refused when it disagrees with the run's own project. The sweep
enumerates the crew command tree rather than a hard-coded list, so a verb added
later is covered. It drives the repeated-finding answer both ways: without
``--review-run`` a finding carried by two covering reviews is refused naming
both, and with it the answer lands on the review named.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon.crew import plan_review
from reckon.crew.run_paths import crew_home

SAMPLE_RUN = "r-20260101T000000000000-sweep"
SAMPLE_PROJECT = "sweep-fixture"
OTHER_PROJECT = "sweep-other"

# ``follow`` declares ``--run`` as a repeatable delivery filter that already
# takes ``--project``; it is not a single-run identity and is excluded by name.
EXCLUDED_VERBS = {"follow"}

DISAGREEMENT = "disagrees with run"


def _options(command) -> set[str]:
    return {opt for param in command.params for opt in getattr(param, "opts", [])}


def _single_run_verbs() -> dict[str, object]:
    """Every leaf verb in the crew tree whose only --run is a single-run id."""
    found: dict[str, object] = {}

    def walk(group, prefix: str) -> None:
        for name, command in group.commands.items():
            path = f"{prefix}{name}"
            if getattr(command, "commands", None):
                walk(command, path + " ")
                continue
            run_params = [
                param
                for param in command.params
                if "--run" in getattr(param, "opts", [])
            ]
            if not run_params:
                continue
            if any(getattr(param, "multiple", False) for param in run_params):
                continue
            found[path] = command

    walk(cli_module.crew, "")
    return found


VERBS = _single_run_verbs()

# The minimal valid options each verb needs beyond --run, so click's own
# required-option validation does not fire before the identity check.
MINIMAL_OPTIONS: dict[str, list[str]] = {
    "ack": ["--reason", "deferred for the sweep", "--until", "2999-01-01T00:00:00Z"],
    "attach": ["--task", "sweep-task"],
    "check": ["--manifest"],
    "complete": ["--gate", "passed"],
    "directory": [],
    "discard": [],
    "dispose": [
        "--kind",
        "exempted",
        "--reason",
        "sweep fixture",
        "--dimension",
        "reuse_search",
    ],
    "gc": [],
    "observe": [],
    "path": ["--kind", "runs"],
    "queue drop": [],
    "queue edit": ["--time-budget", "30m"],
    "queue hold": [],
    "queue release": [],
    "queue reprioritise": ["--priority", "1"],
    "repair": ["--status", "complete", "--reason", "sweep fixture"],
    "resume": ["--advice", "sweep fixture"],
    "shadow": ["--session", "sweep-session", "--backend", "local-worker"],
    "stop": [],
    "supersede-review": ["--reason", "sweep fixture"],
    "verify-gate": ["--checkout-path", "."],
    "view": [],
    "widen": ["--write-path", "reckon/sweep_fixture.py"],
}

# What the matching-project arm expects to reach: the exit code, and the verb's
# own words where it refuses the fixture run for a reason of its own. A live
# pointer carrying a project and nothing else is all the fixture run is, so a
# verb reading state the run does not hold refuses it; the empty reason marks a
# verb that completes on that fixture.
ACCEPTED: dict[str, tuple[int, str]] = {
    "ack": (0, ""),
    "attach": (1, "is a None launch"),
    "check": (1, "records no node definition"),
    "complete": (1, "cannot be promoted"),
    "directory": (0, ""),
    "discard": (0, ""),
    "dispose": (1, "no stored review for run"),
    "gc": (1, "no record names a worktree"),
    "observe": (0, ""),
    "path": (0, ""),
    "queue drop": (1, "not one of"),
    "queue edit": (1, "not one of"),
    "queue hold": (1, "crew stop"),
    "queue release": (1, "not held"),
    "queue reprioritise": (1, "not one of"),
    "repair": (1, "has no manifest to repair"),
    "resume": (1, "is not a spawned run"),
    "shadow": (1, "is not a committed ledger record"),
    "stop": (1, "has no process to stop"),
    "supersede-review": (1, "names no committed row"),
    "verify-gate": (1, "has no row in the"),
    "view": (0, ""),
    "widen": (1, "records no node holding a write scope"),
}


@pytest.fixture()
def resolvable_run() -> str:
    """A live pointer whose project a --project can agree or disagree with."""
    _write_run_pointer()
    return SAMPLE_RUN


def _write_run_pointer() -> None:
    """Seed a live pointer; re-seeded between invokes of a mutating verb.

    A verb whose matching invocation legitimately acts on the run (``discard``
    removes the pointer) would otherwise leave the disagreeing invocation with
    no run to resolve, hiding the identity refusal behind the verb's own
    missing-run error.
    """
    live = crew_home() / "live"
    live.mkdir(parents=True, exist_ok=True)
    (live / f"{SAMPLE_RUN}.json").write_text(
        json.dumps({"run_id": SAMPLE_RUN, "project": SAMPLE_PROJECT}),
        encoding="utf-8",
    )


def test_the_sweep_covers_every_single_run_verb() -> None:
    assert VERBS, "the crew tree exposed no single-run verb to sweep"
    assert set(VERBS) - EXCLUDED_VERBS, "no verb beyond the exclusion was found"
    assert not (set(VERBS) & EXCLUDED_VERBS), (
        "follow declares a repeatable --run and must be excluded by its filter shape"
    )
    assert set(MINIMAL_OPTIONS) >= set(VERBS) - EXCLUDED_VERBS, (
        "every swept verb needs its minimal options declared"
    )


def test_every_single_run_verb_declares_a_project_option() -> None:
    missing = sorted(
        verb for verb, command in VERBS.items() if "--project" not in _options(command)
    )
    assert missing == [], f"these --run verbs do not accept --project: {missing}"


@pytest.mark.parametrize("verb", sorted(VERBS))
def test_a_matching_project_is_accepted_and_a_disagreeing_one_refused(
    verb: str, resolvable_run: str
) -> None:
    extra = MINIMAL_OPTIONS[verb]
    common = ["crew", *verb.split(), "--run", resolvable_run, *extra]

    agreed = CliRunner().invoke(cli_module.main, [*common, "--project", SAMPLE_PROJECT])
    # Accepting the project means the invocation reaches the verb, so this arm
    # asserts the exit code it reaches there and, where the fixture run lacks
    # the state a verb acts on, the verb's own refusal by its own words. A verb
    # that answered the identity check with anything else failed this arm: a
    # fixture run carries a live pointer and nothing more, so a verb reading a
    # manifest, a stored review, a ledger row, a worktree or a spawned process
    # legitimately refuses it, and only for that reason.
    expected_code, expected_refusal = ACCEPTED[verb]
    assert agreed.exit_code == expected_code, (
        f"{verb} did not reach its own behaviour with a matching --project, "
        f"expected exit {expected_code}:\n{agreed.output}"
    )
    if expected_refusal:
        assert expected_refusal in agreed.output, (
            f"{verb} refused a matching --project for a reason other than its "
            f"own ({expected_refusal!r}):\n{agreed.output}"
        )
    assert DISAGREEMENT not in agreed.output, (
        f"{verb} refused a --project that agrees with the run:\n{agreed.output}"
    )
    _write_run_pointer()

    disagreed = CliRunner().invoke(
        cli_module.main, [*common, "--project", OTHER_PROJECT]
    )
    assert disagreed.exit_code != 0, (
        f"{verb} accepted a --project that disagrees with the run:\n{disagreed.output}"
    )
    assert DISAGREEMENT in disagreed.output, (
        f"{verb} did not name the disagreement:\n{disagreed.output}"
    )


# ── The repeated-finding answer ──────────────────────────────────────────────


@pytest.fixture()
def reviewed_project(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repo = tmp_path / "repo"
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    plan_path = plans / "fixture.html"
    plan_path.write_text(
        """<!doctype html>
<html><head>
<meta name="docs-project" content="sample">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="fixture">
<meta name="plan-title" content="Fixture">
<meta name="plan-status" content="active">
<meta name="plan-impl" content="0.0">
<meta name="plan-modified" content="2026-09-25">
<meta name="plan-version" content="1">
</head><body><h2 id="delivery">Delivery</h2><p>Ship one measured change.</p></body></html>
""",
        encoding="utf-8",
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "plans-main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "docs/plans/fixture.html"],
        ["commit", "-q", "-m", "chore: seed fixture"],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (config_home / "mounts.json").write_text(
        json.dumps({"sample": str(repo / "docs")}), encoding="utf-8"
    )
    return config_home, repo, plan_path


def _store_review(
    config_home: Path,
    *,
    rubric: str,
    version: int,
    fingerprint: str,
    run_id: str,
    finding: str,
) -> None:
    plan_review.store_plan_review(
        {
            "project": "sample",
            "plan_slug": "fixture",
            "plan_version": version,
            "rubric": rubric,
            "reviewed_blob_sha": run_id[-1] * 40,
            "plan_fingerprint": fingerprint,
            "findings": [{"id": finding, "type": "reuse", "text": "duplicate"}],
            "responses": {},
            "review_run_id": run_id,
        },
        base_dir=config_home / "crew" / "reviews",
    )


def _two_covering_reviews(config_home: Path, plan_path: Path, finding: str) -> None:
    _store_review(
        config_home,
        rubric="design",
        version=0,
        fingerprint="an-earlier-version",
        run_id="r-design-0",
        finding=finding,
    )
    _store_review(
        config_home,
        rubric="content",
        version=1,
        fingerprint=plan_review.plan_fingerprint(plan_path),
        run_id="r-content-1",
        finding=finding,
    )


def test_a_finding_carried_by_two_covering_reviews_is_refused_naming_both(
    reviewed_project: tuple[Path, Path, Path],
) -> None:
    config_home, _repo, plan_path = reviewed_project
    finding = "dup-1"
    _two_covering_reviews(config_home, plan_path, finding)

    result = CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "review",
            "--project",
            "sample",
            "--plan",
            "fixture",
            "--answer",
            finding,
            "--acted",
        ],
    )
    assert result.exit_code != 0, result.output
    assert "r-design-0" in result.output, result.output
    assert "r-content-1" in result.output, result.output
    assert "--review-run" in result.output, result.output


def test_review_run_selects_which_covering_review_answers_the_finding(
    reviewed_project: tuple[Path, Path, Path],
) -> None:
    config_home, _repo, plan_path = reviewed_project
    finding = "dup-1"
    _two_covering_reviews(config_home, plan_path, finding)

    result = CliRunner().invoke(
        cli_module.main,
        [
            "crew",
            "review",
            "--project",
            "sample",
            "--plan",
            "fixture",
            "--answer",
            finding,
            "--acted",
            "--review-run",
            "r-design-0",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert finding not in payload["unanswered"], payload
    # The answer must land on the review --review-run named. Both covering
    # reviews raise the finding, so a refusal read from the payload cannot tell
    # which one was answered: the stores must say it.
    stored = {
        plan_review._record_run_id(record): record
        for record in plan_review.list_plan_reviews(
            "sample", base_dir=config_home / "crew" / "reviews"
        )
    }
    assert set(stored) == {"r-design-0", "r-content-1"}, sorted(stored)
    assert stored["r-design-0"]["responses"][finding]["action"] == "acted", stored[
        "r-design-0"
    ]
    assert stored["r-content-1"]["responses"] == {}, stored["r-content-1"]
