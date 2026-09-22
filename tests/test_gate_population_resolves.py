"""A gate population names files the repository has.

A gate command is evidence about what was run, so a population it names that the
repository does not hold is caught when the node is dispatched, where it costs a
refusal, rather than inside the worker, where it costs the worker's judgement
about what the coordinator meant. Measured on the launch-resolution node, whose
brief listed ``tests/test_crew_resum*.py`` while no file matched it.

A population is a set of files, which is what a glob metacharacter spells; a
single literal path is a file claim rather than a population, and one the
repository lacks fails loudly inside the worker's own gate run, so it is not
this finding's subject. The check asks the store rather than policing the
spelling, so it admits a glob matching at least one file, never rejects a
pattern for containing a glob character, and leaves a node naming no population
alone. The fixture repository holds exactly one seeded test file, which is what
lets a matching glob and a missing population be put to the same store in one
case: the verdicts differ because of what the repository holds, not because of
how the patterns are spelt. The patterns are not the repository's real ones, so
the cases read this repository's state only through the same assertions.

A node that creates the files its own gate runs names them before they exist, so
a population resolving against one of the node's declared write paths is
admitted; that is the door the exemption leaves open rather than a silent hole.

The declared mutation removes the match check from ``plan_dispatch``, and the red
log it produced is kept beside the passing one under
``reports/reckon/crew-runtime-positive-controls/gate-population/``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import crew

PROJECT = "proj"

# The population seeded into the fixture repository, and the two patterns the
# cases put to it: one the repository holds and one it does not.
SEEDED_TEST = "tests/test_crew_dispatch_seeded.py"
MATCHING_GLOB = "tests/test_crew_dispatch*.py"
MISSING_POPULATION = "tests/test_crew_resum*.py"
DECLARED_NODE_WRITE_PATH = "tests/test_gate_population_resolves.py"
DECLARED_POPULATION = "tests/test_gate_population*.py"

DISPATCH_CONFIG = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}

# Every node here writes a check, so every node declares the mutation its check
# must fail against; the gate-population finding is the one under test.
DECLARED_MUTATION = (
    "removing the gate-population match check admits a pattern matching nothing"
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


@pytest.fixture()
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


@pytest.fixture()
def repository(config_home: Path, tmp_path: Path) -> Path:
    """A repository holding one test file and no file for ``MISSING_POPULATION``."""
    root = tmp_path / "repo"
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "fixture.html").write_text(
        "<!doctype html>\n<html><head>\n"
        f'<meta name="docs-project" content="{PROJECT}">\n'
        '<meta name="reckon-type" content="plan">\n'
        '<meta name="plan-slug" content="fixture">\n'
        '</head><body><h2 id="guard">Guard</h2></body></html>\n',
        encoding="utf-8",
    )
    (root / "tests").mkdir()
    (root / SEEDED_TEST).write_text("def test_seeded() -> None:\n    pass\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "worker@example.invalid")
    _git(root, "config", "user.name", "Worker")
    _git(root, "add", SEEDED_TEST, "docs/plans/fixture.html")
    _git(root, "commit", "-q", "-m", "test: seed the fixture repository")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _node(
    home: Path,
    *,
    done_when: str,
    write_paths: list[str] | None = None,
    node_id: str = "gate-population-node",
) -> crew.TaskNode:
    return crew.TaskNode(
        id=node_id,
        goal="land the gate population check and declare its mutation",
        plan="fixture",
        section="guard",
        spec_level="exact",
        done_when=done_when,
        write_paths=list(write_paths or [DECLARED_NODE_WRITE_PATH]),
        time_budget="20m",
        manifest_path=str(home / "manifests" / f"{node_id}.md"),
        negative_control=DECLARED_MUTATION,
    )


def _resolve(repo: Path, node: crew.TaskNode):
    return crew.plan_dispatch(
        node=node, project=PROJECT, repo=repo, config=DISPATCH_CONFIG
    )


# ── a population the repository does not hold is refused ────────────────────


def test_dispatch_refuses_a_gate_population_the_repository_lacks(
    config_home: Path, repository: Path
) -> None:
    """The guarded thing happens: the brief names a population nothing matches."""
    node = _node(
        config_home,
        done_when=f"pytest -q {MISSING_POPULATION} reports the refusal",
    )

    with pytest.raises(crew.CrewError) as refusal:
        crew.dispatch(
            node=node,
            project=PROJECT,
            repo=repository,
            config=DISPATCH_CONFIG,
            session="session-gate-population",
            launcher=lambda *args, **kwargs: pytest.fail(
                "the launcher ran, so the refusal did not land before a worktree"
            ),
        )

    message = str(refusal.value)
    assert MISSING_POPULATION in message, message
    # Nothing was created: no worktree, no live pointer.
    assert not list(crew.list_live(project=PROJECT))


def test_a_gate_population_matching_a_file_is_admitted(
    config_home: Path, repository: Path
) -> None:
    """The negative half: a glob the repository satisfies is not a defect."""
    node = _node(
        config_home,
        done_when=f"pytest -q {MATCHING_GLOB} reports zero failures",
    )

    resolution = _resolve(repository, node)

    assert resolution.validation.ok, resolution.validation.findings


# ── the verdict turns on what the repository holds, not on the spelling ─────


def test_the_missing_population_is_refused_while_the_matching_glob_is_admitted(
    config_home: Path, repository: Path
) -> None:
    """Two globs, one store: the difference is the repository, not the syntax."""
    refused = _resolve(
        repository,
        _node(
            config_home,
            done_when=f"pytest -q {MISSING_POPULATION} reports the refusal",
            node_id="missing-population-node",
        ),
    )
    admitted = _resolve(
        repository,
        _node(
            config_home,
            done_when=f"pytest -q {MATCHING_GLOB} reports zero failures",
            node_id="matching-population-node",
        ),
    )

    findings = list(refused.validation.findings)
    assert not refused.validation.ok
    assert any(
        finding["property"] == "gate-population"
        and MISSING_POPULATION in finding["detail"]
        for finding in findings
    ), findings
    assert admitted.validation.ok, admitted.validation.findings


# ── a node naming no population is unaffected ───────────────────────────────


def test_a_node_naming_no_population_is_unaffected(
    config_home: Path, repository: Path
) -> None:
    """A measure naming no path makes no claim about the repository."""
    node = _node(
        config_home,
        done_when="pytest reports one passing gate-population case",
    )

    resolution = _resolve(repository, node)

    assert resolution.validation.ok, resolution.validation.findings


def test_a_population_the_node_declares_it_will_write_is_admitted(
    config_home: Path, repository: Path
) -> None:
    """The files this node creates are absent by design, and declared as such."""
    node = _node(
        config_home,
        done_when=f"pytest -q {DECLARED_POPULATION} reports zero failures",
        write_paths=[DECLARED_NODE_WRITE_PATH],
    )

    resolution = _resolve(repository, node)

    assert resolution.validation.ok, resolution.validation.findings


def test_a_literal_path_matching_no_file_is_not_a_population(
    config_home: Path, repository: Path
) -> None:
    """A single file claim is not this finding's subject, and is not refused.

    A literal path the repository lacks fails inside the section of the worker's
    gate that runs it, so the worker sees pytest's own "file not found" beside
    the name it cannot find; the substitution this finding prevents is only
    possible where a subset of a population is silently dropped.
    """
    node = _node(
        config_home,
        done_when="pytest -q tests/test_absent_example.py reports zero failures",
    )

    resolution = _resolve(repository, node)

    assert resolution.validation.ok, resolution.validation.findings


def test_a_ratio_in_the_measure_is_not_read_as_a_population(
    config_home: Path, repository: Path
) -> None:
    """A measure reporting "110/135" names no files, so it is asked nothing."""
    node = _node(
        config_home,
        done_when="the print reads 110/135 cells and pytest reports zero failures",
    )

    resolution = _resolve(repository, node)

    assert resolution.validation.ok, resolution.validation.findings
