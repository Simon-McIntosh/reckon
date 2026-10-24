"""One reader owns each figure row and each plan-file parse on the read path.

The metadata index and the Figures inventory each derived a figure's row, and
the two carried different fields: the index row served no ``caption``, ``dims``,
``download``, ``for_plan`` or ``path``. The crew poll parsed a referenced plan
through its own open and parse, so a plan the memoised reader had already parsed
was opened again.

The index now takes its figure row whole from ``figures.figure_rows`` and adds
only the fields the index alone carries. The crew poll reads a plan's parse from
``_plan_html.read_state_and_text_file``, which owns and memoises it.

Each case carries a control applied as a mutation:

1. keep the index deriving its figure row on its own, so the row lacks the keys
   the figures builder gives, and the first case fails;
2. let the crew poll open the plan itself, and the second case's open count
   rises above zero for an already-parsed plan.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from reckon import _plan_html, figures, file_memo, metadata_index, serve

_PROJECT = "sample"
_PLAN = "visible-work"


def _png_bytes(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
    )


def _plan_html_text() -> str:
    return (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{_PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{_PLAN}">'
        '<meta name="plan-sprint" content="current">'
        '<meta name="plan-effort-hours" content="3.25">'
        "<title>Visible work</title>"
        "</head><body></body></html>"
    )


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    config_home = tmp_path / "config"
    config_home.mkdir()
    mounts_file = config_home / "mounts.json"
    mounts_file.write_text(
        json.dumps({_PROJECT: str(tmp_path / "repository" / "docs")})
    )
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    metadata_index.clear()
    file_memo.clear()
    yield
    metadata_index.clear()
    file_memo.clear()


@pytest.fixture()
def docs(tmp_path) -> Path:
    """A docs tree with a captioned PNG and a plan a crew row reads."""

    figures_dir = tmp_path / "repository" / "docs" / "figures" / "work"
    figures_dir.mkdir(parents=True)
    (figures_dir / "still.png").write_bytes(_png_bytes(320, 200))
    (figures_dir / "captured.png").write_bytes(_png_bytes(100, 50))
    (figures_dir / "capture-index.json").write_text(
        json.dumps(
            {
                "captures": [
                    {
                        "capture": "control-layout",
                        "image": "captured.png",
                        "description": "Control layout at reading width",
                    }
                ]
            }
        )
    )
    plans = tmp_path / "repository" / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / f"{_PLAN}.html").write_text(_plan_html_text(), encoding="utf-8")
    return tmp_path / "repository" / "docs"


def test_the_index_figure_row_carries_every_key_figures_gives(docs: Path) -> None:
    """An index figure row carries the figures row's keys at equal values.

    The index endpoint serves ``ROW_FIELDS``, so the row compared here is the
    one the index's builder produces; the projection to the served shape
    happens where the row is stored and is the same one this case leaves
    alone.
    """

    inventory = {row["path"]: row for row in figures.figure_rows(docs, _PROJECT, set())}
    assert inventory, "the figures inventory served no rows"

    for path, inventory_row in inventory.items():
        index_row = metadata_index._figure_index_row(
            path, docs, _PROJECT, [], lambda _path, _signature: (0, "")
        )
        assert index_row is not None, path

        missing = set(inventory_row) - set(index_row)
        assert not missing, (path, sorted(missing))
        for field, expected in inventory_row.items():
            assert index_row[field] == expected, (path, field)


def test_a_crew_plan_read_opens_no_parsed_plan_again(
    docs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crew plan-details read reuses the parse the memoised reader holds."""

    plan = docs / "plans" / f"{_PLAN}.html"

    # The memoised reader takes ``plan``'s parse first, as another reader of the
    # same plan would before the crew poll runs.
    _plan_html.read_state_and_text_file(plan)

    opened: list[Path] = []
    original_open = Path.open

    def counting_open(self, *args, **kwargs):
        opened.append(Path(self))
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting_open)

    sprint, effort_hours = serve._crew_plan_details(docs, _PLAN)

    assert (sprint, effort_hours) == ("current", 3.25)
    assert opened == [], [str(path) for path in opened]
