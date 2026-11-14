"""Whether a section has an unlanded run reads discard records only for its rows.

The admission pass asks this for every waiting request. Checking every ledger
row's discard record, a file lookup on shared storage, before matching the
row's section made one pass take many minutes with thirty requests waiting.
"""

from __future__ import annotations

from reckon.crew.admission import readiness


def test_rows_of_other_sections_never_reach_the_discard_check(monkeypatch) -> None:
    rows = [
        {
            "run_id": f"r-{index}",
            "promoted_revision": "a" * 40,
            "plan": "other",
            "section": "s1",
        }
        for index in range(100)
    ]
    monkeypatch.setattr(readiness, "_ledger_run_rows", lambda project, cache: rows)
    checked: list[str] = []

    def discarded(run_id: str) -> bool:
        checked.append(run_id)
        return False

    monkeypatch.setattr(readiness._release, "_discarded", discarded)

    held = readiness._section_run_unlanded("p", "wanted-plan", "s2", live=[])

    assert held is False
    assert checked == []
