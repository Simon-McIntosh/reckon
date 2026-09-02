from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import reckon.crew as crew


FIXTURE = Path(__file__).parent / "fixtures" / "crew-public-surface.txt"
MODULES = (
    "node",
    "routing",
    "runs",
    "prompts",
    "dispatch",
    "promotion",
    "recovery",
    "reports",
    "summary",
    "ticker",
)


def test_public_surface_matches_snapshot() -> None:
    expected = set(FIXTURE.read_text().splitlines())
    actual = {name for name in dir(crew) if not name.startswith("_")}

    assert actual == expected


def test_concern_modules_import_in_fresh_interpreters() -> None:
    for module in MODULES:
        result = subprocess.run(
            [sys.executable, "-c", f"import reckon.crew.{module}"],
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr
