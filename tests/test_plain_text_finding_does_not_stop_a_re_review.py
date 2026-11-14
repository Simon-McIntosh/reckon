"""A review that stored its findings as plain text does not stop a re-review.

A stored review may record its findings as bare strings such as
``"non-blocking: reckon/crew/promotion.py:291 ..."`` rather than as records
carrying a ``severity`` field. Reading such a finding's severity through the
record shape raises ``AttributeError: 'str' object has no attribute '....'``,
which the reflex's review pass reads as a failed composition and drops on every
sweep, so the run can never be reviewed again. The disposition writer must read
the same findings without falling over.

Both cases run through the composer the reflex dispatches with and the store's
own disposition writer, against a real git history, so the text asserted on disk
is the brief a reviewer is handed rather than one the test assembles.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reckon.crew import findings as findings_module
from reckon.crew import recovery, recovery_review_subject, review_dispositions, runs
from reckon.crew import review as review_module

PROJECT = "plain-text-finding-fixture"
SESSION = "coordinator-fixture"
RUN = "r-reviewed-once-plain-text"

LOCAL_BACKEND = "clive"
OTHER_BACKEND = "delta"

pytestmark = pytest.mark.arms_watch_producer

BLOCKING_CLAIM = "blocking: the guard swallows its own error"
NON_BLOCKING_CLAIM = "non-blocking: the helper repeats the score map"

FOLDED_INTO = "repair-the-guard"
EXEMPTION_REASON = "folds into the metrics node"


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


def _store_earlier_review(run_id: str, *, base: str, head: str) -> dict:
    """Store the earlier review with both findings written as bare strings."""
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
            "findings": [BLOCKING_CLAIM, NON_BLOCKING_CLAIM],
            review_module.FINDING_DISPOSITIONS_KEY: {
                "0": {"kind": "folded", "node": FOLDED_INTO},
                "1": {"kind": "exempted", "reason": EXEMPTION_REASON},
            },
        }
    )
    # Written through the store's own writer rather than ``store_review``,
    # because ``store_review`` normalises a bare-string finding into a record
    # and the production record this repairs was written as plain text.
    path = review_module.review_path(PROJECT, run_id, reviewed_head_sha=head)
    path.parent.mkdir(parents=True, exist_ok=True)
    review_module._write_record(path, stored)
    return stored


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
    repair = _commit(repo, {"pkg/mod.py": "VALUE = 2\n"}, "fix: stop swallowing it")
    _git(repo, "update-ref", "refs/remotes/origin/main", reviewed)
    return {
        "config_home": config_home,
        "repo": repo,
        "reviewed": reviewed,
        "repair": repair,
    }


def _compose_the_re_review(world: dict) -> str:
    """Compose the brief the reflex composes for a run it has already reviewed."""
    record = runs.read_pointer(RUN)
    move = recovery.review_head_move(record)
    assert move and move["changes_runtime_source"] is True, move
    fields = recovery._review_dispatch_fields(record, delta=move)
    assert fields["delta_base"] == world["reviewed"]
    return Path(fields["brief"]).read_text(encoding="utf-8")


def test_a_re_review_brief_lists_plain_text_findings_with_severities(world):
    """Both bare-string findings reach the brief with the severity each states."""
    text = _compose_the_re_review(world)

    assert recovery_review_subject.RE_REVIEW_HEADING in text
    assert f"Previously reviewed head: {world['reviewed']}" in text
    assert f"finding 0 [blocking]: {BLOCKING_CLAIM} — folded:{FOLDED_INTO}" in text
    assert (
        f"finding 1 [non-blocking]: {NON_BLOCKING_CLAIM} — "
        f"exempted:{EXEMPTION_REASON}" in text
    )


def test_a_plain_text_finding_is_disposable(world):
    """Disposing the non-blocking plain-text finding records its disposition."""
    review_dispositions.record_finding_disposition(
        PROJECT, RUN, 1, kind="exempted", reason="judged harmless"
    )

    path, record = review_module.stored_record(
        PROJECT, RUN, reviewed_head_sha=world["reviewed"]
    )
    assert path is not None and record is not None
    assert findings_module.finding_disposition(record, 1) == "exempted:judged harmless"
