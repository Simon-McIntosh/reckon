"""A sprint move settles all-or-nothing, and its reply matches the disk.

Faults are injected at each step of a move run through the edit-plan write
path. The item is meant to end in exactly one sprint, and the reply's
applied-or-not verdict and versions are compared against a fresh read of both
sprint resources.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import _plan_html, project_state
from reckon import mcp as mcp_module
from reckon import mcp_edit_plan as edit_module
from reckon.project_state import create_project_state, read_resource, write_resource

SOURCE = "S3"
TARGET = "S5"
SLUG = "alpha"


def _write_plan(docs: Path, slug: str) -> None:
    path = docs / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        '<meta name="docs-project" content="sample">'
        f"<title>{slug}</title></head><body><main></main></body></html>"
    )
    path.write_text(
        _plan_html.write_state(
            bare,
            {
                "type": "plan",
                "slug": slug,
                "title": slug.title(),
                "status": "active",
                "impl": 0.0,
            },
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def sprint_pair(tmp_path: Path) -> Path:
    docs = tmp_path / "docs"
    docs.mkdir()
    _write_plan(docs, SLUG)
    create_project_state(docs, "sample")
    write_resource(docs, "sample", "sprint", SOURCE, {"items": [SLUG]}, 0, create=True)
    write_resource(docs, "sample", "sprint", TARGET, {"items": []}, 0, create=True)
    return docs


def _slugs(sprint: dict) -> set[str]:
    return {
        str(row if isinstance(row, str) else row.get("slug")) for row in sprint["items"]
    }


def _move(docs: Path) -> dict:
    _, from_version = read_resource(docs, "sample", "sprint", SOURCE)
    _, to_version = read_resource(docs, "sample", "sprint", TARGET)
    return mcp_module._edit_plan(
        "sample",
        SOURCE,
        [
            {
                "op": "move",
                "target": "sprint_item",
                "slug": SLUG,
                "to": TARGET,
                "to_version": to_version,
            }
        ],
        from_version,
        checkout_path=str(docs.parent),
        doc_type="sprint",
    )


def _fault_on_write_call(monkeypatch: pytest.MonkeyPatch, ordinal: int) -> None:
    original = project_state._write_resource_unlocked
    calls = {"count": 0}

    def wrapper(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == ordinal:
            raise project_state.ProjectStateError("injected sprint write failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(project_state, "_write_resource_unlocked", wrapper)


def _fault_marking_committed(
    monkeypatch: pytest.MonkeyPatch, *, journal_gone: bool
) -> None:
    def wrapper(path: Path) -> None:
        if journal_gone:
            path.unlink()
            raise FileNotFoundError(path)
        raise OSError("injected commit-mark failure")

    monkeypatch.setattr(project_state, "_mark_move_journal_committed", wrapper)


def _fault_read_back_disagrees(
    monkeypatch: pytest.MonkeyPatch, stale: tuple[dict, int]
) -> None:
    """Make the post-write read of the source sprint report the pre-move state.

    This is what a reverted sprint looks like to the read-back: both writes
    landed and the source still holds the item.
    """
    original_read = project_state._read_resource_unchecked
    original_write = project_state._write_resource_unlocked
    state = {"writes": 0, "fired": False}

    def write_wrapper(*args, **kwargs):
        result = original_write(*args, **kwargs)
        state["writes"] += 1
        return result

    def read_wrapper(docs_dir, project, resource_type, resource_id):
        if (
            not state["fired"]
            and state["writes"] >= 2
            and (resource_type, resource_id) == ("sprint", SOURCE)
        ):
            state["fired"] = True
            return stale
        return original_read(docs_dir, project, resource_type, resource_id)

    monkeypatch.setattr(project_state, "_write_resource_unlocked", write_wrapper)
    monkeypatch.setattr(project_state, "_read_resource_unchecked", read_wrapper)


def _assert_reply_matches_disk(reply: dict, docs: Path, *, applied: bool) -> None:
    source, from_version = read_resource(docs, "sample", "sprint", SOURCE)
    target, to_version = read_resource(docs, "sample", "sprint", TARGET)
    assert (SLUG in _slugs(source)) != (SLUG in _slugs(target)), (
        "the item must be in exactly one sprint"
    )
    assert (SLUG in _slugs(target)) is applied
    if applied:
        assert reply["ok"] is True, (
            f"reply says not applied while the item is in {TARGET}: {reply}"
        )
        assert reply.get("applied") is True, reply
        assert reply["sprint_versions"] == {
            SOURCE: from_version,
            TARGET: to_version,
        }
    else:
        assert reply["ok"] is False, f"reply claims a move that did not land: {reply}"
        assert reply.get("applied") is False, reply
        assert reply.get("sprint_versions") == {
            SOURCE: from_version,
            TARGET: to_version,
        }, reply


def test_fault_after_journal_written_reports_not_applied(
    sprint_pair: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fault_on_write_call(monkeypatch, 1)
    reply = _move(sprint_pair)
    _assert_reply_matches_disk(reply, sprint_pair, applied=False)


def test_fault_after_first_sprint_write_reports_not_applied(
    sprint_pair: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fault_on_write_call(monkeypatch, 2)
    reply = _move(sprint_pair)
    _assert_reply_matches_disk(reply, sprint_pair, applied=False)


def test_fault_after_second_sprint_write_reports_applied(
    sprint_pair: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fault_marking_committed(monkeypatch, journal_gone=False)
    reply = _move(sprint_pair)
    _assert_reply_matches_disk(reply, sprint_pair, applied=True)
    assert any("journal_commit_mark_failed" in row for row in reply["warnings"])


def test_commit_mark_with_journal_already_gone_reports_applied(
    sprint_pair: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fault_marking_committed(monkeypatch, journal_gone=True)
    reply = _move(sprint_pair)
    _assert_reply_matches_disk(reply, sprint_pair, applied=True)


def test_a_read_back_that_disagrees_restores_both_sprints_before_refusing(
    sprint_pair: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_path = sprint_pair / "sprints" / f"{SOURCE}.html"
    target_path = sprint_pair / "sprints" / f"{TARGET}.html"
    source_before = source_path.read_bytes()
    target_before = target_path.read_bytes()
    stale = read_resource(sprint_pair, "sample", "sprint", SOURCE)
    _fault_read_back_disagrees(monkeypatch, stale)

    reply = _move(sprint_pair)

    assert reply["ok"] is False, reply
    assert reply.get("applied") is False, reply
    assert source_path.read_bytes() == source_before
    assert target_path.read_bytes() == target_before
    _assert_reply_matches_disk(reply, sprint_pair, applied=False)
    journals = list(
        (sprint_pair / ".reckon" / "transactions").glob("sprint-move-*.json")
    )
    assert journals == []


def test_a_settled_move_leaves_no_journal_between_the_sprints(
    sprint_pair: Path,
) -> None:
    reply = _move(sprint_pair)
    _assert_reply_matches_disk(reply, sprint_pair, applied=True)
    journals = list(
        (sprint_pair / ".reckon" / "transactions").glob("sprint-move-*.json")
    )
    assert journals == []


def _fault_version_read_after_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the reply's own version read once the move has settled.

    The move and its internal read-back have both finished; only the read the
    reply performs to name the sprints' versions fails.
    """
    original_write = project_state._write_resource_unlocked
    original_read = edit_module.read_resource
    state = {"writes": 0}

    def write_wrapper(*args, **kwargs):
        result = original_write(*args, **kwargs)
        state["writes"] += 1
        return result

    def read_wrapper(docs_dir, project, resource_type, resource_id):
        if state["writes"] >= 2 and resource_type == "sprint":
            raise OSError("injected version read failure")
        return original_read(docs_dir, project, resource_type, resource_id)

    monkeypatch.setattr(project_state, "_write_resource_unlocked", write_wrapper)
    monkeypatch.setattr(edit_module, "read_resource", read_wrapper)


def test_a_failed_post_write_version_read_keeps_the_applied_verdict(
    sprint_pair: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fault_version_read_after_settle(monkeypatch)

    reply = _move(sprint_pair)

    assert reply["ok"] is True, reply
    assert reply.get("applied") is True, (
        f"a failed version read must not drop the verdict: {reply}"
    )
    source, _ = read_resource(sprint_pair, "sample", "sprint", SOURCE)
    target, _ = read_resource(sprint_pair, "sample", "sprint", TARGET)
    assert SLUG not in _slugs(source)
    assert SLUG in _slugs(target)
    assert not reply.get("sprint_versions"), reply
    warnings = reply.get("warnings") or []
    assert any(row.startswith("sprint_version_read_failed=") for row in warnings), (
        f"the reply must name the failed read: {reply}"
    )
    assert any(SOURCE in row for row in warnings), reply
