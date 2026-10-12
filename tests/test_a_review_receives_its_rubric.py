"""Every run review's brief carries its rubric, not just the done-when.

A review worker reads the prompt it is handed and nothing else. The rubric that
names the six dimensions, the SCORE emission grammar, the reuse rule, the
assertion-independence check and the claims grammar lives in a prompt file a
reviewer finds by searching the repository — which reaches a reviewer in any
other mounted project only by chance. These cases compose the reflex review of a
temporary implement run and of a temporary investigate run through
:func:`recovery._review_dispatch_fields` and hold the composed brief to the
rubric its own done-when assumes: the landed-node rubric for the implement run,
the claims rubric for the read-only run. The ``review-missing`` obligation's
next command is composed from the same fields, so the brief it names carries the
same text; a brief-carried run's stored brief is named as the reviewed
authority; and a node that names a plan keeps the plan as its authority, with
the composed brief riding beside it.

The negative control composes the brief from the reviewed run's own brief alone,
so the SCORE emission block is missing and the first case fails, which is the
red this node's log records.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from reckon import cli as cli_module
from reckon import flight
from reckon.crew import recovery, runs
from reckon.crew import review as review_module

CONFIG = {
    "default_backend": "worker",
    "local_backend": "worker",
    "backends": {
        "worker": {
            "launch": "cli",
            "command": "worker",
            "model": "test-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        }
    },
    "roles": {"review": {"backend": "worker", "execution_capable": True}},
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}


@pytest.fixture
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated crew home, so a run directory resolves under ``tmp_path``."""
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setattr(
        flight, "resolve", lambda *args, **kwargs: SimpleNamespace(config=CONFIG)
    )
    return home


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """A committed tree a run's repository can point at."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "seed.txt"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "chore: seed"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _compose(record: dict) -> dict:
    """The fields a dispatch composes, distinguished from a read by ``delta``.

    The run-review dispatch passes ``delta`` and materializes the brief; every
    other caller reads the fields without it.
    """
    return recovery._review_dispatch_fields(record, delta=None)


def _stored_brief(home: Path, run_id: str) -> Path:
    brief = home / "crew" / "runs" / run_id / "brief.md"
    brief.parent.mkdir(parents=True, exist_ok=True)
    brief.write_text(
        "BRIEF\nThe worker implements the seed change.\n", encoding="utf-8"
    )
    return brief


def _implement_record(repository: Path, brief: Path) -> dict:
    """A landed implement run carrying a plan section and a stored brief."""
    return {
        "run_id": "r-implement-run",
        "project": "sample",
        "repo": str(repository),
        "worktree": str(repository),
        "role": "implement",
        "node": {
            "id": "landed-node",
            "plan": "fixture",
            "section": "s2",
            "brief": str(brief),
            "brief_path": str(brief),
            "brief_sha256": hashlib.sha256(brief.read_bytes()).hexdigest(),
        },
    }


def _investigate_record(repository: Path, run_id: str) -> dict:
    """A read-only investigate run realised at a plan section."""
    return {
        "run_id": run_id,
        "project": "sample",
        "repo": str(repository),
        "worktree": str(repository),
        "role": "investigate",
        "node": {"id": "health-scan", "plan": "fixture", "section": "s2"},
    }


def test_an_implement_run_review_carries_the_scoring_rubric(
    crew_home: Path, repository: Path
) -> None:
    fields = _compose(
        _implement_record(repository, _stored_brief(crew_home, "r-implement-run"))
    )

    brief = fields["brief_text"]
    assert "SCORE <dimension>: <integer 0..20>" in brief
    for dimension in review_module.REVIEW_DIMENSIONS:
        assert f"SCORE {dimension}:" in brief
    # The brief the dispatch names is the same text the fields carry.
    assert Path(fields["brief"]).read_text(encoding="utf-8") == brief


def test_an_investigate_run_review_carries_the_claims_rubric(
    crew_home: Path, repository: Path
) -> None:
    fields = recovery._review_dispatch_fields(
        _investigate_record(repository, "r-investigate-run")
    )

    brief = fields["brief_text"]
    assert "CLAIM <rank>: <verdict>" in brief
    assert "CLAIM_SUMMARY:" in brief
    assert "SCORE <dimension>" not in brief


def test_the_obligation_next_command_names_a_brief_with_the_same_text(
    crew_home: Path, repository: Path
) -> None:
    record = _implement_record(repository, _stored_brief(crew_home, "r-implement-run"))
    fields = _compose(record)

    action = recovery._review_dispatch_action(record, config=CONFIG)
    argv = shlex.split(action)
    brief_path = Path(argv[argv.index("--brief") + 1])

    assert brief_path.read_text(encoding="utf-8") == fields["brief_text"]
    assert "SCORE <dimension>: <integer 0..20>" in brief_path.read_text(
        encoding="utf-8"
    )


def test_composing_a_review_for_a_home_less_run_creates_nothing(
    crew_home: Path, repository: Path
) -> None:
    """A read that composes a review must not bring a run's home into being.

    The ``review-missing`` obligation composes these fields to print the command
    that dispatches the review, so that composition is a read: it materializes
    no brief and creates no directory, because a directory created here would
    also draw the classification memo into it. The path is still named; only the
    dispatch that passes ``delta`` writes the file.
    """
    record = _investigate_record(repository, "r-no-home")

    fields = recovery._review_dispatch_fields(record)

    assert fields["brief"]
    assert not Path(fields["brief"]).exists()
    assert not runs.run_dir("r-no-home").exists()


def test_composing_a_review_for_a_dispatch_writes_its_brief(
    crew_home: Path, repository: Path
) -> None:
    """The dispatch that passes ``delta`` gets the brief its command names."""
    record = _investigate_record(repository, "r-live-run")

    fields = _compose(record)

    assert Path(fields["brief"]).read_text(encoding="utf-8") == fields["brief_text"]


def test_a_brief_carried_run_names_its_stored_brief(
    crew_home: Path, repository: Path
) -> None:
    brief = _stored_brief(crew_home, "r-implement-run")
    fields = recovery._review_dispatch_fields(_implement_record(repository, brief))

    assert str(brief) in fields["brief_text"]


def test_a_plan_naming_implement_run_names_plan_and_section_beside_its_brief(
    crew_home: Path, repository: Path
) -> None:
    record = _implement_record(repository, _stored_brief(crew_home, "r-implement-run"))
    argv = recovery._review_dispatch_argv(record, config=CONFIG)

    assert argv[argv.index("--brief") + 1]
    assert argv[argv.index("--plan") + 1] == "fixture"
    assert argv[argv.index("--section") + 1] == "s2"


# ── The emitted review command is one dispatch accepts, for every run shape ──


@pytest.fixture
def authority_plan(crew_home: Path, repository: Path) -> Path:
    """A mounted project whose plan carries the section a review names.

    A review of a run that names a plan section keeps the plan as its authority,
    so its command carries ``--plan``/``--section`` beside ``--brief`` and
    dispatch joins the node to its committed section. The plan, its mount and
    the run's repository must agree on the project, so the plan lands in the run
    repository's own docs tree, which the mount registers for the project.
    """
    plan = repository / "docs" / "plans" / "fixture.html"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        "<!doctype html>\n<html><head>\n"
        '<meta name="docs-project" content="sample">\n'
        '<meta name="reckon-type" content="plan">\n'
        '<meta name="plan-slug" content="fixture">\n'
        '</head><body><h2 id="s2">§2 — Review</h2></body></html>\n',
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "add", "docs/plans/fixture.html"], cwd=repository, check=True
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "chore: mount a plan"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    (crew_home / "mounts.json").write_text(
        json.dumps({"sample": str(repository / "docs")}), encoding="utf-8"
    )
    return plan


def _dry_run(argv: list[str], repo: Path):
    """Run an emitted review command through ``crew dispatch --dry-run``."""
    return CliRunner().invoke(
        cli_module.main, [*argv[1:], "--repo", str(repo), "--dry-run"]
    )


def _run_record(repository: Path, run_id: str, node: dict) -> dict:
    """A run whose node names one of the three authority shapes."""
    return {
        "run_id": run_id,
        "project": "sample",
        "repo": str(repository),
        "worktree": str(repository),
        "role": "implement",
        "session": "sess",
        "node": {"id": "landed-node", **node},
    }


def _materialized_argv(record: dict) -> list[str]:
    """Materialize the review brief as the reflex does, then build the command.

    The classifier's read composes the same path and text but creates nothing,
    so the brief a printed command names is written by the dispatch that passes
    ``delta``. Writing it here reproduces that dispatch, so the dry-run proves
    the emitted command is accepted rather than the file's absence.
    """
    _compose(record)
    return recovery._review_dispatch_argv(record, config=CONFIG)


def test_a_plan_section_review_command_is_dispatchable(
    crew_home: Path, repository: Path, authority_plan: Path
) -> None:
    """Plan-and-section: the brief rides beside the plan section, so both name."""
    record = _run_record(
        repository, "r-plan-section", {"plan": "fixture", "section": "s2"}
    )
    argv = _materialized_argv(record)

    assert argv[argv.index("--brief") + 1]
    assert argv[argv.index("--plan") + 1] == "fixture"
    assert argv[argv.index("--section") + 1] == "s2"

    result = _dry_run(argv, repository)

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["ok"] is True


def test_a_plan_without_a_section_review_command_is_dispatchable(
    crew_home: Path, repository: Path
) -> None:
    """Plan-and-no-section: the brief is the authority, so no sectionless plan."""
    record = _run_record(repository, "r-plan-no-section", {"plan": "fixture"})
    argv = _materialized_argv(record)

    # The dry-run is what proves dispatch accepts the command: emitting
    # ``--brief`` beside the sectionless plan makes dispatch refuse the pair,
    # which is the red this node's negative control records.
    result = _dry_run(argv, repository)

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["ok"] is True
    assert argv[argv.index("--brief") + 1]
    assert "--plan" not in argv and "--section" not in argv


def test_a_brief_carried_review_command_is_dispatchable(
    crew_home: Path, repository: Path
) -> None:
    """Brief-and-no-plan: the brief stands alone as the review's authority."""
    brief = _stored_brief(crew_home, "r-brief-run")
    record = _run_record(
        repository, "r-brief-run", {"brief": str(brief), "brief_path": str(brief)}
    )
    argv = _materialized_argv(record)

    assert argv[argv.index("--brief") + 1]
    assert "--plan" not in argv and "--section" not in argv

    result = _dry_run(argv, repository)

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["ok"] is True
