"""One owner for the atomic publish: the surviving forks render through it.

Before the migration the atomic-publish mechanism was forked at three levels --
a private durable-replace in ``project_state`` (five call sites), a private
temporary-and-rename in ``follow_checkpoint``, and three HTML write-then-replace
paths inside ``_store`` itself. Each of those call sites now reaches
``reckon._store.write_atomically``.

These tests drive each former call site with the shared primitive's render
callback made to fail after it has written part of its payload, and assert the
destination is still the previous file (or still absent) with no sibling
temporary left beside it -- never the partial write. They also assert the
``binary`` keyword hands the callback a binary handle, and that a requested
directory fsync actually reaches the directory.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from reckon import _plan_html, _store, project_state

OLD = b"OLD CONTENT\n"
PARTIAL_TEXT = "PARTIAL"
PARTIAL_BYTES = b"PARTIAL"

PROJECT = "sample"
PLAN = "demo-plan"

DECLARATIONS = {"s1": "done", "s2": "implementable"}


def _interrupting(real):
    """Wrap ``write_atomically`` so every render writes part then raises."""

    def interrupting(path, render, **kwargs):
        def partial(handle):
            handle.write(PARTIAL_BYTES if kwargs.get("binary") else PARTIAL_TEXT)
            raise RuntimeError("injected interruption")

        return real(path, partial, **kwargs)

    return interrupting


def _assert_intact(target: Path, prior: bytes | None) -> None:
    """The target is the whole old file, or still absent -- never partial."""
    survivors = list(target.parent.glob(f".{target.name}*"))
    assert survivors == [], f"a temporary survived: {survivors}"
    if prior is None:
        assert not target.exists(), "the interrupted write left a destination"
    else:
        assert target.read_bytes() == prior, "the destination is not the old file"


def _state(authored: str, project: str = PROJECT, slug: str = PLAN) -> str:
    state = {
        "project": project,
        "type": "plan",
        "slug": slug,
        "title": "Demo plan",
        "status": "active",
        "modified": "2026-09-25",
        "version": 0,
        "section_declarations": dict(DECLARATIONS),
        "sections": [],
        "followups": [],
    }
    return _plan_html.write_state(authored, state)


AUTHORED = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    f'<meta name="docs-project" content="{PROJECT}">'
    '<meta name="reckon-type" content="plan">'
    "<title>Demo plan</title></head><body>"
    '<main class="plan-doc">'
    '<h2 id="s1">First section</h2><p>First body.</p>'
    '<h2 id="s2">Second section</h2><p>Second body.</p>'
    "</main></body></html>"
)


# --------------------------------------------------------------------------
# Drivers: one per former call site. Each returns (target, prior-bytes-or-None).
# --------------------------------------------------------------------------


def _drive_json_adapter(tmp_path, monkeypatch):
    """_store.write_json_atomically -- the JSON adapter over the primitive."""
    target = tmp_path / "adapter.json"
    target.write_bytes(OLD)
    monkeypatch.setattr(
        _store, "write_atomically", _interrupting(_store.write_atomically)
    )
    with pytest.raises(RuntimeError):
        _store.write_json_atomically(target, {"a": 1})
    return target, OLD


def _drive_evidence_record(tmp_path, monkeypatch):
    """_store._apply_evidence_appends -- the cumulative landing record."""
    docs = tmp_path / "docs"
    docs.mkdir()
    target = docs / "evidence" / "archive" / f"{PLAN}-landed.html"
    monkeypatch.setattr(
        _store, "write_atomically", _interrupting(_store.write_atomically)
    )
    request = {
        "op": "append_evidence",
        "plan": PLAN,
        "anchor": "beat",
        "title": "beat",
        "body": "<p>x.</p>",
    }
    with pytest.raises(RuntimeError):
        _store._apply_evidence_appends(docs, PROJECT, [request], None)
    return target, None


def _drive_plan_state_write(tmp_path, monkeypatch):
    """_store._write_state -- the plan's semantic HTML writer."""
    checkout = tmp_path / "repo"
    target = checkout / "docs" / "plans" / f"{PLAN}.html"
    target.parent.mkdir(parents=True)
    target.write_text(_state(AUTHORED), encoding="utf-8")
    prior = target.read_bytes()
    monkeypatch.setattr(
        _store, "write_atomically", _interrupting(_store.write_atomically)
    )
    with pytest.raises(RuntimeError):
        _store.write_plan(
            PROJECT,
            PLAN,
            {"title": "A changed title"},
            0,
            root=checkout,
        )
    return target, prior


def _drive_plan_text_write(tmp_path, monkeypatch):
    """_store._replace_plan_text -- the exact-replacement HTML writer."""
    checkout = tmp_path / "repo"
    target = checkout / "docs" / "plans" / f"{PLAN}.html"
    target.parent.mkdir(parents=True)
    target.write_text(_state(AUTHORED), encoding="utf-8")
    prior = target.read_bytes()
    monkeypatch.setattr(
        _store, "write_atomically", _interrupting(_store.write_atomically)
    )
    with pytest.raises(RuntimeError):
        _store._replace_plan_text(
            PROJECT,
            PLAN,
            [("<p>First body.</p>", "<p>Replaced body.</p>")],
            0,
            checkout,
            "plan",
            indexed=False,
        )
    return target, prior


