"""A landing refused under one rule is judged again when that rule changes."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon.crew import queue_landing, review_dispositions, runs
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
def delivery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
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
    return {
        "repo": repo,
        "base": base,
        "head": revisions[-1],
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


def _pointer(repo: Path) -> None:
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


def _store(head: str, *, base: str, text: str) -> None:
    parsed = review_module.parse_review(text)
    parsed.update(
        {
            "project": PROJECT,
            "reviewed_run_id": RUN,
            "review_run_id": "review-of-refused",
            "reviewed_base_sha": base,
            "reviewed_head_sha": head,
        }
    )
    review_module.store_review(parsed)


def _review(head: str, *, base: str, finding: str = "") -> None:
    _store(
        head,
        base=base,
        text="\n".join(
            (
                *((f"FINDING tests/test_alpha.py:3 {finding}",) if finding else ()),
                *(
                    f"SCORE {dimension}: 20"
                    for dimension in review_module.REVIEW_DIMENSIONS
                ),
            )
        ),
    )


def _incomplete_review(head: str, *, base: str) -> None:
    """A record the acceptance rule cannot read, so it refuses the landing."""

    _store(head, base=base, text="SCORE durability: 20\n")


def _land_stub(calls: list[str]):
    def land(run_id: str) -> tuple[bool, str]:
        calls.append(run_id)
        return True, ""

    return land


def _counting_acceptance(judged: list[object], verdict: bool = False):
    real = queue_landing._review_accepts_promotion

    def counting(review):
        judged.append(review)
        return verdict or real(review)

    return counting


def test_a_refusal_under_an_old_rule_is_judged_again_when_the_code_moves(
    delivery: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    head = str(delivery["head"])
    base = str(delivery["base"])
    _manifest([head])
    _pointer(Path(delivery["repo"]))
    _incomplete_review(head, base=base)
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


def test_a_refusal_is_judged_again_when_only_the_review_record_moves(
    delivery: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The code stamp holds; a disposition recorded on the review is the change."""

    head = str(delivery["head"])
    base = str(delivery["base"])
    _manifest([head])
    _pointer(Path(delivery["repo"]))
    _incomplete_review(head, base=base)
    monkeypatch.setattr(queue_landing, "_landing_code_stamp", lambda: "same-rule")
    monkeypatch.setattr(queue_landing, "_land", lambda run_id: (False, "not reached"))

    first = queue_landing.land_once()
    refused = runs.read_pointer(RUN)["admission_landing"]

    assert first["landed"] == []
    assert refused["guard"].startswith("review acceptance")

    _review(
        head,
        base=base,
        finding="non-blocking: the branch naming reads oddly but holds nothing back",
    )
    review_dispositions.record_finding_disposition(
        PROJECT,
        RUN,
        0,
        kind="exempted",
        reason="the reviewer judged it non-blocking",
        reviewed_head_sha=head,
    )

    calls: list[str] = []
    monkeypatch.setattr(queue_landing, "_land", _land_stub(calls))
    second = queue_landing.land_once()

    assert second["landed"] == [RUN]
    assert calls == [RUN]


