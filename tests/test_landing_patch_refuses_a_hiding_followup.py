"""The HTTP patch validator refuses a followup that hides work.

The HTTP plan-write path merges a patch into the stored plan and then runs
``validate_landing_patch``, which refuses an open followup the patch adds whose
invocation hides work. It compares the patched followup list against the stored
one, so a patch that resends the whole list is not mistaken for an append.

No test passed a ``followups`` key through that validator, so dropping the call
or misreading the ids it stores would leave the suite green. The plans are
written into a temporary docs directory and read back through the store, so
each case drives the validator against real parsed plan state rather than
dicts shaped to please the predicate.
"""

from __future__ import annotations

import html
import importlib
import json
from pathlib import Path

import pytest

import reckon._store as _store_module

PROJECT = "temp-landing-patch-project"
HOST_DECLARATIONS = {"s1": "done", "s2": "implementable"}


def _write_plan(
    docs_dir: Path,
    slug: str,
    *,
    status: str = "active",
    declarations: dict[str, str] | None = None,
    followups: tuple[tuple[str, str], ...] = (),
) -> Path:
    """Write one plan HTML into ``docs_dir/plans`` in the store's own layout."""

    path = docs_dir / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    metas = [
        ("docs-project", PROJECT),
        ("reckon-type", "plan"),
        ("plan-slug", slug),
        ("plan-status", status),
        ("plan-modified", "2026-10-02"),
        (
            "plan-section-declarations",
            html.escape(json.dumps(declarations or {}, separators=(",", ":"))),
        ),
    ]
    head = "".join(f'<meta name="{name}" content="{value}">' for name, value in metas)
    articles = "".join(
        f'<article class="r-fu" data-id="{fid}" data-status="open"'
        f' data-written-by="test" data-written-at="2026-10-02">'
        f'<h4 class="r-fu-title">{fid}</h4>'
        f'<div class="r-fu-body"><p>{fid}</p></div>'
        f'<pre class="r-fu-prompt">{html.escape(prompt)}</pre>'
        "</article>"
        for fid, prompt in followups
    )
    path.write_text(
        "<!doctype html><html><head>"
        f"{head}<title>{slug}</title></head>"
        '<body><main class="plan-doc">'
        '<section data-reckon="followups" id="followups">'
        f"<h2>§ Followups</h2>{articles}</section>"
        "</main></body></html>",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def setup(tmp_path, monkeypatch):
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir()
    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text(json.dumps({PROJECT: str(docs_dir)}))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))

    import reckon.serve as serve_mod

    serve_mod._MOUNTS_FILE = mounts_file
    serve_mod._STATE_ROOT = state_root
    importlib.reload(_store_module)
    return docs_dir


def _followup(ident: str, prompt: str) -> dict:
    return {
        "id": ident,
        "title": ident,
        "body": "why",
        "written_by": "test",
        "written_at": "2026-10-02",
        "status": "open",
        "prompt": prompt,
    }


def _land(docs_dir: Path, slug: str, patch: dict) -> None:
    """Run the validator the HTTP write path runs, on the merged state."""

    import reckon.serve as serve_mod

    state, _version = _store_module.read_plan(PROJECT, slug)
    state.setdefault("project", PROJECT)
    state.setdefault("slug", slug)
    serve_mod._patch_into(state, patch)
    _store_module.validate_landing_patch(state, patch)


def test_a_landing_patch_appending_a_hiding_followup_is_refused(setup) -> None:
    _write_plan(setup, "host", declarations=HOST_DECLARATIONS)

    with pytest.raises(_store_module.OpError) as excinfo:
        _land(setup, "host", {"followups": [_followup("hides", "/reckon-build host")]})

    assert "hides" in str(excinfo.value)
    assert "no-section" in str(excinfo.value)


def test_the_same_patch_pointing_at_an_implementable_section_is_accepted(
    setup,
) -> None:
    _write_plan(setup, "host", declarations=HOST_DECLARATIONS)

    _land(
        setup,
        "host",
        {"followups": [_followup("points", "/reckon-build host §2")]},
    )


def test_resending_a_stored_hiding_followup_is_not_an_append(setup) -> None:
    _write_plan(
        setup,
        "host",
        declarations=HOST_DECLARATIONS,
        followups=(("legacy", "/reckon-build host"),),
    )
    stored, _version = _store_module.read_plan(PROJECT, "host")

    _land(
        setup,
        "host",
        {
            "followups": [
                *stored["followups"],
                _followup("points", "/reckon-build host §2"),
            ]
        },
    )


def test_the_appended_followup_is_the_one_named_and_refused(setup) -> None:
    _write_plan(
        setup,
        "host",
        declarations=HOST_DECLARATIONS,
        followups=(("legacy", "/reckon-build host"),),
    )
    stored, _version = _store_module.read_plan(PROJECT, "host")

    with pytest.raises(_store_module.OpError) as excinfo:
        _land(
            setup,
            "host",
            {
                "followups": [
                    *stored["followups"],
                    _followup("appended", "/reckon-build host §1"),
                ]
            },
        )

    message = str(excinfo.value)
    assert "appended" in message
    assert "legacy" not in message
    assert "section-not-implementable" in message