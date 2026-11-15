"""A request whose stored brief copy is gone reads the brief it names.

A queued request carries the stored copy an earlier attempt made in its own
run directory. When that attempt was refused or released, the directory went
with the copy, and every later launch refused on a brief it could no longer
read while the source the request names was still on disk: on 14 November
three review requests were refused this way on every admission pass.
"""

from __future__ import annotations

from reckon.crew import dispatch_admission
from reckon.crew.node import TaskNode


def _node(source: str, stored: str) -> TaskNode:
    node = TaskNode(
        id="review-of-bank-width-probe",
        goal="attach an independent review",
        plan="",
    )
    node.brief = source
    node.brief_path = stored
    return node


def test_a_missing_stored_copy_falls_back_to_the_source(tmp_path):
    source = tmp_path / "review-brief.md"
    source.write_text("review the run\n", encoding="utf-8")
    node = _node(str(source), str(tmp_path / "gone" / "brief.md"))
    assert dispatch_admission.readable_brief_source(node) == str(source)
    assert dispatch_admission._brief_text(node).startswith("review the run")


def test_a_present_stored_copy_is_still_read(tmp_path):
    source = tmp_path / "review-brief.md"
    source.write_text("the source\n", encoding="utf-8")
    stored = tmp_path / "run" / "brief.md"
    stored.parent.mkdir()
    stored.write_text("the stored copy\n", encoding="utf-8")
    node = _node(str(source), str(stored))
    assert dispatch_admission.readable_brief_source(node) == str(stored)
    assert dispatch_admission._brief_text(node).startswith("the stored copy")
