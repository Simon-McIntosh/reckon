"""The promotion command offered for a read-only run is one crew complete accepts.

A read-only run — an investigation that changed no repository file — records its
dispatch base as its only commit, because its manifest must name a commit and the
base revision is what its worktree sits on. The recovery classifier read that
citation back and offered ``crew complete ... --commit <base>``, which promotion
refuses: a citation of the base predates the run, so it would read as landed work
that is not the run's. Two surfaces of one system disagreed about what a
read-only run's promotion is.

The classifier now offers the commitless declaration instead — ``--gate not-run``,
``--no-commit`` and ``--outcome`` placeholders — when the run's only resolved
commit equals its dispatch base. The cases below take the offered command, fill
only its placeholders and run it through the CLI, and assert the run promotes; a
run with a commit of its own still gets its ``--commit`` list. The negative
control is the base revision's offer, which is refused for citing the base as
the run's own work.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import _plan_html, _store, cli, ledger
from reckon.crew import recovery, runs
from reckon.crew import review as review_module
from reckon.crew.runs import _write_json, pointer_path
from tests.conftest import EXECUTABLE_GATE_COMMAND

PROJECT = "offered-promotion-fixture"
PLAN = "offered-promotion-target"
READ_ONLY_RUN = "r-20261004T040000000000-read-only-census"
COMMITTING_RUN = "r-20261004T040000000001-committing-census"

# What the two placeholders of the fixed offer are filled with, and what the
# base revision's ``<verdict>`` spelling is filled with. The read-only fixture's
# gate check ran and passed; the offer still names ``not-run`` because that is
# the declaration this node hands the operator.
READ_ONLY_REASON = "read-only investigation; it changed no repository file"
READ_ONLY_OUTCOME = "the census report, written under the run directory"
PLACEHOLDER_FILLINGS = {
    "<verdict>": "passed",
    "<why the run produced no commit>": READ_ONLY_REASON,
    "<what the run produced>": READ_ONLY_OUTCOME,
}


def _git(tree: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=tree,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _write_plan(root: Path) -> Path:
    path = root / "docs" / "plans" / f"{PLAN}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    state = {
        "type": "plan",
        "slug": PLAN,
        "title": "Offered promotion target",
        "status": "active",
        "version": 0,
        "comments": {},
    }
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def real_crew_home_is_not_a_fixture_target() -> None:
    """No fixture may reach the workstation's real crew pointer directory."""
    real_live = Path.home() / ".config" / "reckon" / "crew" / "live"
    real_pointers = [
        real_live / f"{run_id}.json" for run_id in (READ_ONLY_RUN, COMMITTING_RUN)
    ]
    assert not any(path.exists() for path in real_pointers)
    yield
    assert not any(path.exists() for path in real_pointers)


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    _write_plan(root)
    (root / "docs" / "state" / PROJECT).mkdir(parents=True, exist_ok=True)
    (root / "candidate.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs", "candidate.txt"),
        ("commit", "-q", "-m", "test: seed repository"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _run_tree(repository: Path, tmp_path: Path, run_id: str) -> Path:
    base = _git(repository, "rev-parse", "HEAD")
    tree = tmp_path / "worktrees" / run_id
    tree.parent.mkdir(parents=True, exist_ok=True)
    _git(repository, "worktree", "add", "--detach", "--quiet", str(tree), base)
    return tree


def _manifest(
    tmp_path: Path,
    run_id: str,
    *,
    commits: str,
    changed_paths: str,
) -> Path:
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {run_id}\n"
        "status: complete\n"
        f"commits: [{commits}]\n"
        f"changed_paths: [{changed_paths}]\n"
        f"tests: {EXECUTABLE_GATE_COMMAND}\n",
        encoding="utf-8",
    )
    return manifest


def _pointer(
    repository: Path,
    run_id: str,
    manifest: Path,
    *,
    tree: Path,
    base_sha: str,
) -> None:
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(tree),
            "base_sha": base_sha,
            "process_alive": False,
            "launch": "in-harness",
            "role": "investigate",
            "backend": "local",
            "created_at": "2026-10-04T04:00:00Z",
            "manifest_path": str(manifest),
            "node": {
                "id": run_id,
                "plan": PLAN,
                "section": "read-only-census",
                "time_budget": "25m",
                "write_paths": [],
            },
        },
    )


def _store_review(run_id: str, *, base: str, head: str) -> Path:
    emitted = "\n".join(
        f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
    )
    record = review_module.parse_review(emitted)
    record.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": f"review-of-{run_id}",
            "reviewed_base_sha": base,
            "reviewed_head_sha": head,
        }
    )
    return review_module.store_review(record)


