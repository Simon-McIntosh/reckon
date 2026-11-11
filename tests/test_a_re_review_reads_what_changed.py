"""A run's second review reads what changed since the review before it.

A run that is reviewed, repaired and reviewed again is composed a brief by the
same reflex that composes its first review's. A reviewer handed the whole run
again spends its turn reading work another reviewer already judged, and says
nothing about whether the repair answered what that reviewer found. So a
re-review's brief names the head the earlier review read, the commits the run
gained since, and each finding that review raised with the disposition recorded
against it, and asks for every earlier blocking finding whether it is resolved.
A first review's brief carries none of that.

The re-review cases are driven from the reflex's own head-move decision and
composed through the composer the reflex dispatches with, against a real git
history, so the text they assert on disk is the brief a composed dispatch hands
a reviewer rather than a text the test assembles itself.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from reckon.crew import recovery, recovery_review_subject, runs
from reckon.crew import review as review_module

PROJECT = "re-review-fixture"
SESSION = "coordinator-fixture"
RUN = "r-repaired-twice"

LOCAL_BACKEND = "clive"
OTHER_BACKEND = "delta"

pytestmark = pytest.mark.arms_watch_producer


def _backend(command: str) -> dict:
    return {
        "launch": "cli",
        "command": command,
        "model": "some-model",
        "effort": "high",
        "sandbox": "worktree-full",
        "session_reuse": True,
        "time_budget": "25m",
    }


CONFIG = {
    "default_backend": LOCAL_BACKEND,
    "local_backend": LOCAL_BACKEND,
    "backends": {
        LOCAL_BACKEND: _backend("codex"),
        OTHER_BACKEND: _backend("claude"),
    },
    "roles": {"implement": {}, "review": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}

# The earlier review's two findings and the answers recorded against each,
# spelled here rather than read from the composer, so the expectation is
# independent of the rendering the code under test builds.
BLOCKING_FOLDED_FINDING = "the guard swallows its own error"
EXEMPTED_FINDING = "the helper repeats the score map"
EXEMPTION_REASON = "folds into the metrics node"

# The node the earlier review's blocking finding was folded into when the run
# was repaired. A blocking finding left undisposed refuses a review dispatch,
# so the fold is what lets the re-review compose at all.
FOLDED_INTO = "repair-the-guard"

# What the reviewer is asked to answer about the earlier review's findings.
RESOLUTION_ASK = "whether the new head resolves it"

# A commit line in the composed block: two leading spaces and a full revision.
COMMIT_LINE = re.compile(r"^  ([0-9a-f]{40}) ")


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _commit(repository: Path, paths: dict[str, str], message: str) -> str:
    for name, body in paths.items():
        target = repository / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    subprocess.run(
        ["git", "add", *paths], cwd=repository, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", message],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    return _git(repository, "rev-parse", "HEAD")


def _store_earlier_review(run_id: str, *, base: str, head: str) -> None:
    """Store the review the run already had, with two findings and one answer."""
    emitted = "\n".join(
        f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
    )
    stored = review_module.parse_review(emitted)
    stored.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": f"review-of-{run_id}",
            "reviewed_base_sha": base,
            "reviewed_head_sha": head,
            "added_failure_count": 0,
            "added_failure_ids": [],
            "findings": [
                {
                    "file": "pkg/mod.py",
                    "line": 1,
                    "severity": "blocking",
                    "text": BLOCKING_FOLDED_FINDING,
                },
                {
                    "file": "pkg/mod.py",
                    "line": 2,
                    "severity": "non-blocking",
                    "text": EXEMPTED_FINDING,
                },
            ],
            review_module.FINDING_DISPOSITIONS_KEY: {
                "0": {"kind": "folded", "node": FOLDED_INTO},
                "1": {"kind": "exempted", "reason": EXEMPTION_REASON},
            },
        }
    )
    review_module.store_review(stored)


def _write_run(config_home: Path, repo: Path, *, head: str) -> None:
    manifest = config_home / "manifests" / f"{RUN}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {RUN}\nstatus: complete\ncommits: [{head}]\n", encoding="utf-8"
    )
    runs._write_json(
        runs.pointer_path(RUN),
        {
            "run_id": RUN,
            "project": PROJECT,
            "session": SESSION,
            "repo": str(repo),
            "worktree": str(repo),
            "process_alive": False,
            "launch": "in-harness",
            "role": "implement",
            "manifest_path": str(manifest),
            "node": {
                "id": RUN,
                "plan": "fixture-plan",
                "write_paths": ["pkg/mod.py"],
                "goal": "expose the reviewed value",
                "done_when": "the value reads 3 and its test passes",
            },
        },
    )


@pytest.fixture()
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A repository, an isolates crew home and a run repaired by two commits."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    repo = tmp_path / "repo"
    repo.mkdir()
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@invalid.example"),
        ("config", "user.name", "Worker"),
    ):
        _git(repo, *arguments)
    seed = _commit(repo, {"seed.txt": "seed\n"}, "chore: seed")
    reviewed = _commit(
        repo, {"pkg/mod.py": "VALUE = 1\n"}, "feat: the change under review"
    )
    monkeypatch.setattr(
        review_module,
        "review_store_root",
        lambda base_dir=None: config_home / "reviews",
    )
    _write_run(config_home, repo, head=reviewed)
    _store_earlier_review(RUN, base=seed, head=reviewed)
    first_repair = _commit(
        repo, {"pkg/mod.py": "VALUE = 2\n"}, "fix: stop swallowing the guard error"
    )
    second_repair = _commit(
        repo, {"pkg/mod.py": "VALUE = 3\n"}, "fix: reuse the score map"
    )
    # The integration branch holds what the run was reviewed on, so the two
    # repairs above are the run's own commits and the head move reads them as
    # its delta. Without a remote-tracking ref the history is uncomparable and
    # the reflex falls through to a full review.
    _git(repo, "update-ref", "refs/remotes/origin/main", reviewed)
    return {
        "config_home": config_home,
        "repo": repo,
        "reviewed": reviewed,
        "repairs": [first_repair, second_repair],
    }


def _compose_the_re_review(world: dict) -> dict:
    """Compose the brief the reflex composes for a run it has already reviewed.

    The move is the reflex's own decision, read through the same call the reflex
    makes before it composes, and the fields are composed with it exactly as
    :func:`reckon.crew.recovery.dispatch_review_for_run` composes them. The
    launch itself is not exercised here: a backend launch preflight fails on
    this workstation independent of any change, so a test that ran it would
    measure the sandbox rather than the brief.
    """
    record = runs.read_pointer(RUN)
    move = recovery.review_head_move(record)
    assert move and move["changes_runtime_source"] is True, move
    fields = recovery._review_dispatch_fields(record, delta=move)
    assert fields["review_tier"] == "light"
    assert fields["delta_base"] == world["reviewed"]
    return fields


def _brief_text(world: dict) -> str:
    return Path(_compose_the_re_review(world)["brief"]).read_text(encoding="utf-8")


def test_a_re_review_brief_names_the_head_the_earlier_review_read(world):
    """The composed dispatch names the revision the earlier review stood on."""
    text = _brief_text(world)

    assert f"Previously reviewed head: {world['reviewed']}" in text
    assert recovery_review_subject.RE_REVIEW_HEADING in text


def test_a_re_review_brief_names_exactly_the_commits_since_that_head(world):
    """The block lists the two repair commits and no other commit."""
    text = _brief_text(world)

    listed = [
        match.group(1)
        for match in map(COMMIT_LINE.match, text.splitlines())
        if match is not None
    ]

    assert listed == world["repairs"]
    assert "Commits since that head (2):" in text


def test_a_re_review_brief_names_earlier_findings_with_dispositions(world):
    """Each earlier finding is quoted with the answer recorded against it."""
    text = _brief_text(world)

    assert (
        f"  finding 0 [blocking] pkg/mod.py:1: {BLOCKING_FOLDED_FINDING} — "
        f"folded:{FOLDED_INTO}" in text
    )
    assert (
        f"  finding 1 [non-blocking] pkg/mod.py:2: {EXEMPTED_FINDING} — "
        f"exempted:{EXEMPTION_REASON}" in text
    )
    assert RESOLUTION_ASK in text


def test_a_first_review_brief_names_no_earlier_review(world):
    """The same run composed without the move carries the block not at all."""
    record = runs.read_pointer(RUN)

    text = recovery._review_dispatch_fields(record)["brief_text"]

    assert recovery_review_subject.RE_REVIEW_HEADING not in text
    assert "Previously reviewed head:" not in text
    assert BLOCKING_FOLDED_FINDING not in text
    assert EXEMPTED_FINDING not in text
    assert RESOLUTION_ASK not in text
    # The head this review reads is the run's current head, which is the second
    # repair. A first review names that and only that; the intermediate repair
    # belongs to the earlier review's range and to no first review's brief.
    assert world["repairs"][0] not in text
    assert not [
        match
        for match in map(COMMIT_LINE.match, text.splitlines())
        if match is not None
    ]
