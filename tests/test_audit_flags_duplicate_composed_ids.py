"""The doc audit flags an element id a composed record repeats.

A cumulative evidence record composes its own bytes with its fragments, so two
fragments that reuse one ``id`` — or a fragment that reuses an id the record
already carries — produce a document with two equal anchor targets. The audit
reads the composed text and reports the duplicated id once per collision,
naming the record file and each fragment that carries it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import ledger
from reckon.doccheck import audit_file

PROJECT = "demo"
PLAN = "demo"


def _record_bytes() -> bytes:
    return (
        '<!doctype html>\n<html lang="en"><head>\n'
        '  <meta charset="utf-8">\n'
        f'  <meta name="docs-project" content="{PROJECT}">\n'
        '  <meta name="reckon-type" content="evidence">\n'
        f'  <meta name="plan-evidence-for" content="{PLAN}">\n'
        '</head><body><main class="plan-doc">\n'
        '  <section id="s3"><h2>Record section</h2></section>\n'
        "</main></body></html>\n"
    ).encode()


def _fragment_bytes(node: str, element_id: str) -> bytes:
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        f'<meta name="reckon-type" content="evidence"></head>'
        f'<body><main class="plan-doc"><section id="{element_id}">'
        f"<h2>{node} anchor</h2></section></main></body></html>\n"
    ).encode()


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repository with an empty ledger and a record whose fragments live beside it."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    root = tmp_path / "repo"
    docs = root / "docs"
    (docs / "state" / PROJECT).mkdir(parents=True)
    ledger.write(PROJECT, {"members": [], "runs": [], "holds": []}, 0, root=root)
    (docs / "evidence" / "archive").mkdir(parents=True)
    return root


def _write_record(root: Path) -> Path:
    record = root / "docs" / "evidence" / "archive" / f"{PLAN}-landed.html"
    record.write_bytes(_record_bytes())
    return record


def _write_fragment(root: Path, node: str, element_id: str) -> Path:
    fragment_dir = root / "docs" / "evidence" / "fragments" / PLAN
    fragment_dir.mkdir(parents=True, exist_ok=True)
    fragment = fragment_dir / f"{node}.html"
    fragment.write_bytes(_fragment_bytes(node, element_id))
    return fragment


def _duplicate_ids(findings) -> list:
    return [f for f in findings if f.code == "duplicate-element-id"]


def test_record_and_fragment_sharing_an_id_are_flagged(repository: Path) -> None:
    record = _write_record(repository)
    fragment = _write_fragment(repository, "case-one-node", "s3")

    findings = _duplicate_ids(audit_file(record, project=PROJECT))

    assert len(findings) == 1
    message = findings[0].message
    assert "s3" in message
    # The message names the fragment that carries the colliding id, so the
    # conflicting node is identified rather than described.
    assert fragment.name in message
    assert "the record file" in message


def test_fragment_named_for_its_node_does_not_collide(repository: Path) -> None:
    record = _write_record(repository)
    _write_fragment(repository, "case-two-node", "case-two-node")

    assert _duplicate_ids(audit_file(record, project=PROJECT)) == []


def test_single_document_with_a_duplicated_id_is_flagged(tmp_path: Path) -> None:
    document = tmp_path / "doc.html"
    document.write_text(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="plan-slug" content="{PLAN}">'
        '<meta name="plan-status" content="active"></head><body>'
        '<section id="dup">one</section><section id="dup">two</section>'
        "</body></html>",
        encoding="utf-8",
    )

    findings = _duplicate_ids(audit_file(document))

    assert len(findings) == 1
    assert "dup" in findings[0].message
