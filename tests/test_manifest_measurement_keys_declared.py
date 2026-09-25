"""The manifest schema declares the keys a scratch-tree measurement names.

A gate or base-arm measurement run in a scratch copy carries two facts a reader
needs to tell which tree served it: the absolute path of the module under test
as imported, and the resolved working directory the run resolved from. The
dispatch template offers both as ``measurement_module`` and ``measurement_cwd``
keys beside ``test_logs``, so a reader who never opens the log still sees which
tree served each arm. The schema named neither, so a manifest carrying them
reached the audit and promotion as unshaped keys.

A declared field's value is read as the shape the schema says it is, and a
composed shape the field does not accept is refused with the schema's shape
message naming the field rather than carried forward as a plausible value. That
refusal fires on a mapping body under a text-declared field. A list body under
a text-declared field is joined into the field's text instead, which is the
tolerant reading every text field gets and is not a refusal -- so a manifest
carrying either measurement key as a list is read rather than reported, and the
case below pins the reading it does get. Both off-shape readings still depend
on the declaration: an undeclared key keeps the list it composed, which is what
the declared negative control removes the entries to show as a failure.
"""

from __future__ import annotations

from reckon.crew import reports

_MEASUREMENT_MODULE = "/tree/reckon/crew/reports.py"
_MEASUREMENT_CWD = "/tree"

_MANIFEST_CARRYING_BOTH_KEYS_AS_TEXT = """\
status: complete
node: a-measurement-names-its-tree
commits:
  - 1111111 read the manifest through its declared schema
changed_paths:
  - reckon/crew/reports.py
tests: pytest tests/test_manifest_keys.py -> exit 0, 4 passed
measurement_module: /tree/reckon/crew/reports.py
measurement_cwd: /tree
"""

_MAPPING_BODY = """\
status: complete
{key}:
  module: /tree/reckon/crew/reports.py
  cwd: /tree
"""

_LIST_BODY = """\
status: complete
{key}:
  - /tree/reckon/crew/reports.py
  - /tree
"""


def test_the_schema_declares_both_measurement_keys() -> None:
    for key in ("measurement_module", "measurement_cwd"):
        assert key in reports._MANIFEST_FIELD_KEYS
        assert reports._MANIFEST_SCHEMA[key] == reports._TEXT_SHAPE


def test_a_manifest_carrying_both_keys_as_text_parses_with_no_finding() -> None:
    audit = reports.audit_manifest(_MANIFEST_CARRYING_BOTH_KEYS_AS_TEXT)

    assert audit["ok"] is True
    assert audit["findings"] == []
    assert audit["manifest"]["measurement_module"] == _MEASUREMENT_MODULE
    assert audit["manifest"]["measurement_cwd"] == _MEASUREMENT_CWD


def test_a_mapping_body_under_either_key_is_refused_with_the_shape_message() -> None:
    for key in ("measurement_module", "measurement_cwd"):
        audit = reports.audit_manifest(_MAPPING_BODY.format(key=key))

        assert audit["ok"] is False
        finding = " ".join(audit["findings"])
        assert f"field '{key}' carries a mapping" in finding
        assert "the manifest schema accepts text" in finding


def test_a_list_body_under_either_key_is_joined_into_the_fields_text() -> None:
    for key in ("measurement_module", "measurement_cwd"):
        manifest = reports.parse_manifest(_LIST_BODY.format(key=key))

        assert manifest[key] == "/tree/reckon/crew/reports.py, /tree"
        assert not isinstance(manifest[key], list)