def _drive_typed_resource(tmp_path, monkeypatch):
    """project_state._write_resource_unlocked -- a rendered typed resource."""
    docs = tmp_path / "docs"
    docs.mkdir()
    project_state.create_project_state(docs, PROJECT)
    target = project_state.resource_path(docs, PROJECT, "timeline", "timeline")
    prior = target.read_bytes()
    monkeypatch.setattr(
        project_state, "write_atomically", _interrupting(project_state.write_atomically)
    )
    with pytest.raises(RuntimeError):
        project_state.write_resource(
            docs,
            PROJECT,
            "timeline",
            "timeline",
            {"events": []},
            1,
        )
    return target, prior


def _drive_staged_install(tmp_path, monkeypatch):
    """project_state.create_project_state -- the staged HTML install branch."""
    docs = tmp_path / "docs"
    docs.mkdir()
    target = project_state.resource_path(docs, PROJECT, "timeline", "timeline")
    monkeypatch.setattr(
        project_state, "write_atomically", _interrupting(project_state.write_atomically)
    )
    with pytest.raises(RuntimeError):
        project_state.create_project_state(docs, PROJECT)
    return target, None


def _drive_transaction_recovery(tmp_path, monkeypatch):
    """project_state recovery of a prepared sprint-move journal (byte payload)."""
    docs = tmp_path / "docs"
    docs.mkdir()
    source = project_state.resource_path(docs, PROJECT, "sprint", "current")
    source.parent.mkdir(parents=True)
    source.write_text(_state(AUTHORED), encoding="utf-8")
    prior = source.read_bytes()
    journal = project_state._move_journal_path(
        docs, PROJECT, "current", "next", "alpha"
    )
    project_state._publish_move_journal(
        journal, PROJECT, "current", "next", b"SOURCE BEFORE", b"TARGET BEFORE"
    )
    monkeypatch.setattr(
        project_state, "write_atomically", _interrupting(project_state.write_atomically)
    )
    with pytest.raises(RuntimeError):
        project_state.recover_project_state_transactions(docs, PROJECT)
    return source, prior


def _drive_checkpoint_history(tmp_path, monkeypatch):
    """follow_checkpoint.append_history -- the capped pane-history rewrite.

    The rewrite is the atomic call site; the rows appended before it are not.
    The wrapper snapshots the file at the instant before the rewrite so the
    assertion is against the target's own state, not a reconstruction.
    """
    from reckon.crew import follow_checkpoint

    target = tmp_path / "hist.json"
    monkeypatch.setattr(follow_checkpoint, "history_path", lambda p, s: target)
    captured: dict[str, bytes] = {}
    real = follow_checkpoint.write_atomically
    interrupting = _interrupting(real)

    def snapshot_then_interrupt(path, render, **kwargs):
        captured["prior"] = Path(path).read_bytes()
        return interrupting(path, render, **kwargs)

    monkeypatch.setattr(follow_checkpoint, "write_atomically", snapshot_then_interrupt)

    def append_until_capped() -> None:
        for index in range(4):
            follow_checkpoint.append_history(
                PROJECT,
                "sess",
                text=f"row{index}",
                at=1000.0 + index,
                max_rows=1,
                max_seconds=10_000,
                now=1000.0 + index,
            )

    with pytest.raises(RuntimeError):
        append_until_capped()
    return target, captured["prior"]


SITES = {
    "json_adapter": _drive_json_adapter,
    "evidence_record": _drive_evidence_record,
    "plan_state_write": _drive_plan_state_write,
    "plan_text_write": _drive_plan_text_write,
    "typed_resource": _drive_typed_resource,
    "staged_install": _drive_staged_install,
    "transaction_recovery": _drive_transaction_recovery,
    "checkpoint_history": _drive_checkpoint_history,
}


@pytest.mark.parametrize("driver", SITES.values(), ids=list(SITES))
def test_interrupted_write_leaves_old_or_new_at_each_call_site(
    driver, tmp_path, monkeypatch
):
    target, prior = driver(tmp_path, monkeypatch)
    _assert_intact(target, prior)


# --------------------------------------------------------------------------
# The binary keyword and the directory fsync the primitive owes its callers.
# --------------------------------------------------------------------------


def test_binary_hands_the_callback_a_binary_handle(tmp_path):
    target = tmp_path / "blob.bin"
    seen: list[type] = []

    def render(handle):
        seen.append(type(handle))
        assert isinstance(handle, __import__("io").BufferedWriter)
        handle.write(b"\x00\x01\x02binary")

    _store.write_atomically(target, render, binary=True)

    assert target.read_bytes() == b"\x00\x01\x02binary"
    assert seen, "the render callback was never invoked"


def test_binary_write_leaves_old_or_new_on_failure(tmp_path, monkeypatch):
    target = tmp_path / "blob.bin"
    target.write_bytes(OLD)
    monkeypatch.setattr(
        _store, "write_atomically", _interrupting(_store.write_atomically)
    )
    with pytest.raises(RuntimeError):
        _store.write_atomically(
            target, lambda handle: handle.write(b"NEW"), binary=True
        )
    _assert_intact(target, OLD)


def test_directory_fsync_is_reached_when_requested(tmp_path, monkeypatch):
    kinds: list[int | None] = []
    real = os.fsync

    def recording(fd):
        try:
            kinds.append(stat.S_IFMT(os.fstat(fd).st_mode))
        except OSError:
            kinds.append(None)
        return real(fd)

    monkeypatch.setattr(os, "fsync", recording)
    _store.write_atomically(
        tmp_path / "f.json",
        lambda handle: handle.write("{}\n"),
        fsync_directory=True,
    )

    assert stat.S_IFDIR in kinds, "the rename into the directory was never flushed"
