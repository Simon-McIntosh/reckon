"""A suite record keyed by arm is refused where its author can still fix it.

The manifest template defines one flat record per suite arm, carrying
``revision``, ``command``, ``exit_status``, ``log_path``, ``completed`` and
``failure_ids`` at the record's top level. A worker whose gate genuinely has
several arms naturally records an object keyed by arm instead — one keyed by
the terminal each run rendered under, one keyed by the measurement each arm
took. Validation folds such an object into a canonical observation whose every
field is empty, so the arm reads at promotion as one that declares no
completion, and that refusal arrives after the worker's process has ended,
when only a coordinator can restructure the record. The audit refuses the shape
at ``check-manifest`` time instead, naming the field and the keys a flat record
needs.

The two non-flat shapes below are copied from the as-delivered manifests of
``r-20261003T130535115617`` (keyed by terminal: ``dumb`` and ``colour``) and
``r-20261003T132224666771`` (keyed by measurement: ``population``,
``named_alone``, ...). Both passed their workers' own checks and reached
promotion as unreadable arms before a coordinator flattened them by hand.
"""

from __future__ import annotations

import json

from reckon.crew import reports

_TICKER_BASELINE = {
    "dumb": {
        "revision": "aff594161bf1098fe807fa73b41180db35d32130",
        "command": "<repo>/.venv/bin/python -m pytest -p no:cacheprovider -q -rf "
        "--tb=line tests/test_crew_recovery.py tests/test_crew_ticker.py "
        "tests/test_crew_ticker_layout.py "
        "tests/test_ticker_node_names_keep_their_ends.py tests/test_crew_recover.py",
        "environment": "TERM=dumb NO_COLOR=1",
        "exit_status": 1,
        "log_path": "/home/ITER/mcintos/.config/reckon/crew/runs/"
        "r-20261003T130535115617-ticker-tests-hold-under-any-terminal/"
        "logs/base-arm-dumb.log",
        "completed": True,
        "failure_count": 30,
        "failure_ids": [
            "tests/test_crew_recovery.py::test_cli_follow_streams_each_event_as_one_json_document",
            "tests/test_crew_recovery.py::test_snapshot_carries_every_field_the_ticker_column_set_reads",
            "tests/test_crew_recovery.py::test_an_aliased_pointer_renders_the_alias_not_the_model_id",
            "tests/test_crew_recovery.py::test_legacy_log_line_renders_and_new_line_renders_two_cells",
            "tests/test_crew_recovery.py::test_one_stored_new_line_renders_differently_at_two_display_settings",
            "tests/test_crew_ticker.py::test_cli_follow_prints_compact_transition_lines_by_default",
            "tests/test_crew_ticker.py::test_cli_follow_keeps_machine_objects_behind_json_flag",
            "tests/test_crew_ticker.py::test_follow_emit_path_preserves_painted_escape_codes",
            "tests/test_crew_ticker.py::test_cli_watch_follow_emit_site_preserves_painted_escape_codes",
            "tests/test_crew_ticker.py::test_a_shadow_row_says_so_end_to_end_rather_than_by_identifier",
            "tests/test_crew_ticker_layout.py::test_the_queued_counter_still_renders_a_zero_rather_than_dropping_it",
            "tests/test_crew_ticker_layout.py::test_a_reason_is_truncated_to_the_room_the_grid_leaves",
            "tests/test_crew_ticker_layout.py::test_a_reason_that_fits_is_printed_whole",
            "tests/test_crew_ticker_layout.py::test_a_reason_clipped_at_the_margin_ends_on_a_word",
            "tests/test_crew_ticker_layout.py::test_colour_changes_only_presentation",
            "tests/test_crew_ticker_layout.py::test_a_worker_keeps_one_colour_and_neighbours_differ",
            "tests/test_crew_ticker_layout.py::test_a_narrow_width_is_widened_to_what_the_columns_need",
            "tests/test_crew_ticker_layout.py::test_each_side_of_the_transition_is_painted_as_its_own_state",
            "tests/test_crew_ticker_layout.py::test_the_role_is_dim_rather_than_hued",
            "tests/test_crew_ticker_layout.py::test_a_narrow_width_still_widens_to_fit_the_role_column",
            "tests/test_crew_ticker_layout.py::test_the_model_and_effort_are_not_fused_into_one_cell",
            "tests/test_crew_ticker_layout.py::test_the_longest_role_model_and_effort_elide_within_budget",
            "tests/test_crew_ticker_layout.py::test_a_legacy_composed_string_renders_the_same_cells_as_separate_facts",
            "tests/test_crew_ticker_layout.py::test_the_effort_column_keeps_one_offset_whatever_the_alias_length",
            "tests/test_crew_ticker_layout.py::test_no_row_pads_the_widest_alias_before_its_effort",
            "tests/test_ticker_node_names_keep_their_ends.py::test_two_long_names_render_apart_at_the_default_width",
            "tests/test_ticker_node_names_keep_their_ends.py::test_a_name_that_fits_its_cell_is_unchanged",
            "tests/test_ticker_node_names_keep_their_ends.py::test_two_names_that_cut_alike_each_carry_their_own_mint",
            "tests/test_ticker_node_names_keep_their_ends.py::test_a_lone_long_name_carries_no_suffix",
            "tests/test_crew_recover.py::test_recovery_command_help_names_the_state_and_the_boundary",
        ],
    },
    "colour": {
        "revision": "aff594161bf1098fe807fa73b41180db35d32130",
        "command": "<repo>/.venv/bin/python -m pytest -p no:cacheprovider -q -rf "
        "--tb=line tests/test_crew_recovery.py tests/test_crew_ticker.py "
        "tests/test_crew_ticker_layout.py "
        "tests/test_ticker_node_names_keep_their_ends.py tests/test_crew_recover.py",
        "environment": "TERM=xterm-256color, NO_COLOR unset",
        "exit_status": 1,
        "log_path": "/home/ITER/mcintos/.config/reckon/crew/runs/"
        "r-20261003T130535115617-ticker-tests-hold-under-any-terminal/"
        "logs/base-arm-colour.log",
        "completed": True,
        "failure_count": 22,
        "failure_ids": [
            "tests/test_crew_recovery.py::test_cli_follow_streams_each_event_as_one_json_document",
            "tests/test_crew_recovery.py::test_snapshot_carries_every_field_the_ticker_column_set_reads",
            "tests/test_crew_recovery.py::test_an_aliased_pointer_renders_the_alias_not_the_model_id",
            "tests/test_crew_recovery.py::test_legacy_log_line_renders_and_new_line_renders_two_cells",
            "tests/test_crew_recovery.py::test_one_stored_new_line_renders_differently_at_two_display_settings",
            "tests/test_crew_ticker.py::test_cli_follow_prints_compact_transition_lines_by_default",
            "tests/test_crew_ticker.py::test_cli_follow_keeps_machine_objects_behind_json_flag",
            "tests/test_crew_ticker_layout.py::test_a_reason_is_truncated_to_the_room_the_grid_leaves",
            "tests/test_crew_ticker_layout.py::test_a_reason_that_fits_is_printed_whole",
            "tests/test_crew_ticker_layout.py::test_a_reason_clipped_at_the_margin_ends_on_a_word",
            "tests/test_crew_ticker_layout.py::test_a_narrow_width_is_widened_to_what_the_columns_need",
            "tests/test_crew_ticker_layout.py::test_a_narrow_width_still_widens_to_fit_the_role_column",
            "tests/test_crew_ticker_layout.py::test_the_model_and_effort_are_not_fused_into_one_cell",
            "tests/test_crew_ticker_layout.py::test_the_longest_role_model_and_effort_elide_within_budget",
            "tests/test_crew_ticker_layout.py::test_a_legacy_composed_string_renders_the_same_cells_as_separate_facts",
            "tests/test_crew_ticker_layout.py::test_the_effort_column_keeps_one_offset_whatever_the_alias_length",
            "tests/test_crew_ticker_layout.py::test_no_row_pads_the_widest_alias_before_its_effort",
            "tests/test_ticker_node_names_keep_their_ends.py::test_two_long_names_render_apart_at_the_default_width",
            "tests/test_ticker_node_names_keep_their_ends.py::test_a_name_that_fits_its_cell_is_unchanged",
            "tests/test_ticker_node_names_keep_their_ends.py::test_two_names_that_cut_alike_each_carry_their_own_mint",
            "tests/test_ticker_node_names_keep_their_ends.py::test_a_lone_long_name_carries_no_suffix",
            "tests/test_crew_recover.py::test_recovery_command_help_names_the_state_and_the_boundary",
        ],
    },
}

