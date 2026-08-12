#!/usr/bin/env python3
"""Regenerate the committed artifacts derived from ``reckon/schema/flight.yaml``.

    uv run python scripts/regen_flight_schema.py

The LinkML source is authoritative. Both outputs — the Pydantic model reckon
imports at runtime and the JSON Schema served for editor completion — are
committed so that reading a flight config needs none of the generator
toolchain. Never hand-edit either output: run this, and the round-trip test
proves the committed copies match their source.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "reckon" / "schema" / "flight.yaml"
PYDANTIC_OUT = ROOT / "reckon" / "_flight_schema.py"
JSON_SCHEMA_OUT = ROOT / "docs" / "_shared" / "flight.schema.json"

BANNER = (
    "# Generated from reckon/schema/flight.yaml — do not edit.\n"
    "# Regenerate with: uv run python scripts/regen_flight_schema.py\n"
)


def _run(command: list[str]) -> str:
    """Run a generator and return its stdout, failing loudly on a bad exit."""
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(
            f"{command[0]} failed ({result.returncode}):\n{result.stderr.strip()}"
        )
    return result.stdout


def generate_pydantic() -> str:
    """Return the Pydantic module text for the flight schema."""
    body = _run(
        [
            "gen-pydantic",
            "--extra-fields",
            "forbid",
            "--meta",
            "None",
            str(SOURCE),
        ]
    )
    return BANNER + body


def generate_json_schema() -> str:
    """Return the JSON Schema text for the flight schema."""
    text = _run(["gen-json-schema", "--closed", str(SOURCE)])
    return text if text.endswith("\n") else text + "\n"


def main() -> int:
    PYDANTIC_OUT.write_text(generate_pydantic())
    JSON_SCHEMA_OUT.write_text(generate_json_schema())
    print(f"wrote {PYDANTIC_OUT.relative_to(ROOT)}")
    print(f"wrote {JSON_SCHEMA_OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
