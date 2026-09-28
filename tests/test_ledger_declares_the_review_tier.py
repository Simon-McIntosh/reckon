"""The promoted ledger schema declares the review tier its rows carry.

``reckon/crew/promotion.py`` writes ``review_tier`` on every promoted row, and
nineteen committed rows under ``docs/state/reckon/runs`` already carry it, so the
key is part of the promoted shape. ``RECORD_FIELDS`` omitted it, which the
declared-field check in ``tests/test_record_fields_declare_every_key.py`` reports
as an undeclared key the moment a promoted row is read.

This case builds one synthesised promoted row carrying the tier, mounts it under
project state, and runs that same declared-field check over the mounted rows. The
collection step calls the check's own ``_promoted_ledger_key_union`` rather than a
second copy of the mounted-ledger walk, so the two agree by construction on which
keys are observed.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from reckon import ledger
from tests.test_record_fields_declare_every_key import _promoted_ledger_key_union

PROJECT = "proj"


def _mount_synthesised_promoted_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, row: Mapping[str, Any]
) -> Path:
    """Write one promoted row under mounted project state and return the checkout.

    The declared-field check resolves mounted projects through the config home,
    so the row has to sit where the mount registry points, not beside the test.
    """
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.delenv("RECKON_MOUNTS_PATH", raising=False)

    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )

    path = ledger.run_path(PROJECT, str(row["run_id"]), root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ledger.serialize_run(row), encoding="utf-8")
    return root


def test_review_tier_is_a_declared_record_field() -> None:
    """The key promotion writes on every promotion is in the declared schema."""
    assert "review_tier" in ledger.RECORD_FIELDS


def test_a_promoted_row_carrying_review_tier_has_no_undeclared_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A synthesised promoted row carrying review_tier leaves no undeclared key.

    The row is assembled through ``ledger.build_record`` -- the same builder
    promotion uses -- so the case exercises the real promoted shape rather than a
    hand-picked key set, and the tier is then set exactly as promotion sets it.
    """
    row = ledger.build_record(
        run_id="r-20260928T120000000000-synthesised",
        plan="plan-a",
        gate="passed",
    )
    row["review_tier"] = "light"
    _mount_synthesised_promoted_row(tmp_path, monkeypatch, row)

    observed, mount_paths = _promoted_ledger_key_union()

    assert observed, (
        "the mounted synthesised promoted row was not read; checked mounted "
        "ledger paths: " + ", ".join(mount_paths)
    )
    assert "review_tier" in observed, (
        "the mounted row's tier was not collected: " + ", ".join(sorted(observed))
    )

    undeclared = sorted(observed - set(ledger.RECORD_FIELDS))
    assert not undeclared, "promoted ledger rows carry undeclared keys: " + ", ".join(
        undeclared
    )
