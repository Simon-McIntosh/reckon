"""The lifecycle scalars are written as one block and read by a stated rule.

Two landing records merged with a union resolution leave a document carrying
two copies of every scalar line, and the three lifecycle scalars — impl,
version, modified — are the ones a merge rewrites on both sides. Written as
adjacent lines they are a single hunk for a three-way merge; read by a stated
rule they resolve to a named copy instead of whichever copy happens to sit
last in the document.

The rule: the FIRST occurrence in document order wins. The writer replaces the
first occurrence in place, so reader and writer then name the same line.
Every later copy is ignored and reported as a compatibility warning.
"""

from __future__ import annotations

from datetime import date

import pytest
from bs4 import BeautifulSoup

from reckon import _store
from reckon._plan_html import parse_meta, read_state

# The three lifecycle scalars sit apart from each other, which is the shape a
# union-resolved merge produces before this change converges them.
PLAN_DOC = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="docs-project" content="demo">
  <meta name="reckon-type" content="plan">
  <meta name="plan-slug" content="scalar-block">
  <meta name="plan-impl" content="0.0">
  <meta name="plan-title" content="Scalar block">
  <meta name="plan-status" content="active">
  <meta name="plan-version" content="2">
  <meta name="plan-summary" content="One block, one hunk">
  <meta name="plan-modified" content="2026-01-01">
  <title>Scalar block</title>
</head>
<body><main class="plan-doc"><h1>Scalar block</h1></main></body>
</html>
"""

DUPLICATE_DOC = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="reckon-type" content="plan">
  <meta name="plan-impl" content="{impl_first}">
  <meta name="plan-version" content="{version_first}">
  <meta name="plan-modified" content="{modified_first}">
  <meta name="plan-impl" content="{impl_last}">
  <meta name="plan-version" content="{version_last}">
  <meta name="plan-modified" content="{modified_last}">
</head>
<body><main class="plan-doc"></main></body>
</html>
"""


def _next_element(tag):
    sibling = tag.next_sibling
    while sibling is not None and not getattr(sibling, "name", None):
        sibling = sibling.next_sibling
    return sibling


def _duplicate_document(first: tuple[str, str, str], last: tuple[str, str, str]) -> str:
    return DUPLICATE_DOC.format(
        impl_first=first[0],
        version_first=first[1],
        modified_first=first[2],
        impl_last=last[0],
        version_last=last[1],
        modified_last=last[2],
    )


@pytest.mark.parametrize(
    ("first", "last"),
    [
        (("0.9", "3", "2026-01-01"), ("0.1", "7", "2026-02-02")),
        (("0.1", "7", "2026-02-02"), ("0.9", "3", "2026-01-01")),
    ],
    ids=["first-copy-newer", "copies-swapped"],
)
def test_duplicate_scalar_resolves_to_the_first_copy(first, last):
    state = read_state(_duplicate_document(first, last))
    assert state["impl"] == float(first[0])
    assert state["version"] == int(first[1])
    assert state["modified"] == first[2]
    warnings = " ".join(state.get("compatibility_warnings") or [])
    for name in ("plan-impl", "plan-version", "plan-modified"):
        assert name in warnings


def test_metadata_reader_applies_the_same_rule(tmp_path):
    path = tmp_path / "duplicate-scalars.html"
    path.write_text(
        _duplicate_document(("0.9", "3", "2026-01-01"), ("0.1", "7", "2026-02-02")),
        encoding="utf-8",
    )
    record = parse_meta(path, "duplicate-scalars")
    assert record["impl"] == pytest.approx(0.9)
    assert record["version"] == 3
    assert record["modified"] == "2026-01-01"
    warnings = " ".join(record.get("compatibility_warnings") or [])
    assert "plan-impl" in warnings


def test_store_write_emits_the_lifecycle_scalars_as_one_block(tmp_path, monkeypatch):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    checkout = tmp_path / "checkout"
    plan_dir = checkout / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_file = plan_dir / "scalar-block.html"
    plan_file.write_text(PLAN_DOC, encoding="utf-8")

    state = read_state(plan_file.read_text(encoding="utf-8"))
    state["impl"] = 0.5
    expected_version = int(state.get("version") or 0)
    _store.write_plan("demo", "scalar-block", state, expected_version, root=checkout)

    soup = BeautifulSoup(plan_file.read_text(encoding="utf-8"), "html.parser")
    impl = soup.find("meta", attrs={"name": "plan-impl"})
    version = soup.find("meta", attrs={"name": "plan-version"})
    modified = soup.find("meta", attrs={"name": "plan-modified"})
    assert impl is not None and version is not None and modified is not None
    assert _next_element(impl) is version
    assert _next_element(version) is modified
    assert impl.get("content") == "0.5"
    assert version.get("content") == str(expected_version + 1)
    assert modified.get("content") == date.today().isoformat()