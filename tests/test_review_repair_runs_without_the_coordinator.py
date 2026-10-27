"""A stored finding carries the id an answer names, from the moment it is stored.

The reflex answers a stored finding by identity: a repair says which finding it
acted on, and the lifecycle reads that answer back. A finding stored without an
id can be answered by nothing, so this file pins the identity to the record on
disk rather than to a reader that re-derives it. The id is the shared
``repair.finding_id`` function of the finding's own file, line and text, so the
stored id and the id a repair brief prints are one value.

A stored review is keyed to the head it read, so a disposition naming the run
rather than that head must be accepted only when the run's current head descends
from it: an ancestor check is what distinguishes a review of this work from the
same run's earlier or divergent revision. And the re-review brief a later round
composes carries the diff from the head last reviewed to the new one, so the
repair reads what changed rather than re-reading the whole review.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from reckon.crew import repair as repair_module
from reckon.crew import review as review_module
from reckon.crew import review_lifecycle, review_records

PROJECT = "sample"
RUN_ID = "r-reviewed"
FINDING = {
    "file": "reckon/crew/thing.py",
    "line": "10",
    "text": "off-by-one in the loop",
}


def _record(findings: list[dict]) -> dict:
    return {
        "project": PROJECT,
        "reviewed_run_id": RUN_ID,
        "status": "parsed",
        "scores": dict(zip(review_module.REVIEW_DIMENSIONS, (10,) * 6, strict=True)),
        "total": 60,
        "findings": findings,
    }


def _store(tmp_path: Path, monkeypatch, findings: list[dict]) -> dict:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    path = review_module.store_review(_record(findings))
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_stored_review_s_findings_carry_the_shared_id(tmp_path, monkeypatch) -> None:
    """The record on disk carries the id ``repair.finding_id`` derives for it."""
    stored = _store(tmp_path, monkeypatch, [dict(FINDING)])

    findings = stored["findings"]
    assert len(findings) == 1
    expected = repair_module.finding_id(FINDING)
    assert findings[0]["id"] == expected
    # The identity is not positional: it derives from the finding's own content.
    assert expected.startswith(repair_module.FINDING_ID_PREFIX)


def test_a_declined_finding_is_answerable_by_its_stored_id(
    tmp_path, monkeypatch
) -> None:
    """An answered stored finding leaves the lifecycle's open set.

    Without the stored id the lifecycle can neither match nor be given the
    answer, so this is the behaviour the stamped id enables.
    """
    stored = _store(tmp_path, monkeypatch, [dict(FINDING)])
    stored["responses"] = {stored["findings"][0]["id"]: {"action": "declined"}}

    assert review_lifecycle._run_open(stored) is False


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "worker@example.invalid")
    _git(repo, "config", "user.name", "Worker")
    (repo / "one.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "one.txt")
    _git(repo, "commit", "-q", "-m", "chore: seed")
    return repo, _git(repo, "rev-parse", "HEAD")


def _advance(repo: Path, name: str) -> str:
    (repo / name).write_text(name + "\n", encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", "chore: " + name)
    return _git(repo, "rev-parse", "HEAD")


def _store_at(tmp_path: Path, monkeypatch, base_sha: str, head_sha: str) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    review_module.store_review(
        {
            **_record([dict(FINDING)]),
            "reviewed_base_sha": base_sha,
            "reviewed_head_sha": head_sha,
        }
    )


def test_a_review_is_found_from_a_later_head_by_descent(tmp_path, monkeypatch) -> None:
    """A head descending from the reviewed one resolves that stored review.

    The stored record is keyed to the head it read, so selecting by the run's
    current head alone finds nothing once the run has moved. Descent is what
    keeps a review of this work reachable after the repair it caused committed.
    """
    repo, reviewed_head = _repo(tmp_path)
    _store_at(tmp_path, monkeypatch, reviewed_head, reviewed_head)
    later_head = _advance(repo, "two.txt")
    assert later_head != reviewed_head

    path, record, stored_head = review_records.stored_review_descending_from(
        PROJECT, RUN_ID, later_head, repository=repo
    )

    assert record is not None
    assert path is not None
    assert stored_head == reviewed_head
    assert record["reviewed_head_sha"] == reviewed_head


def test_a_review_is_not_found_from_a_divergent_head(tmp_path, monkeypatch) -> None:
    """A head outside the reviewed one's history resolves nothing.

    The negative control for the arm above: without the ancestry test, any
    stored record for the run would be accepted against any head, which is the
    same as not keying the record to a revision at all.
    """
    repo, seed = _repo(tmp_path)
    # The divergent commit branches from the seed, so the reviewed head that
    # lands on main afterwards is not in its history.
    _git(repo, "checkout", "-q", "-b", "divergent", seed)
    divergent_head = _advance(repo, "other.txt")
    _git(repo, "checkout", "-q", "main")
    reviewed_head = _advance(repo, "reviewed.txt")
    _store_at(tmp_path, monkeypatch, reviewed_head, reviewed_head)

    _path, record, stored_head = review_records.stored_review_descending_from(
        PROJECT, RUN_ID, divergent_head, repository=repo
    )

    assert record is None
    assert stored_head == ""


def test_the_re_review_brief_carries_the_diff_and_the_answers() -> None:
    """A later round's brief names what changed and what was already answered.

    The repair reads one brief, so a round that opened after a further commit
    must carry the diff from the head last reviewed and the answers already
    recorded, rather than making the repair rediscover either.
    """
    review = _record([dict(FINDING)])
    review["findings"][0]["id"] = repair_module.finding_id(FINDING)
    diff = "diff --git a/two.txt b/two.txt\n+two\n"

    brief = repair_module.compose_repair_brief(
        review,
        review["findings"],
        diff=diff,
        answers=[{"id": "fdeadbeef00", "action": "declined", "detail": "covered"}],
    )

    assert "Changes since the last reviewed head" in brief
    assert "+two" in brief
    assert "Answers already recorded on this review:" in brief
    assert "[fdeadbeef00] declined: covered" in brief

    node = repair_module.compose_repair_node(review, diff=diff)
    assert node is not None
    assert "+two" in node["brief"]


def test_a_repair_moves_the_head_and_the_disposition_still_lands(
    tmp_path, isolated_reckon_home
) -> None:
    """A hand-resumed repair leaves the run reviewable: the disposition is accepted.

    The review is stored at the head it read and the repair then commits, so the
    run's live head is a descendant rather than that head. Disposing through the
    CLI must answer the stored review rather than refuse for a head mismatch,
    which is what keeps a worker-repaired run inside the review lifecycle.
    """
    from click.testing import CliRunner

    from reckon.cli import main as cli_main
    from reckon.crew import runs

    repo, reviewed_head = _repo(tmp_path)
    (repo / "docs" / "state" / PROJECT).mkdir(parents=True)
    (isolated_reckon_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    review_file = review_module.store_committed_review(
        {
            **_record([dict(FINDING)]),
            "review_run_id": "r-review",
            "reviewed_base_sha": reviewed_head,
            "reviewed_head_sha": reviewed_head,
        },
        root=repo,
    )
    repaired_head = _advance(repo, "repaired.txt")
    assert repaired_head != reviewed_head
    runs._write_json(
        runs.pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "process_alive": False,
            "repo": str(repo),
            "worktree": str(repo),
            "node": {"id": RUN_ID, "plan": "fixture", "section": "s2"},
        },
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "crew",
            "dispose",
            "--project",
            PROJECT,
            "--run",
            RUN_ID,
            "--finding",
            "0",
            "--kind",
            "folded",
            "--node",
            "fix-a",
        ],
    )

    assert result.exit_code == 0, result.output
    stored = json.loads(review_file.read_text(encoding="utf-8"))
    assert stored[review_module.FINDING_DISPOSITIONS_KEY]["0"]["kind"] == "folded"
