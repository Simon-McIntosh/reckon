"""Loading a flight config must not be broken by a key it does not know.

One flight config file is shared by servers, producers and workers of several
ages: a long-running MCP server, a dispatch loop and a worker all read the same
host layer, and they do not upgrade together. So a key added by the newest
writer reaches readers that predate it, and at load such a key is ignored and
reported rather than raised.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon.cli import main as cli_main
from reckon.flight import FlightConfigError, flight_report, resolve

# The newer-code key paths the fixture carries: one at the top level and one
# nested inside a declared namespace. Both are the shape a reader meets when a
# writer ahead of it lands a key, and both must come back on the report.
TOP_LEVEL_KEY = "a_key_from_newer_code"
NESTED_KEY = "a_key_from_newer_code"
NESTED_PATH = f"budget.{NESTED_KEY}"


def write(path: Path, text: str) -> Path:
    """Write a config layer and return its path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def newer_config(tmp_path: Path) -> Path:
    """A host layer carrying two keys the running schema does not declare."""
    return write(
        tmp_path / "host" / "flight.yaml",
        f"{TOP_LEVEL_KEY}: 1\nbudget:\n  {NESTED_KEY}: 2\n",
    )


def test_a_config_with_newer_keys_loads_and_reports_their_paths(tmp_path):
    """A newer config must not stop an older reader, and must say what it ignored.

    On a schema that refuses undeclared keys this resolution raises
    ``Extra inputs are not permitted``, which is the failure this node exists
    to remove.
    """
    host = newer_config(tmp_path)

    resolved = resolve(host_path=host)

    assert resolved.unknown_keys == [TOP_LEVEL_KEY, NESTED_PATH]


def test_the_flight_report_carries_the_unknown_key_paths(tmp_path):
    """The report a reader sees names the keys the running code did not know."""
    host = newer_config(tmp_path)

    report = flight_report(host_path=host)

    assert report["flight_unknown_keys"] == [TOP_LEVEL_KEY, NESTED_PATH]


def test_a_config_the_schema_fully_declares_reports_no_unknown_keys(tmp_path):
    """The control: reported paths come from the config, not from the checker:
    a layer the schema declares reports nothing, so a non-empty list above is
    evidence of a key and not of a checker that always finds something.
    """
    host = write(tmp_path / "host" / "flight.yaml", "gates:\n  enforce: strict\n")

    resolved = resolve(host_path=host)

    assert resolved.unknown_keys == []


def test_check_refuses_a_newer_key_that_the_load_ignored(tmp_path, monkeypatch):
    """The same config that loads is refused by the strict authoring check."""
    host = newer_config(tmp_path)
    monkeypatch.setenv("RECKON_FLIGHT_CONFIG", str(host))

    result = CliRunner().invoke(cli_main, ["flight", "--check"])

    assert result.exit_code != 0
    assert TOP_LEVEL_KEY in result.output
    assert "Extra inputs are not permitted" in result.output


def test_the_strict_check_refuses_a_mistyped_declared_namespace(tmp_path):
    """A typo inside a declared block is a key named ``budget.enfoce``, not an
    unknown namespace: the walk follows the declaration, so it names the typo
    rather than the block that holds it.
    """
    host = write(tmp_path / "host" / "flight.yaml", "budget:\n  enfoce: 1\n")

    with pytest.raises(FlightConfigError) as excinfo:
        resolve(host_path=host, strict=True)

    assert excinfo.value.key_path == "budget.enfoce"


def test_a_type_error_on_a_declared_key_is_still_refused_at_load(tmp_path):
    """Forward compatibility is for keys, not for shapes: a declared key that
    holds the wrong type is still a configuration error when it is read.
    """
    host = write(
        tmp_path / "host" / "flight.yaml",
        "domain:\n"
        "  invariants:\n"
        "    - id: one\n"
        "      rule: a rule\n"
        "      applies_to: not_a_list\n",
    )

    with pytest.raises(FlightConfigError) as excinfo:
        resolve(host_path=host)

    assert excinfo.value.key_path == "domain.invariants.0.applies_to"
