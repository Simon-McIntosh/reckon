"""Tests for layered flight-control configuration (reckon/flight.py).

Covers the measures the flight-control design commits to demonstrating:

  - the committed Pydantic model and JSON Schema regenerate from the LinkML
    source with no diff, so neither has been hand-edited into drift
  - all four layers resolve in precedence order, and a layer overriding one
    backend key leaves that backend's other keys standing — deep merge, not
    block replacement
  - every resolved key reports which layer supplied it
  - a malformed layer raises an error naming file, key path and constraint,
    and never falls back to defaults
  - backend availability is reported for present and missing commands alike
  - `reckon flight` emits parseable JSON with a stable key order, exiting
    non-zero only on a config error
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon.cli import main as cli_main
from reckon import crew
from reckon._flight_schema import BackendConfig
from reckon.flight import (
    FlightConfigError,
    deep_merge,
    flight_report,
    parse_overrides,
    probe_availability,
    resolve,
)

ROOT = Path(__file__).resolve().parent.parent


def write(path: Path, text: str) -> Path:
    """Write a config layer and return its path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture(autouse=True)
def isolated_host_config(monkeypatch, tmp_path):
    """Keep the machine-contract tests off this workstation's real host layer.

    Without this the suite's verdict would depend on whatever the developer
    happens to have configured, and a broken host config would fail tests that
    are not about it.
    """
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(tmp_path / "absent" / "flight.yaml"))


@pytest.fixture
def layers(tmp_path):
    """A three-file layer set with only the shipped layer populated."""
    return {
        "host": tmp_path / "host" / "flight.yaml",
        "project": tmp_path / "project" / "flight.yaml",
    }


def resolve_files(layers, *, overrides=None):
    """Resolve using explicit layer paths, isolated from the real host config."""
    return resolve(
        overrides=overrides,
        host_path=layers["host"],
        project_path=layers["project"],
    )


# ── 1. Generated artifacts match their LinkML source ────────────────────────


def _linkml_available() -> bool:
    return shutil.which("gen-pydantic") is not None and (
        shutil.which("gen-json-schema") is not None
    )


requires_linkml = pytest.mark.skipif(
    not _linkml_available(),
    reason="LinkML generators absent — install the dev dependency group",
)


@requires_linkml
def test_committed_pydantic_model_matches_linkml_source():
    """reckon/_flight_schema.py regenerates byte-identically from flight.yaml."""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import regen_flight_schema
    finally:
        sys.path.pop(0)

    committed = (ROOT / "reckon" / "_flight_schema.py").read_text()
    assert committed == regen_flight_schema.generate_pydantic(), (
        "_flight_schema.py is stale or hand-edited — regenerate with "
        "`uv run python scripts/regen_flight_schema.py`"
    )


@requires_linkml
def test_committed_json_schema_matches_linkml_source():
    """docs/_shared/flight.schema.json regenerates byte-identically."""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import regen_flight_schema
    finally:
        sys.path.pop(0)

    committed = (ROOT / "docs" / "_shared" / "flight.schema.json").read_text()
    assert committed == regen_flight_schema.generate_json_schema(), (
        "flight.schema.json is stale or hand-edited — regenerate with "
        "`uv run python scripts/regen_flight_schema.py`"
    )


def test_shipped_defaults_validate_against_the_schema():
    """The bottom layer is held to the same schema as every other layer."""
    resolved = resolve(host_path=Path("/nonexistent/flight.yaml"))
    assert resolved.config["version"] == 1
    assert resolved.config["default_backend"] in resolved.config["backends"]


def test_shipped_layer_names_no_provider_command_or_model():
    """Provider names, commands and model identifiers stay out of the package.

    The shipped layer may define only the calling harness, which needs no
    external command and no model identifier; anything else is user data that
    belongs in a host or project layer.
    """
    resolved = resolve(host_path=Path("/nonexistent/flight.yaml"))
    for backend in resolved.config["backends"].values():
        assert backend["launch"] == "in-harness"
        assert backend.get("command") is None
        assert backend.get("model") is None
        assert backend.get("effort") is None


def test_schema_enumerates_no_backend_names():
    """Backend keys are user data, so the schema fixes none of them."""
    schema = json.loads((ROOT / "docs" / "_shared" / "flight.schema.json").read_text())
    backends = schema["$defs"]["FlightConfig"]["properties"]["backends"]
    assert "enum" not in json.dumps(backends["additionalProperties"])


# ── 2. Layer precedence and deep merge ──────────────────────────────────────


