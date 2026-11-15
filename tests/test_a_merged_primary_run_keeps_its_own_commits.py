"""A landing attributes the run's first-parent history, including its merge."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew import recovery
from reckon.crew import review as review_module
from reckon.crew.obligations import obligations
from reckon.crew.runs import _write_json, pointer_path
from tests.helpers.git import git

PROJECT = "merged-history"
SESSION = "history-owner"
FILE = "reckon/result.py"


def _commit(tree: Path, path: str, text: str) -> str:
    target = tree / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text + "\n")
    git(tree, "add", path)
    git(tree, "commit", "-qm", "test: record content", "-m", "Record fixture content.")
    return git(tree, "rev-parse", "HEAD")


@pytest.fixture
def merged_run(tmp_path, monkeypatch):
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    repository = tmp_path / "repo"
    (repository / "docs" / "state" / PROJECT).mkdir(parents=True)
    git(repository, "init", "-q", "-b", "main")
    git(repository, "config", "user.name", "Test User")
    git(repository, "config", "user.email", "test@example.invalid")
    _commit(repository, FILE, "seed")
    (home / "mounts.json").write_text(json.dumps({PROJECT: str(repository / "docs")}))
    tree = tmp_path / "worker"
    git(repository, "worktree", "add", "-q", "--detach", str(tree), "HEAD")
    # Dispatch can start from a promoted predecessor not yet merged into main.
    base = _commit(tree, "predecessor.txt", "dispatch base")
    own = [_commit(tree, FILE, value) for value in ("first", "second")]
    primary = [_commit(repository, "primary.txt", str(number)) for number in range(40)]
    git(
        tree,
        "merge",
        "--no-ff",
        "main",
        "-m",
        "test: merge primary",
        "-m",
        "Refresh primary content.",
    )
    own.append(git(tree, "rev-parse", "HEAD"))
    own.append(_commit(tree, FILE, "last"))
    assert len(git(tree, "rev-list", f"{base}..HEAD").splitlines()) == 44
    assert len(primary) == 40
    assert git(tree, "show", "HEAD:primary.txt") == "39"
    run_id = "r-merged-history"
    manifest = tmp_path / "manifest.md"
    log = tmp_path / "gate.log"
    log.write_text("1 passed in 0.01s\nEXIT=0\n")
    manifest.write_text(
        "node: merged-history\nstatus: complete\n"
        f"commits: {json.dumps(own)}\nchanged_paths: [{FILE}]\n"
        "tests: python -m pytest tests/test_history.py\n"
        f"test_logs: [{log}]\n"
    )
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
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
            "id": "merged-history",
            "plan": "fixture",
            "section": "history",
            "time_budget": "25m",
            "write_paths": [FILE],
        },
    }
    _write_json(pointer_path(run_id), record)
    review = review_module.parse_review(
        "\n".join(
            f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
        )
    )
    review.update(
        project=PROJECT,
        reviewed_run_id=run_id,
        reviewed_base_sha=base,
        reviewed_head_sha=own[-1],
    )
    review_module.store_review(review)
    return repository, tree, record, own, primary, manifest


def _offered_commits(row):
    words = shlex.split(row["next_command"])
    return [words[index + 1] for index, word in enumerate(words) if word == "--commit"]


def test_one_landing_call_records_only_the_runs_line_of_history(merged_run):
    repository, _tree, record, own, primary, _manifest = merged_run
    result = crew.complete(record["run_id"], gate="passed", root=repository)
    assert result["record"]["commits"] == own
    assert result["record"]["promoted_revision"] == own[-1]
    rows = ledger.runs(PROJECT, root=repository)
    assert len(rows) == 1
    assert rows[0]["commits"] == own
    assert not set(primary).intersection(rows[0]["commits"])


def test_obligations_offer_exactly_the_runs_commits_and_merge(merged_run):
    _repository, _tree, record, own, _primary, _manifest = merged_run
    rows = obligations(PROJECT, SESSION)["obligations"]
    row = next(row for row in rows if row["run_id"] == record["run_id"])
    assert _offered_commits(row) == own
    assert recovery.classify_pointer(record)["commits_beyond_base"] == len(own)


@pytest.mark.parametrize("extra_primary", [False, True])
def test_manifest_accepts_owned_commits_and_ignores_imported_primary(
    merged_run, extra_primary
):
    repository, _tree, record, own, primary, manifest = merged_run
    citations = own + ([primary[13]] if extra_primary else [])
    text = manifest.read_text().replace(json.dumps(own), json.dumps(citations))
    manifest.write_text(text)
    result = crew.complete(record["run_id"], gate="passed", root=repository)
    assert result["record"]["commits"] == own
    assert ledger.runs(PROJECT, root=repository)[0]["commits"] == own


def test_a_manifest_citing_an_unmerged_branch_is_refused(merged_run, tmp_path):
    repository, _tree, record, own, _primary, manifest = merged_run
    other = tmp_path / "unmerged"
    git(repository, "worktree", "add", "-q", "--detach", str(other), record["base_sha"])
    foreign = _commit(other, "foreign.txt", "never merged")
    manifest.write_text(
        manifest.read_text().replace(json.dumps(own), json.dumps([*own, foreign]))
    )
    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(record["run_id"], gate="passed", root=repository)
    assert foreign in str(refusal.value)
    assert ledger.runs(PROJECT, root=repository) == []
    assert pointer_path(record["run_id"]).exists()
