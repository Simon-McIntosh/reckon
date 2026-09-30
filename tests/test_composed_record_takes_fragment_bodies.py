"""A composed landed record takes each fragment's body content, not its document.

A fragment is written as a full HTML document. Appending its bytes whole would
carry its ``head`` into the record, so the fragment's own ``plan-*`` metas would
read as duplicates of the record's scalars. The composer appends only the
children of the fragment's ``main`` element (or of its ``body`` when it has no
``main``), so the record keeps exactly one of each scalar and the fragment's
authored anchors still appear.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from reckon import doccheck, ledger
from reckon.evidence import compose_landed_record

PROJECT = "proj"
PLAN = "demo"

#: The cumulative record itself. Its ``plan-slug`` and ``plan-evidence-for`` are
#: the scalars the composed record must keep exactly one of.
RECORD_BYTES = (
    b'<!doctype html>\n<html lang="en"><head>\n'
    b'  <meta charset="utf-8">\n'
    b'  <meta name="docs-project" content="proj">\n'
    b'  <meta name="reckon-type" content="evidence">\n'
    b'  <meta name="plan-slug" content="demo-landed">\n'
    b'  <meta name="plan-evidence-for" content="demo">\n'
    b"</head><body>\n"
    b'  <main class="plan-doc"><h1>Record</h1></main>\n'
    b"</body></html>\n"
)

#: A full-document fragment. Its own ``plan-slug`` and ``plan-evidence-for``
#: name the plan it belongs to, and its authored section id is the anchor a
#: reader is expected to find in the composed record.
FRAGMENT_BYTES = (
    b'<!doctype html>\n<html lang="en"><head>\n'
    b'  <meta charset="utf-8">\n'
    b'  <meta name="plan-slug" content="demo">\n'
    b'  <meta name="plan-evidence-for" content="demo">\n'
    b"  <title>fragment</title>\n"
    b"</head><body>\n"
    b'  <main class="plan-doc">\n'
    b'    <section id="what-changed">\n'
    b"      <h2>What changed</h2>\n"
    b"    </section>\n"
    b"  </main>\n"
    b"</body></html>\n"
)

#: A full-document fragment that never names a ``body``: its content sits
#: directly under ``html``. Its ``head`` carries the same two scalars as
#: ``FRAGMENT_BYTES``, so a composer that falls through to the raw bytes for
#: this shape re-introduces them.
BODYLESS_FRAGMENT_BYTES = (
    b'<!doctype html>\n<html lang="en"><head>\n'
    b'  <meta charset="utf-8">\n'
    b'  <meta name="plan-slug" content="demo">\n'
    b'  <meta name="plan-evidence-for" content="demo">\n'
    b"  <title>fragment</title>\n"
    b"</head>\n"
    b'  <section id="bodyless-content">\n'
    b"    <h2>Bodyless fragment</h2>\n"
    b"  </section>\n"
    b"</html>\n"
)

NODE = "composed-record-takes-fragment-bodies"


def _seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fragment_bytes: bytes
) -> Path:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    root = tmp_path / "repo"
    docs = root / "docs"
    (docs / "state" / PROJECT).mkdir(parents=True)
    ledger.write(PROJECT, {"members": [], "runs": [], "holds": []}, 0, root=root)

    fragment_dir = docs / "evidence" / "fragments" / PLAN
    fragment_dir.mkdir(parents=True)
    (fragment_dir / f"{NODE}.html").write_bytes(fragment_bytes)

    archive = docs / "evidence" / "archive"
    archive.mkdir(parents=True)
    path = archive / f"{PLAN}-landed.html"
    path.write_bytes(RECORD_BYTES)
    return path


@pytest.fixture()
def record_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return _seed(tmp_path, monkeypatch, FRAGMENT_BYTES)


def _meta_values(text: str, name: str) -> list[str]:
    soup = BeautifulSoup(text, "html.parser")
    return [
        str(tag.get("content") or "")
        for tag in soup.find_all("meta")
        if (tag.get("name") or "").strip() == name
    ]


def test_the_record_keeps_one_of_each_scalar_over_a_fragment(record_path: Path) -> None:
    composed = compose_landed_record(record_path, PLAN, project=PROJECT)
    text = composed.decode("utf-8")

    # Exactly one of each scalar, and both the record's — not the fragment's.
    assert _meta_values(text, "plan-slug") == ["demo-landed"]
    assert _meta_values(text, "plan-evidence-for") == ["demo"]
    # The fragment's body content is present, so the wrapper alone was dropped.
    assert 'id="what-changed"' in text

    findings = doccheck.audit_file(record_path)
    duplicates = [f for f in findings if f.code == "duplicate-plan-scalar"]
    assert duplicates == [], [f.message for f in duplicates]


def test_a_fragments_section_id_appears_once(record_path: Path) -> None:
    composed = compose_landed_record(record_path, PLAN, project=PROJECT).decode("utf-8")
    assert composed.count('id="what-changed"') == 1


def test_a_bodyless_full_document_keeps_only_the_records_scalar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _seed(tmp_path, monkeypatch, BODYLESS_FRAGMENT_BYTES)

    composed = compose_landed_record(path, PLAN, project=PROJECT).decode("utf-8")

    # The fragment's head is dropped even though it never named a body, so its
    # plan-slug does not re-enter beside the record's.
    assert _meta_values(composed, "plan-slug") == ["demo-landed"]
    assert _meta_values(composed, "plan-evidence-for") == ["demo"]
    # The content that sat directly under html is kept.
    assert 'id="bodyless-content"' in composed
