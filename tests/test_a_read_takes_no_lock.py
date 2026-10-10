"""A project-state read takes no write lock when nothing needs recovering.

The read path used to run recovery unconditionally, and recovery took an
exclusive ``flock`` on ``docs/.reckon/locks/<project>-transactions-recovery.lock``.
Opening that lock file is a write, so a read of a read-only ``docs/`` tree died
with ``EROFS``. A read now lists the transaction journals without a lock and
recovers only when one is present, so a tree with nothing to recover opens no
lock file and creates none, while a real journal is now still recovered under
its lock.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from reckon._plan_html import write_state
from reckon.project_state import (
    create_project_state,
    read_resource,
    resource_path,
    write_resource,
)

PROJECT = "sample"
PLAN = "demo-plan"


def _write_plan(docs: Path, slug: str) -> None:
    path = docs / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    html = (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{slug}</title></head><body><main></main></body></html>"
    )
    path.write_text(
        write_state(
            html,
            {
                "project": PROJECT,
                "type": "plan",
                "slug": slug,
                "title": slug.title(),
                "status": "active",
                "impl": 0.0,
            },
        ),
        encoding="utf-8",
    )


def _build_project(docs: Path, tmp_path: Path, monkeypatch) -> None:
    """Create a distributed project holding one sprint with one plan item."""
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(tmp_path / "mounts.json"))
    docs.mkdir(parents=True, exist_ok=True)
    _write_plan(docs, PLAN)
    create_project_state(docs, PROJECT)
    write_resource(
        docs,
        PROJECT,
        "sprint",
        "current",
        {"theme": "Current", "status": "active", "items": [{"slug": PLAN}]},
        0,
        create=True,
    )


def _recovery_lock_path(docs: Path) -> Path:
    return docs / ".reckon" / "locks" / f"{PROJECT}-transactions-recovery.lock"


def _make_readonly(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        path.chmod(0o555 if path.is_dir() else 0o444)
    root.chmod(0o555)


def _restore_writable(root: Path) -> None:
    for path in sorted(root.rglob("*")):
        with contextlib.suppress(OSError):
            path.chmod(0o755 if path.is_dir() else 0o644)
    with contextlib.suppress(OSError):
        root.chmod(0o755)


def test_a_read_of_a_journal_free_tree_creates_no_lock(
    tmp_path: Path, monkeypatch
) -> None:
    """The corpus that has had writes but holds no pending journal reads clean.

    ``.reckon/transactions`` exists (a sprint move creates it), but it holds no
    journal, so the read must take no lock. On a read-only tree, taking the
    lock is what dies with ``EROFS``.
    """
    docs = tmp_path / "docs"
    _build_project(docs, tmp_path, monkeypatch)
    (docs / ".reckon" / "transactions").mkdir()

    lock_path = _recovery_lock_path(docs)
    assert not lock_path.exists(), "the build left a recovery lock behind"

    _make_readonly(docs)
    try:
        data, version = read_resource(docs, PROJECT, "sprint", "current")
    finally:
        _restore_writable(docs)

    assert data["items"] == [{"slug": PLAN}], "the read did not return the plan"
    assert version == 1
    assert not lock_path.exists(), "the read created a recovery lock file"


def test_a_writable_read_without_a_journal_creates_no_lock(
    tmp_path: Path, monkeypatch
) -> None:
    """On a writable tree the counter file is absent either way.

    This is the direct evidence the lock is never taken, independent of the
    read-only permission check: recovery would have created the lock file.
    """
    docs = tmp_path / "docs"
    _build_project(docs, tmp_path, monkeypatch)
    (docs / ".reckon" / "transactions").mkdir()

    lock_path = _recovery_lock_path(docs)
    read_resource(docs, PROJECT, "sprint", "current")

    assert not lock_path.exists(), "the read created a recovery lock file"


def test_a_read_recovers_a_payload_journal_it_finds(
    tmp_path: Path, monkeypatch
) -> None:
    """A present journal is still recovered, and both sides are restored."""
    from reckon import project_state

    docs = tmp_path / "docs"
    _build_project(docs, tmp_path, monkeypatch)
    write_resource(
        docs,
        PROJECT,
        "sprint",
        "next",
        {"theme": "Next", "status": "active", "items": []},
        0,
        create=True,
    )

    source_path = resource_path(docs, PROJECT, "sprint", "current")
    target_path = resource_path(docs, PROJECT, "sprint", "next")
    source_bytes = source_path.read_bytes()
    target_bytes = target_path.read_bytes()

    journal = project_state._move_journal_path(docs, PROJECT, "current", "next", PLAN)
    project_state._publish_move_journal(
        journal, PROJECT, "current", "next", source_bytes, target_bytes
    )
    # A write interrupted after the journal was published leaves the target
    # neither as the old nor the new settled content.
    target_path.write_text("INTERRUPTED", encoding="utf-8")
    assert journal.exists()

    data, _version = read_resource(docs, PROJECT, "sprint", "next")

    assert not journal.exists(), "the read did not clear the journal it found"
    assert target_path.read_bytes() == target_bytes, "the target was not restored"
    assert source_path.read_bytes() == source_bytes, "the source was not restored"
    assert data["theme"] == "Next"
