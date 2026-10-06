"""``reckon audit-doc`` resolves ``review:<project>/<id>`` against the committed tree.

A review is kept as a committed record in the project's repository beside the
ledger. A plan comment, a followup or an evidence record cites one by
``review:<project>/<review-id>``; the audit follows the citation into the
project's committed reviews tree, stays silent on a reference that resolves,
and reports an error naming the reference when no committed record carries its
id. Every fixture writes its project into a synthetic checkout so nothing
outside ``tmp_path`` is read or written.
"""

from __future__ import annotations

import json
from pathlib import Path

from reckon import doccheck as doccheck_module
from reckon.crew.review import committed_review_root, review_path
from reckon.doccheck import audit_file, checkout_root_for

PROJECT = "tempproj"
RESOLVED_ID = "r-20261006T140000000000-review-of-consumer"
REVIEWED_RUN_ID = "r-20261006T135900000000-consumer"
MISSING_ID = "r-20261006T140000000001-review-of-nowhere"
CODE = doccheck_module._REVIEW_REFERENCE_MISSING


def _checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    (root / "docs").mkdir(parents=True, exist_ok=True)
    return root


def _committed_record(root: Path, review_id: str) -> Path:
    """Write one committed run review through the store's own path helper."""

    committed_root = committed_review_root(PROJECT, root=root)
    assert committed_root is not None
    path = review_path(
        PROJECT,
        REVIEWED_RUN_ID,
        committed_root=committed_root,
        review_run_id=review_id,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "project": PROJECT,
                "review_run_id": review_id,
                "reviewed_run_id": REVIEWED_RUN_ID,
                "status": "parsed",
            }
        ),
        encoding="utf-8",
    )
    return path


def _plan_document(body: str) -> str:
    metas = [
        ("plan-slug", "consumer"),
        ("plan-status", "active"),
        ("plan-modified", "2026-10-02"),
        ("docs-project", PROJECT),
    ]
    head = "".join(f'<meta name="{name}" content="{value}">' for name, value in metas)
    return (
        '<!doctype html><html lang="en"><head>'
        f"{head}<title>consumer</title></head>"
        '<body><main class="plan-doc">'
        '<article class="r-comment"><div class="r-comment-body">'
        f"{body}</div></article></main></body></html>"
    )


def _write_plan(root: Path, body: str, name: str = "consumer") -> Path:
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    path = plans / f"{name}.html"
    path.write_text(_plan_document(body), encoding="utf-8")
    return path


def _write_evidence(root: Path, body: str) -> Path:
    evidence = root / "docs" / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    path = evidence / "consumer-landed.html"
    metas = [
        ("reckon-type", "evidence"),
        ("plan-slug", "consumer-landed"),
        ("plan-evidence-for", "consumer"),
        ("docs-project", PROJECT),
    ]
    head = "".join(f'<meta name="{name}" content="{value}">' for name, value in metas)
    path.write_text(
        f'<!doctype html><html lang="en"><head>{head}<title>consumer</title></head>'
        f"<body><main>{body}</main></body></html>",
        encoding="utf-8",
    )
    return path


def _of_code(findings, code: str):
    return [finding for finding in findings if finding.code == code]


def _naming(findings, reference: str):
    return [finding for finding in findings if reference in finding.message]


def _others(findings):
    """Every finding that is not the review-reference finding under test."""

    return sorted(
        (finding.severity, finding.code, finding.message)
        for finding in findings
        if finding.code != CODE
    )


def test_the_declared_code_is_the_one_the_module_raises():
    assert doccheck_module._REVIEW_REFERENCE_MISSING == "review-reference-missing"


# ── a reference that resolves ───────────────────────────────────────────────


def test_a_reference_to_a_committed_record_resolves(tmp_path: Path):
    root = _checkout(tmp_path)
    _committed_record(root, RESOLVED_ID)
    reference = f"review:{PROJECT}/{RESOLVED_ID}"
    doc = _write_plan(root, f"the section closed on {reference}")

    findings = audit_file(doc, project=PROJECT, root=root)

    assert _of_code(findings, CODE) == []
    assert _naming(findings, reference) == []


def test_the_checkout_root_is_inferred_from_the_document_path(tmp_path: Path):
    root = _checkout(tmp_path)
    _committed_record(root, RESOLVED_ID)
    doc = _write_plan(root, f"closed on review:{PROJECT}/{RESOLVED_ID}")

    assert checkout_root_for(doc) == root.resolve()
    assert _of_code(audit_file(doc, project=PROJECT), CODE) == []


# ── a reference that does not ───────────────────────────────────────────────


def test_a_reference_to_a_missing_record_is_an_error_naming_it(tmp_path: Path):
    root = _checkout(tmp_path)
    reference = f"review:{PROJECT}/{MISSING_ID}"
    doc = _write_plan(root, f"the section closed on {reference}")

    findings = audit_file(doc, project=PROJECT, root=root)

    (finding,) = _of_code(findings, CODE)
    assert finding.severity == "error"
    assert reference in finding.message


def test_only_the_unresolved_reference_is_reported(tmp_path: Path):
    root = _checkout(tmp_path)
    _committed_record(root, RESOLVED_ID)
    doc = _write_plan(
        root,
        f"first review:{PROJECT}/{RESOLVED_ID} then review:{PROJECT}/{MISSING_ID}",
    )

    findings = audit_file(doc, project=PROJECT, root=root)

    (finding,) = _of_code(findings, CODE)
    assert MISSING_ID in finding.message
    assert RESOLVED_ID not in finding.message


def test_an_evidence_record_citation_is_read(tmp_path: Path):
    root = _checkout(tmp_path)
    reference = f"review:{PROJECT}/{MISSING_ID}"
    doc = _write_evidence(root, f"<p>the review {reference} closed the section</p>")

    findings = audit_file(doc, project=PROJECT, root=root)

    (finding,) = _of_code(findings, CODE)
    assert reference in finding.message


def test_a_bare_word_review_without_a_slash_is_not_a_reference(tmp_path: Path):
    """``review:head`` and prose like ``review: null`` name no committed record."""

    root = _checkout(tmp_path)
    doc = _write_plan(root, "the review:head reading and review: null both stand")

    assert _of_code(audit_file(doc, project=PROJECT, root=root), CODE) == []


# ── isolation: no other finding moves when the check fires ──────────────────


def test_no_other_audit_finding_changes_between_the_two_arms(tmp_path: Path):
    root = _checkout(tmp_path)
    _committed_record(root, RESOLVED_ID)
    resolved = _write_plan(root, f"closed on review:{PROJECT}/{RESOLVED_ID}", "a")
    missing = _write_plan(root, f"closed on review:{PROJECT}/{MISSING_ID}", "b")

    resolved_findings = audit_file(resolved, project=PROJECT, root=root)
    missing_findings = audit_file(missing, project=PROJECT, root=root)

    assert _of_code(missing_findings, CODE)
    assert _others(resolved_findings) == _others(missing_findings)
