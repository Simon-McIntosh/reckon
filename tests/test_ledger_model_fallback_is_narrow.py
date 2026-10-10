"""The ledger's model fallback is narrow: malformed catalogue, not ambiguity.

``build_record`` resolves the row's ``lane`` and ``model_key`` through the
catalogue's one name resolver. Two failures can meet it there and they call for
opposite handling. A catalogue that cannot be read or parsed is the catalogue's
fault, so the fallback leaves the pair unset and the row still lands. An
ambiguous name is the caller's fault, and the resolver raises it as a
``ValueError``: the fallback must not catch it, so the refusal surfaces rather
than being stored as an unset pair that quietly drops the name two lanes claim.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from reckon import ledger


def _write_catalogue(monkeypatch: pytest.MonkeyPatch, path: Path, text: str) -> None:
    path.write_text(text)
    monkeypatch.setenv("RECKON_MODEL_CATALOGUE", str(path))


def test_malformed_catalogue_writes_the_row_with_an_unset_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A catalogue that will not parse is not the run's fault: the row lands.

    The top level is a list rather than a mapping, so the catalogue layer is
    malformed and resolution raises ``FlightConfigError``. The fallback is what
    keeps the write from failing, and this test is what fails when it is
    removed.
    """
    catalogue = tmp_path / "catalogue.yaml"
    _write_catalogue(monkeypatch, catalogue, "- not\n- a\n- mapping\n")

    row = ledger.build_record(
        run_id="r-malformed-catalogue",
        plan="plan-a",
        gate="passed",
        backend="claude-opus",
        agent={"backend": "claude-opus"},
    )

    assert row["lane"] is None
    assert row["model_key"] is None


def test_ambiguous_name_surfaces_rather_than_storing_an_unset_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two lanes declaring one bare name refuse, and the refusal names them.

    On a resolver that catches the ambiguity, this row is written with the pair
    unset and the refusal is lost. The fallback narrowed to the catalogue's own
    fault vocabulary lets the ``ValueError`` through.
    """
    catalogue = tmp_path / "catalogue.yaml"
    _write_catalogue(
        monkeypatch,
        catalogue,
        yaml.safe_dump(
            {
                "version": 1,
                "lanes": {
                    "claude": {
                        "default_model": "sonnet",
                        "models": {"opus": {"model": "claude-opus-5-5"}},
                    },
                    "secondary": {
                        "default_model": "opus",
                        "models": {"opus": {"model": "claude-opus-5-5"}},
                    },
                },
            }
        ),
    )

    with pytest.raises(ValueError, match="ambiguous routing name") as refusal:
        ledger.build_record(
            run_id="r-ambiguous-name",
            plan="plan-a",
            gate="passed",
            backend="opus",
            agent={"backend": "opus"},
        )

    detail = str(refusal.value)
    assert "claude" in detail and "secondary" in detail
