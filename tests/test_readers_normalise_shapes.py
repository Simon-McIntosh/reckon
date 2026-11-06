"""Readers accept the shapes their writers have produced.

A stored review record and a delivered manifest have each carried more than one
shape. Findings were bare strings in older records and objects in newer ones,
and a manifest could write a list under a field whose reader declares text. A
reader that indexes each finding, or that hands a list field's entries on one
at a time, must see the same value from either shape.
"""

from __future__ import annotations

import json

from reckon.crew import reports, review

STRING_FINDINGS = {"findings": ["alpha", "beta"]}
OBJECT_FINDINGS = {"findings": [{"text": "alpha"}, {"text": "beta"}]}


def test_string_findings_read_as_the_declared_object_shape() -> None:
    assert review.normalised_findings(STRING_FINDINGS) == [
        {"text": "alpha"},
        {"text": "beta"},
    ]


def test_object_findings_read_as_the_declared_object_shape() -> None:
    assert review.normalised_findings(OBJECT_FINDINGS) == [
        {"text": "alpha"},
        {"text": "beta"},
    ]


def test_both_shapes_read_as_the_same_normalised_list() -> None:
    assert review.normalised_findings(STRING_FINDINGS) == review.normalised_findings(
        OBJECT_FINDINGS
    )


def test_a_record_keeps_its_other_fields_when_its_findings_are_normalised() -> None:
    record = {"status": "parsed", "reviewed_run_id": "r-1", "findings": ["alpha"]}

    normalised = review.with_normalised_findings(record)

    assert normalised["findings"] == [{"text": "alpha"}]
    assert normalised["reviewed_run_id"] == "r-1"
    assert record["findings"] == ["alpha"]


def test_a_yaml_list_of_test_logs_reads_as_that_list() -> None:
    manifest = reports.parse_manifest(
        "status: complete\ntest_logs:\n  - gate_after.log\n  - baseline_gate.log\n"
    )

    assert manifest["test_logs"] == ["gate_after.log", "baseline_gate.log"]


def test_a_json_list_of_test_logs_reads_as_that_list() -> None:
    manifest = reports.parse_manifest(
        json.dumps({"status": "complete", "test_logs": ["gate_after.log"]})
    )

    assert manifest["test_logs"] == ["gate_after.log"]


def test_the_manifest_check_names_a_list_under_a_text_field() -> None:
    findings = reports.audit_manifest(
        'status: complete\ntests: ["python -m pytest", "EXIT=0"]\n'
    )["findings"]

    assert any("tests" in finding and "list" in finding for finding in findings), (
        findings
    )


def test_the_manifest_check_names_a_block_list_under_a_text_field() -> None:
    findings = reports.audit_manifest(
        "status: complete\ntests:\n  - python -m pytest\n  - EXIT=0\n"
    )["findings"]

    assert any("tests" in finding and "list" in finding for finding in findings), (
        findings
    )


def test_the_manifest_check_names_a_json_list_under_a_text_field() -> None:
    body = json.dumps({"status": "complete", "tests": ["python -m pytest", "EXIT=0"]})

    manifest = reports.parse_manifest(body)
    findings = reports.audit_manifest(body)["findings"]

    assert manifest["tests"] == ["python -m pytest", "EXIT=0"]
    assert any("tests" in finding and "list" in finding for finding in findings), (
        findings
    )


def test_a_mapping_under_tests_still_reads_as_that_mapping() -> None:
    manifest = reports.parse_manifest("status: complete\ntests:\n  passed: 41\n")

    assert manifest["tests"] == {"passed": "41"}


def test_a_json_manifest_with_a_text_tests_field_reports_no_shape_finding() -> None:
    body = json.dumps({"status": "complete", "tests": "python -m pytest"})

    findings = reports.audit_manifest(body)["findings"]

    assert not [finding for finding in findings if "list" in finding], findings
