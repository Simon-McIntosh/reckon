"""A manifest field's type comes from the declared schema, not from string splitting.

Seven point repairs to the tolerant line reader did not generalise, so the
reader is driven by a declared schema: each field names the shapes it accepts,
and a real parser (``yaml.compose``, which keeps every scalar's literal text)
decodes the structure. The four defects below are the measured cases that the
line scanner got wrong, each one a value whose type depended on how a string
happened to be split rather than on what the schema says:

* a block list is a list of items, so a comma inside a commit subject is part
  of the item rather than a separator that manufactures a fourth revision;
* a nested mapping is the writer's mapping, so it arrives whole rather than as
  the empty string a skipped body produced;
* a quoted scalar carries its own quoting rules, so a flow list's quoted item
  keeps its commas and its text instead of being split into a fragment;
* a nested key stays under its parent, so the mapping reads under that parent
  rather than being lost and leaving the top-level field untouched.

A mapping written under a list field is one item per line rather than a refusal.
Workers label the entries of a list field (``test_logs``, ``artifacts``,
``follow_ons``, a bare ``commits`` block of sha-and-subject lines), which
composes as a mapping — the shape no list field declared — and refusing it cost
27 delivered manifests that the line-scanning reader had accepted. The refusal
is kept for a shape no field declares, which is a mapping body under a text
field, and a mapping key that is itself a mapping no longer raises out of the
reader.

The negative half is asserted too: the defects the schema was introduced to fix
stay fixed, so a three-commit list whose last subject carries a comma still reads
as three entries rather than four, and a nested mapping under ``tests`` still
reads as that mapping rather than the empty string.
"""

from __future__ import annotations

import pytest

from reckon.crew import reports
from reckon.crew.node import CrewError

# The reader is fed manifests exactly as a worker writes them, so the fixtures
# stay byte-shaped like the text form rather than being built from a dict.

# Three commits, the third subject carrying a comma. The tolerant reader joined
# the block items with ", " and split the join on commas, so the subject's own
# comma became a fourth revision consisting of a prose fragment.
_THREE_COMMITS_ONE_COMMA = """\
status: complete
commits:
  - 1111111 add the first guard
  - 2222222 add the second guard
  - 3333333 record the measurement, its measurement and the correction it forced
changed_paths:
  - reckon/crew/reports.py
"""

# A flow list whose first item is unquoted, so it is not JSON and the fallback
# splitter cut on every comma — including the one inside the quoted item — and
# stripped the quote characters off each fragment.
_FLOW_LIST_WITH_A_QUOTED_ITEM = """\
status: complete
commits: [29937e892, "5a1b2c3d4 record the measurement, its measurement and the correction it forced"]
"""

# A nested mapping under a text-or-mapping field. The body was collected by no
# field at all, so the field read as the empty string.
_NESTED_TESTS_MAPPING = """\
status: complete
tests:
  passed: 41
  failed: 0
"""


def test_a_block_list_item_keeps_its_own_commas() -> None:
    manifest = reports.parse_manifest(_THREE_COMMITS_ONE_COMMA)

    assert len(manifest["commits"]) == 3
    assert manifest["commits"][-1] == (
        "3333333 record the measurement, its measurement and the correction it forced"
    )
    assert "its measurement and the correction it forced" not in manifest["commits"]


def test_a_field_after_the_list_is_still_read() -> None:
    # The list body must end where the next field begins, so the field written
    # after it is not folded into the last item.
    manifest = reports.parse_manifest(_THREE_COMMITS_ONE_COMMA)

    assert manifest["changed_paths"] == ["reckon/crew/reports.py"]


def test_a_nested_mapping_under_tests_reads_as_that_mapping() -> None:
    manifest = reports.parse_manifest(_NESTED_TESTS_MAPPING)

    assert manifest["tests"] == {"passed": "41", "failed": "0"}


def test_a_quoted_item_in_a_flow_list_keeps_its_commas_and_text() -> None:
    manifest = reports.parse_manifest(_FLOW_LIST_WITH_A_QUOTED_ITEM)

    assert manifest["commits"] == [
        "29937e892",
        "5a1b2c3d4 record the measurement, its measurement and the correction it forced",
    ]


def test_a_nested_key_stays_under_its_parent() -> None:
    manifest = reports.parse_manifest(
        "status: complete\n"
        "failure_attribution:\n"
        "  test_local: abc123\n"
        "  test_other: def456\n"
    )

    assert manifest["failure_attribution"] is not None
    assert manifest["failure_attribution"]["test_local"] == "abc123"
    assert manifest["failure_attribution"]["test_other"] == "def456"
    assert "test_local" not in manifest


def test_a_mapping_body_under_a_list_field_reads_as_one_item_per_line() -> None:
    manifest = reports.parse_manifest(
        "status: complete\nchanged_paths:\n  files: 2\n  root: src\n"
    )

    assert manifest["changed_paths"] == ["files: 2", "root: src"]