def test_four_layers_resolve_in_precedence_order(layers):
    """Each layer overrides the one below it, override winning outright."""
    write(
        layers["host"],
        "default_backend: alpha\nbackends:\n  alpha:\n    launch: cli\n    command: alpha-cli\n    model: base\n",
    )
    write(layers["project"], "backends:\n  alpha:\n    model: project\n")

    resolved = resolve_files(layers)
    assert resolved.config["backends"]["alpha"]["model"] == "project"

    overridden = resolve_files(
        layers, overrides={"backends": {"alpha": {"model": "override"}}}
    )
    assert overridden.config["backends"]["alpha"]["model"] == "override"


def test_project_override_of_one_key_leaves_sibling_keys_standing(layers):
    """Deep merge, not block replacement — the gate this plan turns on."""
    write(
        layers["host"],
        "default_backend: alpha\n"
        "backends:\n"
        "  alpha:\n"
        "    launch: cli\n"
        "    command: alpha-cli\n"
        "    model: some-model\n"
        "    effort: high\n"
        "    time_budget: 12m\n",
    )
    write(layers["project"], "backends:\n  alpha:\n    model: other-model\n")

    backend = resolve_files(layers).config["backends"]["alpha"]
    assert backend["model"] == "other-model"
    # Every sibling key survives the override.
    assert backend["command"] == "alpha-cli"
    assert backend["effort"] == "high"
    assert backend["time_budget"] == "12m"
    assert backend["launch"] == "cli"
    # And keys the host layer never mentioned still come from shipped defaults.
    assert resolve_files(layers).config["gates"]["enforce"] == "strict"


def test_the_shipped_layer_carries_the_budget_thresholds(layers):
    """The thresholds a hold is decided by are data, reported per layer like any."""
    resolved = resolve_files(layers)
    thresholds = resolved.config["budget"]

    assert thresholds["utilisation_ceiling_pct"] == 100
    assert thresholds["resume_reserve_pct"] == 5
    # Empty by default: naming which threshold statuses count as exhausted is the
    # config's job, so no backend's vocabulary is enumerated by the schema.
    assert thresholds["exhausted_statuses"] == []
    assert resolved.origin("budget.resume_reserve_pct") == "shipped"


def test_a_project_may_tighten_the_budget_ceiling_alone(layers):
    write(layers["project"], "budget:\n  utilisation_ceiling_pct: 80\n")
    resolved = resolve_files(layers)

    assert resolved.config["budget"]["utilisation_ceiling_pct"] == 80
    assert resolved.config["budget"]["resume_reserve_pct"] == 5
    assert resolved.origin("budget.utilisation_ceiling_pct") == "project"
    assert resolved.origin("budget.resume_reserve_pct") == "shipped"


def test_account_limit_checks_are_an_explicit_host_opt_in(layers):
    write(layers["host"], "backends:\n  native:\n    budget_check: true\n")

    resolved = resolve_files(layers)

    assert resolved.config["backends"]["native"]["budget_check"] is True
    assert resolved.origin("backends.native.budget_check") == "host"


def test_a_utilisation_ceiling_beyond_a_percentage_is_rejected(layers):
    write(layers["host"], "budget:\n  utilisation_ceiling_pct: 140\n")
    with pytest.raises(FlightConfigError) as excinfo:
        resolve_files(layers)
    assert excinfo.value.key_path == "budget.utilisation_ceiling_pct"


def test_a_hold_is_a_summary_occasion(layers):
    """A held wave reports like a dispatched one, so the occasion is nameable."""
    assert "hold" in resolve_files(layers).config["summary"]["at"]


def test_shipped_backend_survives_a_host_layer_adding_another(layers):
    """Adding a backend must not delete the one that shipped."""
    write(
        layers["host"],
        "backends:\n  alpha:\n    launch: cli\n    command: alpha-cli\n",
    )
    backends = resolve_files(layers).config["backends"]
    assert set(backends) == {"native", "alpha"}


def test_deep_merge_replaces_lists_wholesale():
    """A list-valued key is one choice, so it replaces rather than accumulates."""
    merged = deep_merge(
        {"summary": {"at": ["dispatch", "completion"]}},
        {"summary": {"at": ["dispatch"]}},
    )
    assert merged["summary"]["at"] == ["dispatch"]


def test_absent_layers_are_not_an_error(layers):
    """Most installs have no project layer; that is normal, not a failure."""
    resolved = resolve_files(layers)
    present = {layer.name: layer.present for layer in resolved.layers}
    assert present["shipped"] is True
    assert present["host"] is False
    assert present["project"] is False


# ── 3. Provenance ───────────────────────────────────────────────────────────


