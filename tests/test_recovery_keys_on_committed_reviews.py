"""A committed review record moves the memo key, and a composed review writes its bytes.

The review readers read the project's committed ``docs/state/<project>/reviews/``
tree before the host staging store, so a promoted run's classification is
answered from the committed copy. The classification memo keys the inputs a
classification reads, and a key that named only the staging paths would go on
serving the verdict taken before a record was committed over the record the
reader now returns. These cases hold the committed tree's identity in the key:
writing a run's review record under the committed run directory changes the key
the memo is keyed on.

The other half of the record is the plan bytes a plan review was taken against.
A review of an uncommitted plan write could not be re-read, because the digest
it recorded named bytes no object in the repository held. The composition writes
those bytes to the object store, so the recorded ``reviewed_blob_sha`` resolves
with ``git cat-file -e`` even while the plan is still uncommitted.

Every crew directory is environment-resolved under ``tmp_path``; nothing touches
the operator's own store.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon.crew import recovery
from reckon.crew import review as review_module

PROJECT = "recovery-keys"
REVIEWED_RUN = "r-reviewed-run"
REVIEW_RUN = "r-review-run"
PLAN_SLUG = "demo-plan"

PLAN_HTML = """<!doctype html>
<html><head><meta name="plan-slug" content="demo-plan"></head>
<body><h2 id="s1">§1</h2><p>text</p></body></html>
"""


@pytest.fixture()
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthesised project checkout mounted at its own docs directory."""
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config))
    repo = tmp_path / "repo"
    (repo / "docs" / "state" / PROJECT).mkdir(parents=True)
    (config / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    return repo


def _git(tree: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *argv],
        cwd=tree,
        check=True,
        capture_output=True,
        text=True,
    )


def _fresh_repo(tree: Path) -> None:
    tree.mkdir(parents=True, exist_ok=True)
    _git(tree, "init", "-q")
    _git(tree, "config", "user.email", "worker@example.invalid")
    _git(tree, "config", "user.name", "Worker")
    (tree / "README.md").write_text("initial\n", encoding="utf-8")
    _git(tree, "add", "README.md")
    _git(tree, "commit", "-q", "-m", "initial")


def _committed_run_review_path(root: Path) -> Path:
    committed = review_module.committed_review_root(PROJECT, root=root)
    assert committed is not None
    return committed / review_module.COMMITTED_RUN_DIRNAME / REVIEWED_RUN / (
        f"{REVIEW_RUN}.json"
    )


def _write_committed_record(root: Path) -> Path:
    """Place a run's review record in the committed tree, as promotion does."""
    payload = {
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN,
        "review_run_id": REVIEW_RUN,
        "status": "parsed",
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 18),
        "absent": [],
        "total": 18 * len(review_module.REVIEW_DIMENSIONS),
    }
    path = _committed_run_review_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ── (1) A committed write moves the memo key ─────────────────────────────────


def test_a_committed_review_write_changes_the_memo_key(root: Path) -> None:
    record = {"project": PROJECT, "run_id": REVIEWED_RUN}

    before = recovery._review_input_identities(record)
    # No committed record exists yet, so the committed run directory reads its
    # own absence, whatever the staging store holds, and the key is taken.
    assert recovery._classification_key(before)

    committed_path = _write_committed_record(root)
    assert committed_path.is_file()

    after = recovery._review_input_identities(record)

    # The committed record's own path and the run directory that gained it are
    # both inputs, so the write moves the key the memo is keyed on.
    assert str(committed_path) in after
    assert after != before

    # A memo keyed on the earlier state cannot serve the later one: the sha the
    # memo is stored under differs, which is what forces a reclassification.
    assert recovery._classification_key(after) != recovery._classification_key(before)


def test_an_unchanged_committed_tree_leaves_the_key_in_force(root: Path) -> None:
    record = {"project": PROJECT, "run_id": REVIEWED_RUN}
    _write_committed_record(root)

    first = recovery._review_input_identities(record)
    second = recovery._review_input_identities(record)

    # Reading twice with nothing written between returns the same identities, so
    # the committed tree's own identity does not defeat the memo it keys.
    assert second == first


# ── (2) A composed plan review writes the reviewed bytes to the object store ─


def test_composing_a_plan_review_stores_the_reviewed_plan_bytes(
    root: Path, tmp_path: Path
) -> None:
    _fresh_repo(root)
    plan = root / "docs" / "plan.html"
    plan.write_text(PLAN_HTML, encoding="utf-8")
    # The plan is written but never committed: its bytes are reachable from no
    # commit, so only writing the blob at review time can make them an object.
    assert _git(root, "status", "--porcelain").stdout.strip()

    record = {
        "subject": "plan",
        "project": PROJECT,
        "plan_slug": PLAN_SLUG,
        "run_id": "r-plan-review-dispatch",
        "plan_path": str(plan),
        "repo": str(root),
        "rubric": "plan_review",
        "session": "s-test",
    }

    fields = recovery._review_dispatch_fields(record, write=True)
    blob = fields["head"]
    assert blob

    # The object the record will name resolves in the repository.
    _git(root, "cat-file", "-e", blob)

    sidecar = Path(fields["sidecar"])
    recorded = json.loads(sidecar.read_text(encoding="utf-8"))
    assert recorded["reviewed_blob_sha"] == blob
    # The recorded blob's bytes are the plan's own, read back from the object.
    assert _git(root, "cat-file", "blob", blob).stdout == PLAN_HTML