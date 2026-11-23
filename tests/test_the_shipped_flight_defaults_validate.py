"""The shipped flight defaults validate against the flight schema.

A key added to ``reckon/schema/flight-defaults.yaml`` without its declaration in
``reckon/schema/flight.yaml`` makes every strict resolution refuse with "Extra
inputs are not permitted". On 23 November the ``settle`` block did exactly that,
and 19 dispatch tests failed for two days while the selection that ran on each
landing never reached them.
"""

from __future__ import annotations

from pathlib import Path

from reckon import flight


def test_strict_resolution_of_the_shipped_defaults_succeeds(tmp_path: Path) -> None:
    empty = tmp_path / "empty.yaml"
    empty.write_text("{}\n", encoding="utf-8")
    resolved = flight.resolve(
        host_path=empty, project_path=empty, checkout_path=tmp_path, strict=True
    )
    assert resolved.config.get("settle", {}).get("quiet_after_hours") == 6.0