def test_every_resolved_key_reports_its_originating_layer(layers):
    """A resolved value with no visible origin is untunable."""
    write(
        layers["host"],
        "default_backend: alpha\n"
        "backends:\n"
        "  alpha:\n"
        "    launch: cli\n"
        "    command: alpha-cli\n"
        "    model: host-model\n",
    )
    write(layers["project"], "backends:\n  alpha:\n    model: project-model\n")
    resolved = resolve_files(layers, overrides={"gates": {"enforce": "advisory"}})

    assert resolved.origin("backends.alpha.command") == "host"
    assert resolved.origin("backends.alpha.model") == "project"
    assert resolved.origin("gates.enforce") == "override"
    assert resolved.origin("gates.on_fail") == "shipped"
    assert resolved.origin("roles.implement.time_budget") == "shipped"


def test_provenance_covers_every_leaf_of_the_resolved_config(layers):
    """No resolved leaf is left without an origin."""
    resolved = resolve_files(layers)

    def leaves(node, prefix=""):
        for key, value in node.items():
            path = f"{prefix}{key}"
            if isinstance(value, dict) and value:
                yield from leaves(value, f"{path}.")
            else:
                yield path

    missing = [
        path for path in leaves(resolved.config) if path not in resolved.provenance
    ]
    assert missing == []


# ── 4. A malformed layer fails loudly ───────────────────────────────────────


def test_invalid_enum_names_file_key_and_constraint(layers):
    """An unknown enum value stops resolution and says exactly what is wrong."""
    write(layers["host"], "gates:\n  enforce: aggressive\n")
    with pytest.raises(FlightConfigError) as excinfo:
        resolve_files(layers)
    error = excinfo.value
    assert str(layers["host"]) == error.source
    assert error.key_path == "gates.enforce"
    assert "strict" in error.constraint


def test_unknown_key_names_file_key_and_constraint(layers):
    """A typo'd key is an error, not a silently ignored setting."""
    write(layers["host"], "gates:\n  enfoce: strict\n")
    with pytest.raises(FlightConfigError) as excinfo:
        resolve_files(layers)
    error = excinfo.value
    assert str(layers["host"]) == error.source
    assert error.key_path == "gates.enfoce"
    assert "not permitted" in error.constraint


def test_malformed_layer_never_falls_back_to_defaults(layers):
    """The failure mode this rules out: bad config that looks like it worked."""
    write(layers["host"], "gates:\n  enforce: aggressive\n")
    with pytest.raises(FlightConfigError):
        resolve_files(layers)


def test_out_of_range_value_is_rejected(layers):
    """A grace below one would stop a worker before its allowance elapsed."""
    write(
        layers["host"],
        "fences:\n  budget_grace_multiple: 0.5\n",
    )
    with pytest.raises(FlightConfigError) as excinfo:
        resolve_files(layers)
    assert excinfo.value.key_path == "fences.budget_grace_multiple"


def test_retired_backend_concurrency_is_ignored_with_a_runtime_warning(layers):
    write(layers["host"], "backends:\n  native:\n    concurrency: 9\n")

    resolved = resolve_files(layers)
    resolution = crew.plan_dispatch(
        node=crew.TaskNode(
            id="compatibility-reader",
            goal="read a compatible host configuration",
            plan="plan-a",
            done_when=(
                "uv run pytest tests/test_flight.py reports 0 failures and the "
                "resolved warning names the retired declaration"
            ),
            write_paths=["reckon/flight.py"],
        ),
        config=resolved.config,
    )

    assert "concurrency" not in BackendConfig.model_fields
    assert "concurrency" not in resolved.config["backends"]["native"]
    assert resolved.origin("backends.native.concurrency") is None
    assert len(resolved.warnings) == 1
    assert "retired and was ignored" in resolved.warnings[0]
    assert resolution.validation.ok
    assert resolution.warnings == resolved.warnings


def test_malformed_time_budget_is_rejected(layers):
    """A duration reckon cannot parse is caught at the config boundary."""
    write(layers["host"], "fences:\n  time_budget: soon\n")
    with pytest.raises(FlightConfigError) as excinfo:
        resolve_files(layers)
    assert excinfo.value.key_path == "fences.time_budget"


def test_unparseable_yaml_names_the_file(layers):
    write(layers["host"], "backends: [unclosed\n")
    with pytest.raises(FlightConfigError) as excinfo:
        resolve_files(layers)
    assert str(layers["host"]) == excinfo.value.source


def test_default_backend_without_a_backend_is_an_error(layers):
    """Cross-layer rule: the named backend must exist once everything merges."""
    write(layers["host"], "default_backend: absent\n")
    with pytest.raises(FlightConfigError) as excinfo:
        resolve_files(layers)
    assert excinfo.value.key_path == "default_backend"
    assert "absent" in excinfo.value.constraint


def test_role_naming_an_undefined_backend_is_an_error(layers):
    write(layers["host"], "roles:\n  review:\n    backend: absent\n")
    with pytest.raises(FlightConfigError) as excinfo:
        resolve_files(layers)
    assert excinfo.value.key_path == "roles.review.backend"


