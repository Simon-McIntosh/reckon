"""An appended decision keeps its question and its options.

A decision appended with the spelling the open-decisions view presents
(``question`` and ``options``) used to land as a block whose question paragraph
fell back to the key and which rendered no options at all: the append returned
ok, so nothing reported the loss. These cases append such a decision, read it
back through this repository's own reader, and assert the question text and
every option value survive.

Hermetic fixture mirrors tests/test_edit_plan.py: temp docs dir + temp state
root via RECKON_MOUNTS_PATH / RECKON_STATE_ROOT, with _store and mcp reloaded.
Every case also asserts the REAL state directory -- resolved from the ambient
environment at import, before any monkeypatch -- is untouched afterwards.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

import reckon._store as _store_module
import reckon.mcp as mcp_module


def _real_state_root() -> Path:
    """Resolved from the ambient environment at import, before any monkeypatch."""
    env = os.environ.get("RECKON_STATE_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    xdg = Path.home() / ".config" / "reckon"
    base = xdg if xdg.exists() else Path.home() / "docs-server"
    return base / "state"


_REAL_STATE_ROOT = _real_state_root()


def _fingerprint(root: Path) -> list[tuple[str, int]]:
    if not root.exists():
        return []
    return [
        (str(p.relative_to(root)), p.stat().st_size)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    ]


@pytest.fixture(autouse=True)
def _real_state_untouched():
    before = _fingerprint(_REAL_STATE_ROOT)
    yield
    assert _fingerprint(_REAL_STATE_ROOT) == before, (
        "a decision test wrote into the real state directory "
        f"{_REAL_STATE_ROOT} rather than its temporary one"
    )


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    """Hermetic temp docs dir + mounts + state root. Returns (docs, state, proj)."""
    project = "proj"
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir()

    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text(json.dumps({project: str(docs_dir)}))

    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))

    import reckon.serve as serve_mod

    serve_mod._MOUNTS_FILE = mounts_file
    serve_mod._STATE_ROOT = state_root

    importlib.reload(_store_module)
    importlib.reload(mcp_module)

    return docs_dir, state_root, project


def _make_plan(docs_dir: Path, slug: str, state: dict) -> Path:
    from reckon._plan_html import write_state

    base = dict(state)
    base.setdefault("slug", slug)
    base.setdefault("title", slug.title())
    base.setdefault("status", "active")
    base.setdefault("type", "plan")
    bare = (
        "<!doctype html>\n"
        '<html lang="en">\n<head>'
        '<meta charset="utf-8">'
        '<meta name="docs-project" content="proj">'
        f"<title>{slug}</title></head>\n"
        '<body><main class="plan-doc"></main></body>\n</html>\n'
    )
    html = write_state(bare, base)
    path = docs_dir / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    return path


QUESTION = "Which transport should the watcher use?"
OPTIONS = ["unix socket", "tcp loopback"]
KEY = "transport"


def _append_decision(project, item: dict, key: str = KEY):
    return mcp_module._edit_plan(
        project,
        "plan-a",
        [{"op": "append", "target": "decisions", "key": key, "item": item}],
        0,
    )


def test_appended_decision_keeps_its_question_and_every_option(setup):
    docs_dir, _, project = setup
    _make_plan(docs_dir, "plan-a", {"version": 0, "decisions": {}})

    r = _append_decision(project, {"question": QUESTION, "options": list(OPTIONS)})
    assert r["ok"] is True, r

    data, _ = _store_module.read_plan(project, "plan-a")
    decision = data["decisions"][KEY]
    assert decision["title"] == QUESTION
    assert decision["choices"] == OPTIONS
    assert list(decision["option_labels"]) == OPTIONS
    # The negative half: on the defect the question paragraph held the key, so
    # equality against the question alone would pass if a key can read like a
    # question. The answer must be the question, not the key that addresses it.
    assert decision["title"] != KEY


def test_the_written_question_paragraph_is_not_the_key(setup):
    docs_dir, _, project = setup
    _make_plan(docs_dir, "plan-a", {"version": 0, "decisions": {}})

    r = _append_decision(project, {"question": QUESTION, "options": list(OPTIONS)})
    assert r["ok"] is True, r

    soup = BeautifulSoup(
        (docs_dir / "plan-a.html").read_text(encoding="utf-8"), "html.parser"
    )
    para = soup.select_one(f'.r-dec[data-key="{KEY}"] .r-dec-q')
    assert para is not None, "the written decision carries no question paragraph"
    assert para.get_text(strip=True) == QUESTION
    assert para.get_text(strip=True) != KEY
    values = [b.get("data-value") for b in soup.select(".r-dec .r-opt")]
    assert values == OPTIONS


def test_appended_decision_without_options_still_succeeds(setup):
    docs_dir, _, project = setup
    _make_plan(docs_dir, "plan-a", {"version": 0, "decisions": {}})

    r = _append_decision(project, {"question": "Retire the legacy reader?"})
    assert r["ok"] is True, r

    data, _ = _store_module.read_plan(project, "plan-a")
    decision = data["decisions"][KEY]
    assert decision["title"] == "Retire the legacy reader?"
    assert decision["choices"] == []
