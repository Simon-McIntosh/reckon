"""A landing refused under one rule is judged again when that rule changes."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon.crew import queue_landing, runs
from reckon.crew import review as review_module

PROJECT = "landing-fixture"
SESSION = "owner"
RUN = "r-refused-implementation"


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture()
def delivery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str | Path]:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "worker@example.invalid")
    _git(repo, "config", "user.name", "Worker")
    plan = repo / "docs" / "plans" / "delivery-plan.html"
    plan.parent.mkdir(parents=True)
    plan.write_text(
        '<!doctype html><meta name="docs-project" content="landing-fixture">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="delivery-plan">'
        '<meta name="plan-status" content="active">'
        '<h2 id="s2">Downstream</h2>\n',
        encoding="utf-8",
    )
    state = repo / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}),
        encoding="utf-8",
    )
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    (repo / "marker.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "marker.txt", "docs")
    _git(repo, "commit", "-q", "-m", "chore: seed fixture")
    base = _git(repo, "rev-parse", "HEAD")
    revisions = []
    for step in ("first", "second", "third"):
        (repo / "marker.txt").write_text(f"{step}\n", encoding="utf-8")
        _git(repo, "add", "marker.txt")
        _git(repo, "commit", "-q", "-m", f"feat: deliver {step}")
        revisions.append(_git(repo, "rev-parse", "HEAD"))
    head = revisions[-1]
    return {
        "repo": repo,
        "base": base,
        "head": head,
        "revisions": revisions,
    }


def _manifest(commit_entries: list[str]) -> None:
    manifest = runs.run_dir(RUN) / runs.DEFAULT_MANIFEST_NAME
    manifest.parent.mkdir(parents=True, exist_ok=True)
    lines = "".join(f"  - {entry}\n" for entry in commit_entries)
    manifest.write_text(
        f"node: implementation\nstatus: complete\ncommits:\n{lines}",
        encoding="utf-8",
    )


def _pointer(repo: Path, head: str) -> None:
    manifest = runs.run_dir(RUN) / runs.DEFAULT_MANIFEST_NAME
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
            "node": {"id": "implementation", "write_paths": ["marker.txt"]},
        },
    )


def _review(head: str, *, scores: int = 20) -> None:
    text = "\n".join(
        f"SCORE {dimension}: {scores}" for dimension in review_module.REVIEW_DIMENSIONS
    )
    parsed = review_module.parse_review(text)
    parsed.update(
        {
            "project": PROJECT,
            "reviewed_run_id": RUN,
            "review_run_id": "review-of-refused",
            "reviewed_head_sha": head,
        }
    )
    review_module.store_review(parsed)


def _incomplete_review(head: str) -> None:
    """A record the acceptance rule cannot read, so it refuses the landing."""

    parsed = review_module.parse_review("SCORE durability: 20\n")
    parsed.update(
        {
            "project": PROJECT,
            "reviewed_run_id": RUN,
            "review_run_id": "review-of-refused",
            "reviewed_head_sha": head,
        }
    )
    review_module.store_review(parsed)


def _land_stub(calls: list[str]):
    def land(run_id: str) -> tuple[bool, str]:
        calls.append(run_id)
        return True, ""

    return land


def test_a_refusal_under_an_old_rule_is_judged_again_when_the_code_moves(
    delivery: dict[str, str | Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    head = str(delivery["head"])
    _manifest([head])
    _pointer(Path(delivery["repo"]), head)
    _incomplete_review(head)
    monkeypatch.setattr(queue_landing, "_land", lambda run_id: (False, "not reached"))
    monkeypatch.setattr(queue_landing, "_landing_code_stamp", lambda: "old-rule")

    first = queue_landing.land_once()
    refused = runs.read_pointer(RUN)["admission_landing"]

    assert first["landed"] == []
    assert "review acceptance" in refused["guard"]
    assert refused["code_stamp"] == "old-rule"
    assert refused["review_stamp"]

    calls: list[str] = []
    monkeypatch.setattr(queue_landing, "_land", _land_stub(calls))
    monkeypatch.setattr(queue_landing, "_landing_code_stamp", lambda: "new-rule")
    monkeypatch.setattr(queue_landing, "_review_accepts_promotion", lambda _r: True)

    second = queue_landing.land_once()

    assert second["landed"] == [RUN]
    assert calls == [RUN]


def test_an_unchanged_refusal_is_not_judged_a_second_time(
    delivery: dict[str, str | Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    head = str(delivery["head"])
    _manifest([head])
    _pointer(Path(delivery["repo"]), head)
    _incomplete_review(head)
    judged: list[object] = []
    real = queue_landing._review_accepts_promotion

    def counting(review):
        judged.append(review)
        return real(review)

    monkeypatch.setattr(queue_landing, "_review_accepts_promotion", counting)
    monkeypatch.setattr(queue_landing, "_land", lambda run_id: (False, "not reached"))
    monkeypatch.setattr(queue_landing, "_landing_code_stamp", lambda: "same-rule")

    first = queue_landing.land_once()
    second = queue_landing.land_once()

    assert first["obligations"], first
    assert second == {"landed": [], "obligations": [], "deferred": []}
    assert len(judged) == 1
    assert runs.read_pointer(RUN)["admission_landing"]["status"] == "refused"


def test_a_refusal_with_no_recorded_stamps_is_judged_once_more(
    delivery: dict[str, str | Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    head = str(delivery["head"])
    _manifest([head])
    _pointer(Path(delivery["repo"]), head)
    _review(head)

    def stamp_previous(pointer: dict) -> dict:
        pointer["admission_landing"] = {
            "head": head,
            "guard": "review acceptance floor or findings",
            "status": "refused",
        }
        return pointer

    runs._mutate_pointer(RUN, stamp_previous)
    calls: list[str] = []
    monkeypatch.setattr(queue_landing, "_land", _land_stub(calls))

    result = queue_landing.land_once()

    assert result["landed"] == [RUN]
    assert calls == [RUN]


@pytest.mark.parametrize("entry_shape", ["subject", "joined"])
def test_a_commit_entry_spelling_more_than_one_revision_reaches_its_head(
    delivery: dict[str, str | Path],
    monkeypatch: pytest.MonkeyPatch,
    entry_shape: str,
) -> None:
    head = str(delivery["head"])
    revisions = [str(revision) for revision in delivery["revisions"]]
    if entry_shape == "subject":
        entry = f"{head} docs(evidence): record the section-wait repair"
    else:
        entry = " ".join(revisions)
    _manifest([entry])
    _pointer(Path(delivery["repo"]), head)
    _review(head)
    judged: list[object] = []
    calls: list[str] = []

    def counting(review):
        judged.append(review)
        return True

    monkeypatch.setattr(queue_landing, "_review_accepts_promotion", counting)
    monkeypatch.setattr(queue_landing, "_land", _land_stub(calls))

    result = queue_landing.land_once()

    assert result["landed"] == [RUN]
    assert [review["reviewed_head_sha"] for review in judged] == [head]
