"""The agent-facing vocabulary names every op edit_plan sends and view read_plan serves.

An agent discovers edit_plan's ops from ``_OP_VOCAB`` and read_plan's views from
the tool docstring. Each is a hand-maintained list beside the machinery that
implements it, so an op or view can exist and work while remaining
undiscoverable. These tests hold the two lists against their sources: the op
dispatch table in ``reckon/_store.py`` and the accepted view names in
``reckon/mcp_views.py``.
"""

from __future__ import annotations

from reckon._store import _OP_DISPATCH
from reckon.mcp import _OP_VOCAB, _read_plan_tool
from reckon.mcp_views import VIEW_NAMES


def test_op_vocab_names_every_dispatchable_op() -> None:
    missing = sorted(set(_OP_DISPATCH) - set(_OP_VOCAB))
    assert not missing, (
        f"_OP_VOCAB omits dispatchable ops {missing}; an op that exists in "
        f"_OP_DISPATCH but not the vocabulary is undiscoverable to an agent"
    )


def test_insert_section_entry_names_its_keys_and_its_placement() -> None:
    entry = _OP_VOCAB["insert_section"]
    for key in ("id", "title", "body"):
        assert key in entry, f"insert_section vocabulary omits its {key!r} key"
    assert "before the first structured-state region" in entry
    assert "data-reckon='section'" in entry


def test_read_plan_docstring_names_every_accepted_view() -> None:
    docstring = _read_plan_tool.__doc__ or ""
    missing = sorted(view for view in VIEW_NAMES if f"'{view}'" not in docstring)
    assert not missing, (
        f"read_plan docstring omits accepted views {missing}; a view that "
        f"read_plan serves but the docstring hides is undiscoverable to an agent"
    )
    assert "``section`` argument" in docstring
