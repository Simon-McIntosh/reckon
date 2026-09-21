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


def test_a_null_option_value_is_refused_not_stored_as_its_string(setup):
    docs_dir, _, project = setup
    _make_plan(docs_dir, "plan-a", {"version": 0, "decisions": {}})

    r = _append_decision(
        project,
        {"question": QUESTION, "options": [{"value": None, "label": "none"}]},
    )
    assert r["ok"] is False, r
    # The refusal names the offending option rather than the collection.
    assert "{'value': None, 'label': 'none'}" in json.dumps(r), r

    # Nothing landed, so the read-back cannot present ``None`` as selectable.
    data, _ = _store_module.read_plan(project, "plan-a")
    assert KEY not in data.get("decisions", {})


def test_an_empty_option_value_is_still_refused(setup):
    docs_dir, _, project = setup
    _make_plan(docs_dir, "plan-a", {"version": 0, "decisions": {}})

    r = _append_decision(project, {"question": QUESTION, "options": [{"value": ""}]})
    assert r["ok"] is False, r

    data, _ = _store_module.read_plan(project, "plan-a")
    assert KEY not in data.get("decisions", {})


def test_two_valid_option_objects_read_back_as_both_values(setup):
    docs_dir, _, project = setup
    _make_plan(docs_dir, "plan-a", {"version": 0, "decisions": {}})

    r = _append_decision(
        project,
        {
            "question": QUESTION,
            "options": [
                {"value": "unix socket", "label": "Unix socket"},
                {"value": "tcp loopback", "label": "TCP loopback"},
            ],
        },
    )
    assert r["ok"] is True, r

    data, _ = _store_module.read_plan(project, "plan-a")
    decision = data["decisions"][KEY]
    assert decision["choices"] == OPTIONS
    assert list(decision["option_labels"]) == OPTIONS


def test_appended_decision_without_options_still_succeeds(setup):
    docs_dir, _, project = setup
    _make_plan(docs_dir, "plan-a", {"version": 0, "decisions": {}})

    r = _append_decision(project, {"question": "Retire the legacy reader?"})
    assert r["ok"] is True, r

    data, _ = _store_module.read_plan(project, "plan-a")
    decision = data["decisions"][KEY]
    assert decision["title"] == "Retire the legacy reader?"
    assert decision["choices"] == []


def test_a_mapping_option_with_a_null_label_falls_back_to_its_value(setup):
    """A value-to-label mapping whose label is null keeps the value as label.

    The mapping spelling puts the option's value on the key side and its label
    on the value side, so a null there states the label is absent. Reading the
    null as the label and stringifying it would store the option labelled
    ``None`` while its value stayed usable -- a decision the lead cannot read.
    """
    docs_dir, _, project = setup
    _make_plan(docs_dir, "plan-a", {"version": 0, "decisions": {}})

    r = _append_decision(
        project, {"question": QUESTION, "options": {"unix socket": None}}
    )
    assert r["ok"] is True, r

    data, _ = _store_module.read_plan(project, "plan-a")
    decision = data["decisions"][KEY]
    assert decision["choices"] == ["unix socket"]
    assert decision["option_labels"] == {"unix socket": "unix socket"}
    # The negative half: the literal ``None`` is what a read that stringified
    # the null label would have stored, so an equality against the value alone
    # is not enough.
    assert "None" not in decision["option_labels"].values()

    soup = BeautifulSoup(
        (docs_dir / "plan-a.html").read_text(encoding="utf-8"), "html.parser"
    )
    button = soup.select_one(f'.r-dec[data-key="{KEY}"] .r-opt')
    assert button is not None, "the written decision renders no option"
    assert button.get_text(strip=True) == "unix socket"


def test_a_mapping_option_with_a_null_value_side_is_refused(setup):
    """A mapping whose value side is null has no option value and is refused.

    ``{None: "unix socket"}`` stringifies to the option spelled ``None`` just
    as the object spelling did, so the same guard must refuse it -- and it must
    refuse the append as a whole rather than drop the malformed entry, because
    a dropped option is the loss this normalisation exists to prevent.
    """
    docs_dir, _, project = setup
    _make_plan(docs_dir, "plan-a", {"version": 0, "decisions": {}})

    r = _append_decision(
        project, {"question": QUESTION, "options": {None: "unix socket"}}
    )
    assert r["ok"] is False, r
    # The refusal names the null value rather than the collection.
    assert "null 'value'" in json.dumps(r), r

    data, _ = _store_module.read_plan(project, "plan-a")
    assert KEY not in data.get("decisions", {})


def test_an_option_object_carrying_no_value_key_is_refused_as_missing(setup):
    """An option object with no ``value`` key is refused, and named as missing.

    Falling back to the label made a label-only object a valid option whose
    value was the label -- accepted, so nothing reported the substitution --
    and an object carrying neither key was refused as a null even though no
    value key was present. The refusal must say the key is missing.
    """
    docs_dir, _, project = setup
    _make_plan(docs_dir, "plan-a", {"version": 0, "decisions": {}})

    r = _append_decision(
        project, {"question": QUESTION, "options": [{"label": "unix socket"}]}
    )
    assert r["ok"] is False, r
    assert "carries no 'value' key" in json.dumps(r), r
    # The refusal must not claim a null where no value key was present at all.
    assert "null" not in json.dumps(r), r

    # An object carrying neither key is the same defect, not a null.
    r2 = _append_decision(project, {"question": QUESTION, "options": [{}]}, key="other")
    assert r2["ok"] is False, r2
    assert "carries no 'value' key" in json.dumps(r2), r2
    assert "null" not in json.dumps(r2), r2

    data, _ = _store_module.read_plan(project, "plan-a")
    assert data.get("decisions", {}) == {}