def test_gates_off_spelled_as_a_yaml_boolean_is_rejected(layers):
    """`enforce: off` parses as false in YAML, so the schema spells it out."""
    write(layers["host"], "gates:\n  enforce: off\n")
    with pytest.raises(FlightConfigError) as excinfo:
        resolve_files(layers)
    assert excinfo.value.key_path == "gates.enforce"

    write(layers["host"], "gates:\n  enforce: disabled\n")
    assert resolve_files(layers).config["gates"]["enforce"] == "disabled"


# ── 5. Availability ─────────────────────────────────────────────────────────


def test_available_command_is_reported_with_its_resolved_path():
    config = {
        "backends": {
            "alpha": {"launch": "cli", "command": "python3"},
        }
    }
    entry = probe_availability(config)["alpha"]
    assert entry["command_found"] is True
    assert entry["command_path"] == shutil.which("python3")


def test_missing_command_is_reported_not_acted_on():
    """A missing backend stays in the config; the caller decides to degrade."""
    config = {
        "backends": {
            "alpha": {"launch": "cli", "command": "reckon-no-such-binary"},
        }
    }
    entry = probe_availability(config)["alpha"]
    assert entry["command_found"] is False
    assert entry["command_path"] is None
    assert "not on PATH" in entry["detail"]


def test_in_harness_backend_needs_no_command():
    entry = probe_availability({"backends": {"native": {"launch": "in-harness"}}})[
        "native"
    ]
    assert entry["command_found"] is True
    assert entry["command"] is None


def test_auth_check_is_not_run_unless_asked_for():
    config = {
        "backends": {
            "alpha": {"launch": "cli", "command": "python3", "auth_check": ["false"]},
        }
    }
    assert probe_availability(config)["alpha"]["authenticated"] is None
    assert probe_availability(config)["alpha"]["detail"] == "auth_check not run"


def test_auth_check_exit_status_is_reported():
    ok = {
        "backends": {
            "alpha": {"launch": "cli", "command": "python3", "auth_check": ["true"]},
        }
    }
    bad = {
        "backends": {
            "alpha": {"launch": "cli", "command": "python3", "auth_check": ["false"]},
        }
    }
    assert probe_availability(ok, probe_auth=True)["alpha"]["authenticated"] is True
    assert probe_availability(bad, probe_auth=True)["alpha"]["authenticated"] is False


def test_backend_declaring_cli_without_a_command_is_flagged():
    entry = probe_availability({"backends": {"alpha": {"launch": "cli"}}})["alpha"]
    assert entry["command_found"] is False
    assert "no command" in entry["detail"]


# ── 6. The machine contract ─────────────────────────────────────────────────


def run_cli(*args):
    return CliRunner().invoke(cli_main, ["flight", *args])


def test_default_stdout_parses_as_json_and_exits_zero():
    result = run_cli()
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert set(payload) == {
        "availability",
        "config",
        "layers",
        "project",
        "provenance",
        "warnings",
    }
    assert payload["warnings"] == []


def test_key_order_is_stable_across_runs():
    first = run_cli().output
    second = run_cli().output
    assert first == second
    payload = json.loads(first)
    assert list(payload) == sorted(payload)
    assert list(payload["config"]) == sorted(payload["config"])


def test_pretty_is_the_same_document_indented():
    assert json.loads(run_cli("--pretty").output) == json.loads(run_cli().output)
    assert "\n  " in run_cli("--pretty").output


def test_invalid_config_exits_non_zero(tmp_path, monkeypatch):
    bad = write(tmp_path / "flight.yaml", "gates:\n  enforce: aggressive\n")
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(bad))
    result = run_cli()
    assert result.exit_code != 0
    assert "gates.enforce" in result.output


def test_override_flag_reaches_the_override_layer():
    payload = json.loads(run_cli("--set", "gates.enforce=advisory").output)
    assert payload["config"]["gates"]["enforce"] == "advisory"
    assert payload["provenance"]["gates.enforce"] == "override"


def test_overrides_parse_as_yaml_scalars():
    parsed = parse_overrides(
        ["roles.review.session_reuse=true", "gates.require_evidence=false"]
    )
    assert parsed == {
        "roles": {"review": {"session_reuse": True}},
        "gates": {"require_evidence": False},
    }


def test_malformed_override_is_rejected():
    with pytest.raises(FlightConfigError):
        parse_overrides(["gates.enforce"])


def test_report_includes_availability_and_layer_inventory():
    report = flight_report(host_path=Path("/nonexistent/flight.yaml"))
    assert [layer["name"] for layer in report["layers"]] == [
        "shipped",
        "host",
        "project",
        "override",
    ]
    assert "native" in report["availability"]