_TICKER_AFTER = {
    "dumb": {
        "revision": "e1537020a600759149bbbb7e4aacc216c6b8b5e3",
        "command": "<repo>/.venv/bin/python -m pytest -p no:cacheprovider -q -rf "
        "--tb=line tests/test_crew_recovery.py tests/test_crew_ticker.py "
        "tests/test_crew_ticker_layout.py "
        "tests/test_ticker_node_names_keep_their_ends.py tests/test_crew_recover.py",
        "environment": "TERM=dumb NO_COLOR=1",
        "exit_status": 0,
        "log_path": "/home/ITER/mcintos/.config/reckon/crew/runs/"
        "r-20261003T130535115617-ticker-tests-hold-under-any-terminal/"
        "logs/head-arm-dumb.log",
        "completed": True,
        "failure_count": 0,
        "summary": "215 passed in 112.76s (0:01:52)",
    },
    "colour": {
        "revision": "e1537020a600759149bbbb7e4aacc216c6b8b5e3",
        "command": "<repo>/.venv/bin/python -m pytest -p no:cacheprovider -q -rf "
        "--tb=line tests/test_crew_recovery.py tests/test_crew_ticker.py "
        "tests/test_crew_ticker_layout.py "
        "tests/test_ticker_node_names_keep_their_ends.py tests/test_crew_recover.py",
        "environment": "TERM=xterm-256color, NO_COLOR unset",
        "exit_status": 0,
        "log_path": "/home/ITER/mcintos/.config/reckon/crew/runs/"
        "r-20261003T130535115617-ticker-tests-hold-under-any-terminal/"
        "logs/head-arm-colour.log",
        "completed": True,
        "failure_count": 0,
        "summary": "215 passed in 95.71s (0:01:35)",
    },
}

