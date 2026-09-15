"""The manifest reader accepts JSON and refuses a half-parse.

A manifest whose first character is ``{`` or ``[`` is a JSON document and must
be a JSON object; the tolerant ``key: value`` text form reads everything else.
A body that declares itself JSON and is not a readable object raises rather
than falling back to the text reader, because that fallback once turned a
JSON manifest carrying ``"status": "complete"`` into a well-formed-looking
mapping with eight recognised keys and no status.
"""

from __future__ import annotations

import json

import pytest

from reckon.crew import reports
from reckon.crew.node import CrewError


def _suite(revision: str, failure_ids: list[str]) -> dict[str, object]:
    return {
        "revision": revision,
        "command": "pytest -q",
        "exit_status": 1 if failure_ids else 0,
        "log_path": f"/durable/{revision}.log",
        "log_digest": f"sha256:{revision[:8]}",
        "completed": True,
        "failure_count": len(failure_ids),
        "failure_ids": failure_ids,
    }


# Shaped like the node JSON manifests that were abandoned on a regular basis
# before this reader accepted them: an object, typed suite observations, a
# prose failure_attribution and a structured evidence_inputs block.
_REAL_SHAPED_JSON = """\
{
  "orientation_worktree": "/durable/worktree",
  "orientation_base_sha": "752cc1302bded5c60b279d9fecb1fa1a2a817d95",
  "orientation_write_paths": ["a.py", "b.py"],
  "node": "n-audits-resolve-retired-spellings",
  "status": "complete",
  "commits": ["036dfe72"],
  "changed_paths": [
    "imas_codex/standard_names/audits.py",
    "tests/standard_names/test_audits_resolve_aliases.py"
  ],
  "tests": "UV_PROJECT_ENVIRONMENT=... -> exit 0, 6 passed",
  "test_logs": ["/durable/a.log", "/durable/b.log"],
  "baseline_suite": {
    "revision": "752cc1302bded5c60b279d9fecb1fa1a2a817d95",
    "command": "pytest -q",
    "exit_status": 1,
    "log_path": "/durable/baseline.log",
    "log_digest": "sha256:abc123",
    "completed": true,
    "failure_count": 2,
    "failure_ids": ["tests/test_a.py::test_a", "tests/test_b.py::test_b"]
  },
  "after_suite": {
    "revision": "036dfe72",
    "command": "pytest -q",
    "exit_status": 0,
    "log_path": "/durable/after.log",
    "log_digest": "sha256:def456",
    "completed": true,
    "failure_count": 0,
    "failure_ids": []
  },
  "failure_attribution": "not applicable - implement role, zero added failures",
  "artifacts": ["commit 036dfe72 - 3 files, +101/-8"],
  "evidence_inputs": {"mechanism": "audits.resolve_retired_operator_spellings"},
  "follow_ons": ["workers.py is outside the fence"],
  "blockers": "none"
}
"""


def test_a_json_manifest_carrying_a_status_returns_that_status() -> None:
    parsed = reports.parse_manifest('{"status": "complete", "commits": ["abc123"]}')

    assert parsed["status"] == "complete"
    assert parsed["commits"] == ["abc123"]
    # The half-parse this reader must never return had these keys and no status;
    # the absent list keys are still present, typed as empty.
    assert parsed["changed_paths"] == []
    assert parsed["needs_help"] is None


def test_a_real_shaped_json_manifest_reads_fully() -> None:
    fields = reports.parse_manifest(_REAL_SHAPED_JSON)

    assert fields["status"] == "complete"
    assert fields["commits"] == ["036dfe72"]
    assert fields["changed_paths"] == [
        "imas_codex/standard_names/audits.py",
        "tests/standard_names/test_audits_resolve_aliases.py",
    ]
    assert fields["orientation_worktree"] == "/durable/worktree"
    baseline = fields["baseline_suite"]
    assert baseline["revision"] == "752cc1302bded5c60b279d9fecb1fa1a2a817d95"
    assert baseline["exit_status"] == 1
    assert baseline["completed"] is True
    assert baseline["failure_count"] == 2
    assert baseline["failure_ids"] == [
        "tests/test_a.py::test_a",
        "tests/test_b.py::test_b",
    ]
    assert fields["after_suite"]["failure_ids"] == []
    # A prose failure_attribution stays prose rather than being dropped, and a
    # structured evidence_inputs block is kept whole rather than split apart.
    assert (
        fields["failure_attribution"]
        == "not applicable - implement role, zero added failures"
    )
    assert fields["evidence_inputs"] == {
        "mechanism": "audits.resolve_retired_operator_spellings"
    }
    assert fields["follow_ons"] == ["workers.py is outside the fence"]
    assert fields["blockers"] == []
    assert fields["needs_help"] is None


