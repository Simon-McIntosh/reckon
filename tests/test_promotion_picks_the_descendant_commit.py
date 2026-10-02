"""A promotion promotes the presented commit that descends from all the others.

A run's manifest lists the commits it made in the order its worker wrote them,
which is usually newest first, and a promotion that read a position out of that
list asserted a revision the run never reached whenever the tip was printed
first. The promoted revision is instead selected by descent: of the commits a
promotion presents, the one that every other presented commit is an ancestor of
is the run's tip, whatever order the list is in and however each entry is
spelled.

These assertions hold both orders to the same head, refuse a presentation whose
commits have no single descendant while naming every presented commit, and run
the obligation's own printed remedy line as the operator would.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import crew, ledger
from reckon.cli import main as cli_main
from reckon.crew import recovery, runs
from reckon.crew import review as review_module
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "descendant"
# The run must change runtime source, so a review of it is required at all: a
# run touching only text resolves to the tier that owes no review, skipping the
# review gate this selection feeds.
FILE = "reckon/region.py"


def _git(tree: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=tree,
        check=True,
        capture_output=True,
        text=True,
        env=dict(os.environ),
    )
    return completed.stdout.strip()


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / FILE).parent.mkdir(parents=True, exist_ok=True)
    (root / FILE).write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    _git(root, "add", FILE)
    _git(root, "commit", "-q", "-m", "chore: seed")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _commit(tree: Path, text: str) -> str:
    target = tree / FILE
    target.write_text(f"{text}\n{target.read_text(encoding='utf-8')}", encoding="utf-8")
    _git(tree, "add", FILE)
    _git(tree, "commit", "-q", "-m", "test: " + text)
    return _git(tree, "rev-parse", "HEAD")


def _run_tree(repository: Path, tmp_path: Path, run_id: str) -> Path:
    path = tmp_path / f"{run_id}-tree"
    _git(repository, "worktree", "add", "-q", "--detach", str(path), "HEAD")
    return path


def _linear_run(
    repository: Path, tmp_path: Path, run_id: str
) -> tuple[Path, str, str, str]:
    """A run tree carrying a parent commit and a child commit over a base."""
    base = _git(repository, "rev-parse", "HEAD")
    tree = _run_tree(repository, tmp_path, run_id)
    parent = _commit(tree, "first")
    head = _commit(tree, "second")
    return tree, base, parent, head


def _manifest(tmp_path: Path, run_id: str, commits: list[str]) -> Path:
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    log_name = f"{run_id}-focus.log"
    (manifest.parent / log_name).write_text(
        "focused check passed\nEXIT=0\n", encoding="utf-8"
    )
    manifest.write_text(
        f"node: {PROJECT}-node\n"
        "status: complete\n"
        f"commits: [{', '.join(commits)}]\n"
        f"changed_paths: [{FILE}]\n"
        "tests: pytest -q tests/test_promotion_picks_the_descendant_commit.py\n"
        f"test_logs: [{log_name}]\n",
        encoding="utf-8",
    )
    return manifest


def _pointer(
    repository: Path, tree: Path, run_id: str, base: str, manifest: Path
) -> None:
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(tree),
            "base_sha": base,
            "process_alive": False,
            "launch": "in-harness",
            "role": "implement",
            "backend": "local",
            "created_at": "2026-01-01T00:00:01Z",
            "manifest_path": str(manifest),
            "node": {
                "id": f"{PROJECT}-node",
                "plan": "fixture",
                "section": "selection",
                "time_budget": "25m",
                "write_paths": [FILE],
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
        }
    )
    record["reviewed_base_sha"] = base
    record["reviewed_head_sha"] = head
    return review_module.store_review(record)


# (1) The tip printed first, the order position-based selection got wrong.


def test_a_newest_first_presentation_promotes_the_reviewed_head(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-newest-first"
    tree, base, parent, head = _linear_run(repository, tmp_path, run_id)
    manifest = _manifest(tmp_path, run_id, [head, parent])
    _pointer(repository, tree, run_id, base, manifest)
    _store_review(run_id, base=base, head=head)

    result = crew.complete(
        run_id, gate="passed", commits=[head, parent], root=repository
    )

    row = result["record"]
    assert row["promoted_revision"] == head
    assert row["commits"] == [head, parent]


# (2) The same commits oldest first promote the same head.


def test_an_oldest_first_presentation_promotes_the_same_head(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-oldest-first"
    tree, base, parent, head = _linear_run(repository, tmp_path=tmp_path, run_id=run_id)
    manifest = _manifest(tmp_path, run_id, [parent, head])
    _pointer(repository, tree, run_id, base, manifest)
    _store_review(run_id, base=base, head=head)

    result = crew.complete(
        run_id, gate="passed", commits=[parent, head], root=repository
    )

    row = result["record"]
    assert row["promoted_revision"] == head
    assert row["commits"] == [parent, head]


# (3) A presentation with no single descendant is refused, naming every commit.


def test_commits_on_divergent_branches_are_refused(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-divergent"
    base = _git(repository, "rev-parse", "HEAD")
    tree = _run_tree(repository, tmp_path, run_id)
    head = _commit(tree, "first")
    # A second commit line off the base, so the presented pair has no common
    # descendant: neither tip descends from the other.
    _git(tree, "checkout", "-q", "-b", f"divergent-{run_id}", base)
    other = _commit(tree, "divergent")
    manifest = _manifest(tmp_path, run_id, [head, other])
    _pointer(repository, tree, run_id, base, manifest)
    _store_review(run_id, base=base, head=head)

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(run_id, gate="passed", commits=[head, other], root=repository)

    message = str(refusal.value)
    assert "no single descendant" in message
    assert head[:12] in message
    assert other[:12] in message
    # Nothing landed, and the live pointer survives for the corrected attempt.
    assert ledger.runs(PROJECT, root=repository) == []
    assert pointer_path(run_id).exists()


# (4) The obligation's printed remedy line, run as printed, is accepted.


def test_the_obligation_remedy_line_runs_as_printed(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-remedy-line"
    tree, base, parent, head = _linear_run(repository, tmp_path, run_id)
    # The worker's manifest writes its commits newest first, which is the order
    # the obligation prints them in.
    manifest = _manifest(tmp_path, run_id, [head, parent])
    _pointer(repository, tree, run_id, base, manifest)
    _store_review(run_id, base=base, head=head)

    record = runs.read_pointer(run_id)
    assert recovery.classify_pointer(record)["classification"] == "promotable"

    action = str(recovery.classify_pointer(record)["next_action"])
    tokens = shlex.split(action)
    assert tokens[:3] == ["reckon", "crew", "complete"]
    cited = [tokens[i + 1] for i, part in enumerate(tokens) if part == "--commit"]
    # The line prints the commits newest first, the order the promotion read a
    # position out of and asserted the wrong revision for.
    assert cited == [head, parent]

    # Run it as printed: the leading program name is dropped because the
    # operator already ran it, and the documented <verdict> placeholder is the
    # one value the operator supplies. The commit order is not touched.
    argv = tokens[1:]
    argv[argv.index("--gate") + 1] = "passed"
    result = CliRunner().invoke(cli_main, argv)

    assert result.exit_code == 0, result.output
    rows = ledger.runs(PROJECT, root=repository)
    assert rows[-1]["promoted_revision"] == head