_EARLY_BASELINE = {
    "revision": "b67d70a288a4b3cf0ac05351e7223c8fb20714c3",
    "population_command_files": [
        "tests/test_stored_phase_advances.py",
        "tests/test_spawn_retries_a_vanished_bind.py",
        "tests/test_worker_env_export.py",
    ],
    "population": "15 passed in 50.39s, EXIT=0",
    "named_reproduction": "1 failed in 15.69s, EXIT=1, assert None == 0 at "
    "tests/test_a_killed_worker_resumes_to_completion.py:328",
}

_EARLY_AFTER = {
    "revision": "0a524ce6f",
    "population_files": [
        "tests/test_stored_phase_advances.py",
        "tests/test_spawn_retries_a_vanished_bind.py",
        "tests/test_worker_env_export.py",
        "tests/test_an_early_exit_keeps_its_status.py",
    ],
    "population": "18 passed in 54.82s, EXIT=0",
    "named_alone": "1 passed in 25.19s, EXIT=0",
    "named_with_its_file": "1 passed in 25.89s, EXIT=0",
    "new_file_alone": "3 passed in 7.58s, EXIT=0",
}

# A suite record the dispatch template defines: one arm, one flat record.
_FLAT_RECORD = {
    "revision": "e1537020a600759149bbbb7e4aacc216c6b8b5e3",
    "command": "pytest -q tests/test_crew_ticker.py",
    "exit_status": 0,
    "log_path": "/durable/head-arm.log",
    "completed": True,
    "failure_count": 0,
    "failure_ids": [],
}


def _manifest(**fields: object) -> str:
    lines = [
        "node: a-suite-record-is-flat",
        "status: complete",
        "commits: abc123",
        "tests: pytest tests/test_a_suite_record_is_flat.py -> green",
    ]
    lines.extend(f"{name}: {json.dumps(value)}" for name, value in fields.items())
    return "\n".join(lines) + "\n"


def test_a_terminal_keyed_record_is_refused_naming_the_field() -> None:
    audit = reports.audit_manifest(
        _manifest(baseline_suite=_TICKER_BASELINE, after_suite=_TICKER_AFTER)
    )

    assert audit["ok"] is False
    assert any(
        finding.startswith("baseline_suite is not a flat suite record")
        for finding in audit["findings"]
    )
    assert any(
        finding.startswith("after_suite is not a flat suite record")
        for finding in audit["findings"]
    )


def test_a_measurement_keyed_record_is_refused_naming_the_field() -> None:
    audit = reports.audit_manifest(
        _manifest(baseline_suite=_EARLY_BASELINE, after_suite=_EARLY_AFTER)
    )

    assert audit["ok"] is False
    assert any(
        finding.startswith("baseline_suite is not a flat suite record")
        for finding in audit["findings"]
    )
    assert any(
        finding.startswith("after_suite is not a flat suite record")
        for finding in audit["findings"]
    )


def test_the_finding_lists_the_keys_a_flat_record_needs() -> None:
    audit = reports.audit_manifest(_manifest(baseline_suite=_TICKER_BASELINE))

    finding = next(
        finding
        for finding in audit["findings"]
        if finding.startswith("baseline_suite is not a flat suite record")
    )
    for key in (
        "revision",
        "command",
        "exit_status",
        "log_path",
        "completed",
        "failure_ids",
    ):
        assert key in finding


def test_a_flat_suite_record_audits_with_no_finding() -> None:
    audit = reports.audit_manifest(
        _manifest(baseline_suite=_FLAT_RECORD, after_suite=_FLAT_RECORD)
    )

    assert audit["ok"] is True, audit["findings"]
    assert (
        reports.audit_manifest(
            _manifest(baseline_suite=_FLAT_RECORD, after_suite=_FLAT_RECORD),
            suite_armed=True,
        )["ok"]
        is True
    )


def test_a_prose_not_run_statement_keeps_its_present_treatment() -> None:
    text = _manifest(
        baseline_suite="not run - the gate is a manifest check, not a suite",
        after_suite="not run - the same holds for the head arm",
    )

    audit = reports.audit_manifest(text)

    assert audit["ok"] is True, audit["findings"]
    assert audit["manifest"]["baseline_suite"] == (
        "not run - the gate is a manifest check, not a suite"
    )
    assert reports.audit_manifest(text, suite_armed=True)["findings"] == [
        "baseline_suite must be an inline JSON object",
        "after_suite must be an inline JSON object",
    ]