def test_json_and_text_forms_vote_the_same_fields() -> None:
    payload = {
        "node": "node-a",
        "status": "complete",
        "commits": "abc123",
        "baseline_suite": _suite("base-abc", ["tests/test_a.py::test_a"]),
        "after_suite": _suite("after-abc", []),
    }
    text = (
        "node: node-a\n"
        "status: complete\n"
        "commits: abc123\n"
        "baseline_suite: " + json.dumps(payload["baseline_suite"]) + "\n"
        "after_suite: " + json.dumps(payload["after_suite"]) + "\n"
    )

    assert reports.parse_manifest(text) == reports.parse_manifest(
        json.dumps(payload, indent=2)
    )


def test_a_json_manifest_audits_clean_without_arming() -> None:
    audit = reports.audit_manifest(_REAL_SHAPED_JSON)

    assert audit["ok"] is True, audit["findings"]


def test_a_suite_armed_json_manifest_audits_clean() -> None:
    payload = {
        "node": "node-a",
        "status": "complete",
        "commits": ["abc123"],
        "tests": "pytest -q -> 28 passed",
        "baseline_suite": _suite("base-abc", ["tests/test_old.py::test_old"]),
        "after_suite": _suite(
            "after-abc",
            ["tests/test_old.py::test_old", "tests/test_new.py::test_regression"],
        ),
        "failure_attribution": {"tests/test_new.py::test_regression": "deadbeef1234"},
    }

    audit = reports.audit_manifest(json.dumps(payload, indent=2), suite_armed=True)

    assert audit["ok"] is True, audit["findings"]
    assert audit["manifest"]["failure_attribution"] == payload["failure_attribution"]


@pytest.mark.parametrize(
    "body",
    [
        '{"status": "complete"',
        '{\n  "status": }',
        '{"a": 1',
        "{ bad }",
        '{"status": "complete", }',
    ],
)
def test_an_unreadable_json_body_raises_rather_than_half_parsing(body) -> None:
    with pytest.raises(reports.ManifestParseError) as exc:
        reports.parse_manifest(body)

    message = str(exc.value)
    assert "JSON object" in message
    assert "key: value" in message


def test_the_refusal_names_the_path_when_one_is_given() -> None:
    with pytest.raises(reports.ManifestParseError) as exc:
        reports.parse_manifest(
            '{"status": "complete"',
            path="/runs/x/manifest.json",
        )

    assert "/runs/x/manifest.json" in str(exc.value)


def test_a_json_array_is_not_a_manifest() -> None:
    with pytest.raises(reports.ManifestParseError):
        reports.parse_manifest("[1, 2, 3]")


# A markdown-heading layout: status and commits sit under headings rather than
# at column 0, so the tolerant ``key: value`` reader finds nothing. This is the
# shape that once returned the normalised mapping with eleven keys and no
# status, and the classifier read that partial mapping as a dead worker.
_MARKDOWN_HEADING_MANIFEST = """\
# Delivered manifest

## status

complete

## commits

- 9160ae5026055533316a35f467c8c692fe5a028e
"""


def test_a_heading_manifest_raises_rather_than_half_parsing() -> None:
    with pytest.raises(reports.ManifestParseError) as exc:
        reports.parse_manifest(_MARKDOWN_HEADING_MANIFEST)

    # The refusal names the expected format and the field that could not be
    # determined; it never falls back to the partial mapping with no status.
    message = str(exc.value)
    assert "key: value" in message
    assert "JSON object" in message
    assert "status" in message


def test_the_heading_refusal_names_the_path_when_one_is_given() -> None:
    with pytest.raises(reports.ManifestParseError) as exc:
        reports.parse_manifest(_MARKDOWN_HEADING_MANIFEST, path="/runs/x/manifest.md")
    assert "/runs/x/manifest.md" in str(exc.value)


def test_a_blank_body_is_still_read_not_refused() -> None:
    # The refusal is about content the reader cannot judge; a blank body was
    # always read as no delivery, and that reading is unchanged.
    fields = reports.parse_manifest("   \n\n  ")

    assert fields.get("status") is None
    assert fields["commits"] == []