def test_the_same_mapping_body_is_a_mapping_under_a_field_that_declares_it() -> None:
    # The shape reads differently under each kind of field: a list field takes
    # its entries as items, a text-or-mapping field takes the mapping itself.
    manifest = reports.parse_manifest("status: complete\ntests:\n  files: 2\n")

    assert manifest["tests"] == {"files": "2"}


def test_a_mapping_body_under_a_text_field_is_refused_naming_the_field() -> None:
    # The refusal is kept for a shape no field declares, so a value whose type
    # the schema cannot place is never carried forward as a plausible one.
    with pytest.raises(reports.ManifestParseError) as excinfo:
        reports.parse_manifest(
            "status: complete\nnode:\n  name: a-node\n  role: implement\n"
        )

    assert "node" in str(excinfo.value)


def test_the_refusal_is_a_crew_error_so_it_lands_on_the_cli_surface() -> None:
    with pytest.raises(CrewError):
        reports.parse_manifest("status: complete\nnode:\n  name: a-node\n")


# A commit entry is an identifier followed by its subject, so it routinely
# carries a colon. YAML types such a line as a one-key mapping, which would hand
# a promotion a dict where an object id belongs.
_COMMIT_SUBJECT_WITH_A_COLON = """\
status: complete
commits:
  - 1111111 read the manifest: the declared schema decides a field's type
  - 2222222 add the second guard
"""


def test_a_commit_entry_keeps_a_colon_in_its_subject_as_text() -> None:
    manifest = reports.parse_manifest(_COMMIT_SUBJECT_WITH_A_COLON)

    assert manifest["commits"] == [
        "1111111 read the manifest: the declared schema decides a field's type",
        "2222222 add the second guard",
    ]
    assert all(isinstance(commit, str) for commit in manifest["commits"])


def test_a_quoted_commit_entry_with_a_colon_is_read_the_same_way() -> None:
    manifest = reports.parse_manifest(
        'commits: ["1111111 read the manifest: the schema decides", "2222222 guard"]\n'
    )

    assert manifest["commits"] == [
        "1111111 read the manifest: the schema decides",
        "2222222 guard",
    ]


def test_a_colon_in_another_identifier_field_stays_text() -> None:
    manifest = reports.parse_manifest(
        "status: complete\n"
        "changed_paths:\n"
        "  - docs/a-note: the record of the run\n"
        "  - reckon/crew/reports.py\n"
    )

    assert manifest["changed_paths"] == [
        "docs/a-note: the record of the run",
        "reckon/crew/reports.py",
    ]


# A field whose entries a worker labelled composes as a mapping, and a mapping
# used to raise. Each fixture below is one field's real text, quoted from the
# manifest its comment names, so these cases range over what workers wrote
# rather than over a shape invented here.

# r-20260903T082148119595-promotion-surfaces-the-failure-attribution
_TEST_LOGS_LABELLED_WITH_ITS_OWN_KEYS = """\
status: complete
test_logs:
  gate_after: /home/ITER/mcintos/.config/reckon/crew/runs/r-20260903T082148119595-promotion-surfaces-the-failure-attribution/gate_after.log
  baseline_gate: /home/ITER/mcintos/.config/reckon/crew/runs/r-20260903T082148119595-promotion-surfaces-the-failure-attribution/baseline_gate.log
  pin_mutation_check: /home/ITER/mcintos/.config/reckon/crew/runs/r-20260903T082148119595-promotion-surfaces-the-failure-attribution/pin_mutation_check.log
"""


def test_a_list_field_labelled_with_key_value_lines_reads_one_item_per_line() -> None:
    manifest = reports.parse_manifest(_TEST_LOGS_LABELLED_WITH_ITS_OWN_KEYS)

    assert isinstance(manifest["test_logs"], list)
    assert len(manifest["test_logs"]) == 3
    assert manifest["test_logs"][0].startswith("gate_after: /home/")
    assert manifest["test_logs"][0].endswith("/gate_after.log")
    assert manifest["test_logs"][2].endswith("/pin_mutation_check.log")


# r-20260826T085038261032-ledger-summary-partitions-gates
_ARTIFACTS_LABELLED_WITH_ITS_OWN_KEY = """\
status: complete
artifacts:
  patch: retained in the worktree (shadow run — no commit)
"""


def test_a_single_labelled_entry_reads_as_a_one_item_list() -> None:
    manifest = reports.parse_manifest(_ARTIFACTS_LABELLED_WITH_ITS_OWN_KEY)

    assert manifest["artifacts"] == [
        "patch: retained in the worktree (shadow run — no commit)"
    ]