def test_an_unchanged_refusal_is_not_judged_a_second_time(
    delivery: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    head = str(delivery["head"])
    base = str(delivery["base"])
    _manifest([head])
    _pointer(Path(delivery["repo"]))
    _incomplete_review(head, base=base)
    judged: list[object] = []
    monkeypatch.setattr(
        queue_landing, "_review_accepts_promotion", _counting_acceptance(judged)
    )
    monkeypatch.setattr(queue_landing, "_land", lambda run_id: (False, "not reached"))

    first = queue_landing.land_once()
    second = queue_landing.land_once()

    assert first["obligations"], first
    assert second == {"landed": [], "obligations": [], "deferred": []}
    assert len(judged) == 1
    assert runs.read_pointer(RUN)["admission_landing"]["status"] == "refused"


def test_a_refusal_with_no_recorded_stamps_is_judged_once_more(
    delivery: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    head = str(delivery["head"])
    base = str(delivery["base"])
    _manifest([head])
    _pointer(Path(delivery["repo"]))
    _review(head, base=base)

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


@pytest.mark.parametrize("entry_shape", ["subject", "subject-with-hex-words", "joined"])
def test_a_commit_entry_spelling_more_than_one_revision_reaches_its_head(
    delivery: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    entry_shape: str,
) -> None:
    head = str(delivery["head"])
    base = str(delivery["base"])
    revisions = [str(revision) for revision in delivery["revisions"]]
    if entry_shape == "subject":
        entry = f"{head[:9]} docs(evidence): record the section-wait repair"
    elif entry_shape == "subject-with-hex-words":
        entry = f"{head[:9]} fix: deadbeef and defaced1 both spell hex"
    else:
        entry = " ".join(revisions)
    _manifest([entry])
    _pointer(Path(delivery["repo"]))
    # A record for another revision sits beside the one this head needs, so a
    # selection that took the newest record instead of the matching one is
    # caught by the head the acceptance judgement is shown.
    _review(revisions[1], base=base)
    _review(head, base=base)
    judged: list[object] = []
    calls: list[str] = []
    monkeypatch.setattr(
        queue_landing,
        "_review_accepts_promotion",
        _counting_acceptance(judged, verdict=True),
    )
    monkeypatch.setattr(queue_landing, "_land", _land_stub(calls))

    result = queue_landing.land_once()

    assert result["landed"] == [RUN]
    assert [review["reviewed_head_sha"] for review in judged] == [head]


@pytest.mark.parametrize(
    "entry",
    [
        "docs(evidence): deadbeef defaced1 record the section-wait repair",
        "{revision},",
        "{revision}.",
    ],
    ids=["prose-first", "trailing-comma", "trailing-full-stop"],
)
def test_a_commit_entry_no_revision_can_be_read_from_is_refused_visibly(
    delivery: dict[str, object], monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    head = str(delivery["head"])
    base = str(delivery["base"])
    entry = entry.format(revision=head[:9])

    assert queue_landing._delivered_revision([entry]) == ""

    _manifest([entry])
    _pointer(Path(delivery["repo"]))
    _review(head, base=base)
    judged: list[object] = []
    calls: list[str] = []
    monkeypatch.setattr(
        queue_landing, "_review_accepts_promotion", _counting_acceptance(judged)
    )
    monkeypatch.setattr(queue_landing, "_land", _land_stub(calls))
    monkeypatch.setattr(queue_landing, "_landing_code_stamp", lambda: "rule")

    result = queue_landing.land_once()

    refused = runs.read_pointer(RUN)["admission_landing"]
    assert result["landed"] == []
    assert calls == []
    assert judged == []
    assert refused["status"] == "refused"
    assert entry in refused["guard"]
    assert refused["code_stamp"] == "rule"
    assert result["obligations"] == [
        {"run_id": RUN, "session": SESSION, "guard": refused["guard"]}
    ]

    # the same unreadable entry under the same code is not reported twice, so a
    # coordinator is not flooded while the manifest stands as written
    assert queue_landing.land_once() == {
        "landed": [],
        "obligations": [],
        "deferred": [],
    }


def test_the_delivered_revision_reads_the_revision_not_a_subject_word(
    delivery: dict[str, object],
) -> None:
    head = str(delivery["head"])
    revisions = [str(revision) for revision in delivery["revisions"]]

    assert (
        queue_landing._delivered_revision([f"{head[:9]} fix: deadbeef defaced1"])
        == head[:9]
    )
    assert queue_landing._delivered_revision([" ".join(revisions)]) == revisions[-1]
    assert queue_landing._delivered_revision([f"docs: {head[:9]}"]) == ""
    # two hexadecimal words are both revisions, so the list rule reads the last
    assert queue_landing._delivered_revision([f"deadbeef {head[:9]}"]) == head[:9]