def test_a_body_with_fields_but_no_status_stays_tolerant() -> None:
    # The reader still keeps a partial text mapping that did yield fields — a
    # body with commits but no status line is read around, not refused. Only a
    # body from which no field at all could be read raises.
    fields = reports.parse_manifest("commits: abc123, def456\n")

    assert fields["commits"] == ["abc123", "def456"]


# A body whose every field-shaped line is incidental prose: each line parses as
# ``key: value`` but none of those keys is a manifest field, so the body
# carries no status and no manifest content. It once returned the normalised
# mapping with thirteen keys and no status, which the classifier read as a
# vanished worker.
_INCIDENTAL_PROSE_MANIFEST = """\
note: the work is in the summary below
detail: every field-shaped line here is incidental prose
"""


def test_a_prose_only_body_raises_rather_than_half_parsing() -> None:
    with pytest.raises(reports.ManifestParseError) as exc:
        reports.parse_manifest(_INCIDENTAL_PROSE_MANIFEST)

    # The refusal names what was expected and the fields that were read; it
    # never returns the thirteen-key mapping with no status.
    message = str(exc.value)
    assert "status cannot be determined" in message
    assert "key: value" in message
    assert "note" in message
    assert "detail" in message


def test_the_prose_refusal_names_the_path_when_one_is_given() -> None:
    with pytest.raises(reports.ManifestParseError) as exc:
        reports.parse_manifest(_INCIDENTAL_PROSE_MANIFEST, path="/runs/x/manifest.md")
    assert "/runs/x/manifest.md" in str(exc.value)


def test_an_unknown_status_word_raises_naming_the_word_and_the_recognised() -> None:
    # An invented word is refused rather than carried forward as a state; the
    # refusal names the word it found and the set it recognises, so a reader
    # can tell the difference between an unrecognised spelling and a missing
    # status.
    with pytest.raises(reports.ManifestParseError) as exc:
        reports.parse_manifest("status: finished\ncommits: abc123\n")

    message = str(exc.value)
    assert "'finished'" in message
    assert "complete" in message
    assert "blocked" in message
    assert "in-progress" in message
    assert "running" in message


def test_the_unknown_status_refusal_names_the_path_when_one_is_given() -> None:
    with pytest.raises(reports.ManifestParseError) as exc:
        reports.parse_manifest("status: finished\n", path="/runs/x/manifest.md")
    assert "/runs/x/manifest.md" in str(exc.value)


def test_an_unknown_status_word_in_json_also_raises() -> None:
    with pytest.raises(reports.ManifestParseError) as exc:
        reports.parse_manifest('{"status": "finished", "commits": ["abc123"]}')
    assert "'finished'" in str(exc.value)


@pytest.mark.parametrize(
    "word",
    [
        "complete",
        "blocked",
        "failed",
        "in-progress",
        "in_progress",
        "running",
        "pending",
    ],
)
def test_a_recognised_status_word_parses_unchanged(word) -> None:
    fields = reports.parse_manifest(f"status: {word}\ncommits: abc123\n")

    assert fields["status"] == word


def test_a_declared_wait_status_parses_unchanged() -> None:
    fields = reports.parse_manifest("status: waiting\n")

    assert fields["status"] == "waiting"


def test_the_unsubstituted_template_is_left_to_the_classifier() -> None:
    # The dispatch contract's placeholder is evidence the worker never wrote a
    # verdict; the reader passes it through so the classifier's unwritten
    # handling can name it, rather than refusing it as an unrecognised word.
    fields = reports.parse_manifest("status: complete | blocked | failed\n")

    assert fields["status"] == "complete | blocked | failed"


def test_the_refusal_is_a_crew_error_and_a_value_error() -> None:
    """Both catch surfaces keep working: classification and the promotion guards."""
    assert issubclass(reports.ManifestParseError, CrewError)
    assert issubclass(reports.ManifestParseError, ValueError)


def test_the_text_form_still_reads_around_prose() -> None:
    text = (
        "here is some prose a worker wrote before the manifest\n"
        "node: node-a\n"
        "status: blocked\n"
        "blockers: |\n"
        "  the actual blocker text lives here\n"
    )

    fields = reports.parse_manifest(text)

    assert fields["status"] == "blocked"
    assert fields["blockers"] == ["the actual blocker text lives here"]