# r-20260907T164554588285-nia-convergence-atlas
_FOLLOW_ONS_NUMBERED_WITH_ITS_OWN_KEYS = """\
status: complete
follow_ons:
  1. limited-class panel with flux contours: relabel a limited frame on H200 and persist its flux raster (relabelled limited corpus and pre-correction reads survive only as classifications today) — then add the panel(s) to the atlas grid
  2. reversed-current-block panel: solve one of shots 22475/22550/22626 with negative polarity on H200, persist the terminal (axis-admission-failed) flux raster, add the panel showing the failure beside EFIT references
  3. DIII-D flux persistence: the diiid operands persist no flux field; persisting per_cell_flux_values (or the EFIT psirz already used) lets the DIII-D contours come from Nova's own map
  4. optional: persistence of per-wall-node private-flux classification in the bank operands would let the exact production wall_height_shadow_mask shadow render on the MAST rows
"""


def test_a_labelled_follow_on_keeps_its_label_and_the_colon_inside_it() -> None:
    manifest = reports.parse_manifest(_FOLLOW_ONS_NUMBERED_WITH_ITS_OWN_KEYS)

    assert len(manifest["follow_ons"]) == 4
    assert manifest["follow_ons"][0].startswith(
        "1. limited-class panel with flux contours: relabel a limited frame"
    )
    assert manifest["follow_ons"][0].endswith("add the panel(s) to the atlas grid")
    assert manifest["follow_ons"][3].startswith("4. optional: persistence")
    assert manifest["follow_ons"][3].endswith("shadow render on the MAST rows")


# r-20260915T112004448735-the-skill-mirrors-the-runtime-contract
_COMMITS_WRITTEN_WITHOUT_BULLETS = """\
status: complete
commits:
  569ce7cd3a84e295b4c88b8d17289be5c64dedb4 docs(ship-skill): record the landed landing-contract change
  7a03a312897004841e5b01a5732cb06dc1d1cf0f test(ship-skill): re-aim plan-state-writer contract tests
  e33f0ca1c1028fe0ba7b11fbddc1eee195b3fd53 docs(ship-skills): land-record contract mirrors the runtime prompt
"""


def test_a_commit_line_written_without_a_bullet_keeps_its_sha_and_subject() -> None:
    manifest = reports.parse_manifest(_COMMITS_WRITTEN_WITHOUT_BULLETS)

    assert manifest["commits"] == [
        (
            "569ce7cd3a84e295b4c88b8d17289be5c64dedb4 docs(ship-skill): record the "
            "landed landing-contract change"
        ),
        (
            "7a03a312897004841e5b01a5732cb06dc1d1cf0f test(ship-skill): re-aim "
            "plan-state-writer contract tests"
        ),
        (
            "e33f0ca1c1028fe0ba7b11fbddc1eee195b3fd53 docs(ship-skills): land-record "
            "contract mirrors the runtime prompt"
        ),
    ]


# r-20260905T212626527787-n-sli-the-graph-suite-has-a-base
_SUITE_OBSERVATION_WHOSE_FAILURE_IDS_IS_A_BRACED_NOTE = """\
status: complete
baseline_suite:
  revision: 2b5b40209da51e27fe38ed6a09a54f146983c9f4
  command: "pytest tests/standard_names tests/graph -m graph -p no:cacheprovider"
  exit_status: 1
  log_path: /home/ITER/mcintos/.config/reckon/crew/runs/r-20260905T212626527787-n-sli-the-graph-suite-has-a-base/baseline.log
  completed: true
  failure_count: 51
  failure_ids: {{see baseline_ids.json — 51 fully-qualified pytest node ids, e.g. "tests/graph/test_structural.py::TestClusterIntegrity::test_clusters_have_members", "tests/standard_names/test_name_lifecycle.py::test_full_acceptance_path", ...}}
"""


def test_a_mapping_key_that_is_itself_a_mapping_does_not_crash_the_reader() -> None:
    # The body composes to a mapping whose own key composes as a mapping, which
    # is unhashable: the decode raised TypeError out of parse_manifest rather
    # than reading the field, so a delivered manifest read as a crashed reader.
    # The field reads as its mapping and its composed scalars read as the text
    # the worker wrote, so the braced note where a list of node ids belongs is
    # not typed as a list.
    manifest = reports.parse_manifest(
        _SUITE_OBSERVATION_WHOSE_FAILURE_IDS_IS_A_BRACED_NOTE
    )

    assert manifest["status"] == "complete"
    assert isinstance(manifest["baseline_suite"], dict)
    assert manifest["baseline_suite"]["revision"] == (
        "2b5b40209da51e27fe38ed6a09a54f146983c9f4"
    )
    assert manifest["baseline_suite"]["log_path"].endswith("/baseline.log")
    assert manifest["baseline_suite"]["failure_ids"] is None


def test_a_mapping_used_as_a_key_reads_as_the_text_the_worker_wrote() -> None:
    # Positive control for the reader above: the same braced form the corpus
    # manifest writes, under a field whose schema keeps a mapping, so the
    # unhashable key is read here rather than dropped with the untidy rows.
    manifest = reports.parse_manifest(
        "status: complete\nevidence_inputs:\n  {{a: b}: c}\n"
    )

    assert manifest["evidence_inputs"] == {"{a: b}": "c"}