def _read_only_run(repository: Path, tmp_path: Path) -> tuple[Path, str]:
    """A promotable run whose only recorded commit is its dispatch base."""
    tree = _run_tree(repository, tmp_path, READ_ONLY_RUN)
    base = _git(repository, "rev-parse", "HEAD")
    report = tmp_path / "crew" / "reports" / f"{READ_ONLY_RUN}.html"
    _pointer(
        repository,
        READ_ONLY_RUN,
        _manifest(tmp_path, READ_ONLY_RUN, commits=base, changed_paths=str(report)),
        tree=tree,
        base_sha=base,
    )
    _store_review(READ_ONLY_RUN, base=base, head=base)
    return tree, base


def _committing_run(repository: Path, tmp_path: Path) -> tuple[Path, str, str]:
    """A promotable run whose worktree carries a commit beyond its base."""
    tree = _run_tree(repository, tmp_path, COMMITTING_RUN)
    base = _git(repository, "rev-parse", "HEAD")
    (tree / "candidate.txt").write_text("census repair\n", encoding="utf-8")
    _git(tree, "add", "--", "candidate.txt")
    _git(tree, "commit", "-q", "-m", "test: land the census repair")
    head = _git(tree, "rev-parse", "HEAD")
    _pointer(
        repository,
        COMMITTING_RUN,
        _manifest(
            tmp_path, COMMITTING_RUN, commits=head, changed_paths="candidate.txt"
        ),
        tree=tree,
        base_sha=base,
    )
    _store_review(COMMITTING_RUN, base=base, head=head)
    return tree, base, head


def _offered_argv(action: str) -> list[str]:
    """The offered command as CLI argv, with only its placeholders filled."""
    tokens = shlex.split(action)
    assert tokens and tokens[0] == "reckon", action
    argv = []
    for token in tokens[1:]:
        if token.startswith("<") and token.endswith(">"):
            assert token in PLACEHOLDER_FILLINGS, (
                "the offered command carries a placeholder this test does not "
                f"know how to fill: {token!r}"
            )
            argv.append(PLACEHOLDER_FILLINGS[token])
        else:
            argv.append(token)
    return argv


def test_the_offered_command_for_a_read_only_run_promotes_it(
    repository: Path, tmp_path: Path
) -> None:
    """The offer for a run whose only commit is its base is runnable as offered.

    The command is taken from the classifier, filled only at its placeholders,
    and run through the CLI. The exit status is asserted before the offer's own
    shape so a base revision's offer — which fills, runs, and is refused for
    citing the base — reports that refusal here rather than a shape mismatch.
    """
    _tree, base = _read_only_run(repository, tmp_path)
    row = recovery.classify_pointer(runs.read_pointer(READ_ONLY_RUN))
    assert row["classification"] == "promotable"

    action = str(row["next_action"])
    result = CliRunner().invoke(cli.main, _offered_argv(action))

    assert result.exit_code == 0, (
        f"the offered command `{action}` exited {result.exit_code}:\n{result.output}"
    )

    assert "--commit" not in action
    assert base not in action
    assert "--gate not-run" in action
    assert "--no-commit" in action and "--outcome" in action

    assert not pointer_path(READ_ONLY_RUN).exists()
    rows = ledger.runs(PROJECT, root=repository)
    assert [entry["run_id"] for entry in rows] == [READ_ONLY_RUN]
    assert rows[0]["commits"] == []
    assert rows[0]["no_commit"] == READ_ONLY_REASON

    # The narrative the ``--outcome`` placeholder carried lands as the run's
    # landing comment on the plan, which is where the row defers it to; the
    # row's own outcome field is empty because the comment holds the text.
    state, _version = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    bodies = [
        str(item.get("body") or "")
        for items in (state.get("comments") or {}).values()
        for item in items
    ]
    assert any(READ_ONLY_OUTCOME in body for body in bodies), bodies
    assert rows[0]["outcome"] == ""


def test_a_run_with_commits_beyond_its_base_still_gets_its_commit_list(
    repository: Path, tmp_path: Path
) -> None:
    """A run with work of its own keeps the citation offer, not the declaration."""
    _tree, _base, head = _committing_run(repository, tmp_path)
    row = recovery.classify_pointer(runs.read_pointer(COMMITTING_RUN))
    assert row["classification"] == "promotable"

    action = str(row["next_action"])

    assert f"--commit {head}" in action
    assert "--no-commit" not in action
    assert "--outcome" not in action
