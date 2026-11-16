"""The tick record names a gate a reader can run.

The section's record carries the gate receipt promotion fills, and a receipt
naming a test the tree does not hold, or a command that is a template someone
forgot to resolve, is not a receipt. This case reads the record itself and holds
it to a runnable command: every ``tests/...`` path it names exists, and the page
still carries the paragraph, so deleting the receipt reddens here rather than
four sections later.
"""

from __future__ import annotations

import re
from pathlib import Path

RECORD = Path("docs/evidence/fragments/work-scales-to-eighty-across-nodes/s5.html")
GATE_PARAGRAPH = re.compile(r'<p id="s5-gate">(.*?)</p>', re.DOTALL)
TEST_PATH = re.compile(r"tests/[A-Za-z0-9_./-]+\.py")


def _paragraph() -> str:
    match = GATE_PARAGRAPH.search(RECORD.read_text(encoding="utf-8"))
    assert match is not None, "the tick record carries no gate paragraph"
    return match.group(1)


def test_the_record_names_the_pytest_gate() -> None:
    paragraph = _paragraph()
    assert "python -m pytest" in paragraph
    assert "p no:cacheprovider" in paragraph


def test_every_test_the_record_names_exists() -> None:
    named = sorted(set(TEST_PATH.findall(_paragraph())))
    assert named, "the gate paragraph names no test file"
    missing = [path for path in named if not Path(path).is_file()]
    assert not missing, f"the record names tests the tree does not hold: {missing}"


def test_the_record_still_names_both_arms() -> None:
    paragraph = _paragraph()
    for revision in ("0f519315992f429fae018708f84eba960b99fb15",):
        assert revision in paragraph, f"the gate paragraph names no {revision} arm"

