"""The review store's head key is read, granted and fenced by one head.

A review is evidence about a revision, and the store keys a record by the head
revision it read. Three surfaces decide what that key means: the dispatch the
reflex composes (which write path a reviewer may write to), the classifier that
reads a stored record, and the promotion gate that acts on it. A head key is
worthless if the surface that grants the write path grants only the legacy path
— a reviewer told to write the keyed path is refused it — or if the classifier
accepts a record of an earlier head, or if promotion refuses a record the other
two accept. These assertions hold all three to one head, and each is argued
inside out: the negative half is asserted first.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import recovery, runs
from reckon.crew import review as review_module
from reckon.crew.runs import _write_json, pointer_path

# `reckon.crew` carries a `dispatch` function that shadows the submodule of the
# same name, so the module is fetched by name rather than by attribute.
dispatch_module = importlib.import_module("reckon.crew.dispatch")

PROJECT = "head-keyed-store"
FILE = "region.txt"
BASE = "1" * 40

# Dates are fixed and one second apart so the commit a legacy review's
# timestamp reconstructs is deterministic rather than incidental: a fixture
# that relies on separate commits landing in different wall-clock seconds is a
# test that fails depending on how fast the machine is.
FIRST_WHEN = "2026-01-01T00:00:00+00:00"
REVIEW_WHEN = "2026-01-01T00:00:05+00:00"

REVIEW_CONFIG = {
    "default_backend": "local",
    "local_backend": "local",
    "backends": {
        "local": {
            "launch": "cli",
            "command": "true",
            "model": "fixture",
            "effort": "low",
            "sandbox": "worktree" + "-full",
            "session_reuse": True,
            "time_budget": "20m",
        }
    },
    "roles": {"implement": {}, "review": {}},
    "fences": {"time_budget": "20m", "needs_help_after_failures": 2},
}


def _git(tree: Path, *arguments: str, when: str | None = None) -> str:
    environment = dict(os.environ)
    if when:
        environment["GIT_AUTHOR_DATE"] = when
        environment["GIT_COMMITTER_DATE"] = when
    completed = subprocess.run(
        ["git", *arguments],
        cwd=tree,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return completed.stdout.strip()


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / FILE).write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    _git(root, "add", FILE)
    _git(root, "commit", "-q", "-m", "chore: seed", when=FIRST_WHEN)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _run_tree(repository: Path, tmp_path: Path, run_id: str) -> Path:
    path = tmp_path / f"{run_id}-tree"
    _git(repository, "worktree", "add", "-q", "--detach", str(path), "HEAD")
    return path


def _commit_run(tree: Path, text: str, when: str) -> str:
    target = tree / FILE
    target.write_text(f"{text}\n{target.read_text(encoding='utf-8')}", encoding="utf-8")
    _git(tree, "add", FILE)
    _git(tree, "commit", "-q", "-m", f"test: {text}", when=when)
    return _git(tree, "rev-parse", "HEAD")


def _manifest(tmp_path: Path, run_id: str, commits: str) -> Path:
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {run_id}\n"
        "status: complete\n"
        f"commits: [{commits}]\n"
        f"changed_paths: [{FILE}]\n"
        "tests: focused check passed\n",
        encoding="utf-8",
    )
    return manifest


def _pointer(
    repository: Path, run_tree: Path, run_id: str, base: str, manifest: Path
) -> None:
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(run_tree),
            "base_sha": base,
            "process_alive": False,
            "launch": "in-harness",
            "role": "implement",
            "backend": "local",
            "created_at": "2026-01-01T00:00:01Z",
            "manifest_path": str(manifest),
            "node": {
                "id": run_id,
                "plan": "fixture",
                "section": "review-head",
                "time_budget": "25m",
                "write_paths": [FILE],
            },
        },
    )


def _store_review(
    run_id: str,
    *,
    base: str | None,
    head: str | None,
    score: int = 20,
    timestamp: str | None = None,
) -> Path:
    emitted = "\n".join(
        f"SCORE {dimension}: {score}" for dimension in review_module.REVIEW_DIMENSIONS
    )
    record = review_module.parse_review(emitted)
    record.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": f"review-of-{run_id}",
        }
    )
    if base is not None:
        record["reviewed_base_sha"] = base
    if head is not None:
        record["reviewed_head_sha"] = head
    if timestamp is not None:
        record["timestamp"] = timestamp
    return review_module.store_review(record)


def _run_with_head(
    repository: Path, tmp_path: Path, run_id: str
) -> tuple[Path, str, str]:
    base = _git(repository, "rev-parse", "HEAD")
    tree = _run_tree(repository, tmp_path, run_id)
    head = _commit_run(tree, "repair", FIRST_WHEN)
    manifest = _manifest(tmp_path, run_id, head)
    _pointer(repository, tree, run_id, base, manifest)
    return tree, base, head


# (1) The dispatch grants the head-keyed path beside the legacy path.


def test_the_review_dispatch_grants_the_head_keyed_record_path(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "r-grant-head"
    _tree, base, head = _run_with_head(repository, tmp_path, run_id)
    record = runs.read_pointer(run_id)
    assert recovery.classify_pointer(record)["classification"] == "scoring"

    from reckon import flight

    monkeypatch.setattr(flight, "select_local_backend", lambda resolved: resolved)
    captured: dict[str, object] = {}

    def _capture(**kwargs: object) -> dict[str, str]:
        captured.update(kwargs)
        return {"run_id": f"r-review-{run_id}"}

    monkeypatch.setattr(dispatch_module, "dispatch", _capture)

    result = recovery.dispatch_review_for_run(record, config=REVIEW_CONFIG)
    assert result["dispatched"] is True

    node = captured["node"]
    legacy = review_module.review_path(PROJECT, run_id)
    keyed = review_module.review_path(PROJECT, run_id, reviewed_head_sha=head)
    assert legacy != keyed
    granted = [Path(str(path)) for path in node.write_paths]
    assert legacy in granted
    assert keyed in granted

    # A record stored through store_review at that head lands inside the grant:
    # the head the record names is the path the dispatch handed the reviewer.
    stored = _store_review(run_id, base=base, head=head)
    assert stored == keyed
    assert stored in granted


# (2) The classifier reads the review of the head being classified.


def test_the_classifier_reads_the_review_of_the_head_it_classifies(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-classify-head"
    _tree, base, head = _run_with_head(repository, tmp_path, run_id)
    record = runs.read_pointer(run_id)

    # Negative half first: a review of an earlier revision is not this run's
    # review, so the run is not called promotable on it.
    first = _store_review(run_id, base=BASE, head=base)
    assert first.is_file()
    review, error = recovery._stored_review(record)
    assert review is None and not error
    assert recovery.classify_pointer(record)["classification"] == "scoring"
    assert not review_module.review_path(
        PROJECT, run_id, reviewed_head_sha=head
    ).is_file()

    # Positive half: a review of the classified head is read, and the classifier
    # no longer holds the run back.
    _store_review(run_id, base=BASE, head=head)
    review, error = recovery._stored_review(record)
    assert error == "" and review is not None
    assert review_module.carried_revision_pair(review)[3] == head
    assert review_module.review_path(PROJECT, run_id, reviewed_head_sha=head).is_file()
    assert recovery.classify_pointer(record)["classification"] == "promotable"


# (3) Promotion accepts a legacy record naming no revision when it describes the
#     promoted head, with no waiver.


def test_promotion_accepts_a_legacy_review_describing_the_promoted_head(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-legacy-head"
    _tree, _base, head = _run_with_head(repository, tmp_path, run_id)

    # A record with no revision pair, written at the moment the tree carried
    # ``head``. The revision it read is reconstructed from that timestamp.
    legacy = _store_review(run_id, base=None, head=None, timestamp=REVIEW_WHEN)
    assert legacy == review_module.review_path(PROJECT, run_id)
    stored_record = json.loads(legacy.read_text(encoding="utf-8"))
    # No revision pair is carried: presence is False and the sha is absent,
    # which is the case the promotion must reconstruct rather than refuse.
    assert review_module.carried_revision_pair(stored_record) == (
        False,
        None,
        False,
        None,
    )

    result = crew.complete(run_id, gate="passed", commits=[head], root=repository)
    row = result["record"]
    assert row["promoted_revision"] == head
    # The review landed on the row with no waiver: the reconstructed head equals
    # the asserted one.
    assert row["review"] is not None
    assert row["review"]["status"] == "parsed"
    assert not row.get("review_waiver")


# (4) A refusal never names one revision as both the stored and asserted head.


def test_a_refusal_names_two_revisions_and_never_one_twice(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-two-revisions"
    _tree, base, head = _run_with_head(repository, tmp_path, run_id)
    # A review of the base revision, then a repair: the store holds a revision
    # that disagrees with the head the promotion asserts.
    _store_review(run_id, base=BASE, head=base)

    with pytest.raises(crew.CrewError) as refusal:
        crew.complete(run_id, gate="passed", commits=[head], root=repository)

    message = str(refusal.value)
    assert base[:12] in message
    assert head[:12] in message
    assert base[:12] != head[:12]
    # The two revisions are named as a disagreement, not as one revision
    # appearing twice.
    assert f"{base[:12]} and this promotion asserts {head[:12]}" in message

    # And when the store holds a review of the promoted head, no refusal is
    # produced: the guard never manufactures a disagreement and then names the
    # same revision on both sides.
    _store_review(run_id, base=BASE, head=head)
    stored = crew.complete(run_id, gate="passed", commits=[head], root=repository)
    assert stored["record"]["review"] is not None


# (5) The remedy a refusal prints cites resolved revisions, prose or not.


def _remedy_action(repository: Path, tmp_path: Path, run_id: str, commits: str) -> str:
    base = _git(repository, "rev-parse", "HEAD")
    tree = _run_tree(repository, tmp_path, run_id)
    head = _commit_run(tree, "repair", FIRST_WHEN)
    manifest = _manifest(tmp_path, run_id, commits.format(head=head))
    _pointer(repository, tree, run_id, base, manifest)
    _store_review(run_id, base=BASE, head=head)
    row = recovery.classify_pointer(runs.read_pointer(run_id))
    assert row["classification"] == "promotable"
    return str(row["next_action"])


def test_the_promotion_remedy_cites_resolved_commits(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-remedy-commits"
    # The manifest's commits field is prose naming the revision inside a
    # sentence, which is what a worker actually writes.
    action = _remedy_action(
        repository, tmp_path, run_id, "landed the repair {head} and tidied"
    )
    cited = re.findall(r"--commit ([0-9A-Fa-f]{7,64})", action)
    assert cited, action
    resolved = subprocess.run(
        ["git", "rev-parse", f"{cited[0]}^{{commit}}"],
        cwd=tmp_path / f"{run_id}-tree",
        capture_output=True,
        text=True,
        check=False,
    )
    assert resolved.returncode == 0, resolved.stderr
    assert all(
        value == resolved.stdout.strip() or value in resolved.stdout for value in cited
    )
    assert "{head}" not in action and "landed the repair" not in action


def test_the_remedy_cites_nothing_for_prose_naming_no_commit(
    repository: Path, tmp_path: Path
) -> None:
    run_id = "r-remedy-prose"
    # Prose that names no commit yields no `--commit` value rather than one that
    # fails to resolve: a remedy that reproduces the refusal is worse than one
    # that says nothing about the revision.
    action = _remedy_action(
        repository, tmp_path, run_id, "several commits were made and tidy"
    )
    assert "--commit" not in action
