"""One memo owns a plan file's parse, and content is shared when a caller asks.

The metadata index used to keep a second per-file parse cache beside the one in
``file_memo``, keyed on the file's content digest. Two caches meant clearing
one left the other serving, and a reader that wanted cross-tree reuse had to
know which cache to reach for. The index now keeps no parse cache of its own,
and the opt-in identity lives in ``file_memo.memoized``, so these cases hold
both halves: the index holds none of the old names, and a caller that opts into
the content identity parses byte-identical content once across paths.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import reckon.resources as resources_module
from reckon import _plan_html, file_memo, metadata_index

#: A plan document whose identity is declared in the head, which is the parse
#: identification reads. It carries one section so it stays a record carrier
#: for the readers that derive ``impl`` from the body.
_PLAN_DOC = """<!doctype html><html><head>
<meta name="docs-project" content="proj">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="{slug}">
<meta name="plan-title" content="{title}">
<title>{title}</title>
</head><body>
<h2 id="s1">Section one</h2>
<section data-reckon="section" data-id="s1" data-status="implementable" data-effort-hours="2.0" data-attempts="0"></section>
</body></html>
"""

#: The names the index's private parse cache carried. Each one is a cache of
#: its own, and a caller reaching for it bypassed the memo every other read
#: path uses.
_INDEX_CACHE_NAMES = (
    "_META_BY_DIGEST",
    "_META_HEAD_BY_DIGEST",
    "_digest_memo",
    "parse_meta_shared",
    "_parse_meta_head_shared",
)


@pytest.fixture(autouse=True)
def _empty_memos():
    """Both memos are process-wide; each case starts from an empty pair."""
    file_memo.clear()
    metadata_index.clear()
    yield
    file_memo.clear()
    metadata_index.clear()


def _write_plan(docs: Path, slug: str, title: str) -> Path:
    path = docs / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_PLAN_DOC.format(slug=slug, title=title), encoding="utf-8")
    return path


def _counting_head_parse(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Count the head parses identification performs, without changing them."""
    calls = {"count": 0}
    real = _plan_html._parse_meta_head_uncached

    def counting(path: Path, slug: str | None) -> dict:
        calls["count"] += 1
        return real(path, slug)

    monkeypatch.setattr(_plan_html, "_parse_meta_head_uncached", counting)
    return calls


def test_identical_content_at_two_paths_parses_once(tmp_path, monkeypatch):
    """The content identity shares one parse across two copies of one document."""
    tree_a = tmp_path / "a" / "docs"
    tree_b = tmp_path / "b" / "docs"
    path_a = _write_plan(tree_a, "alpha", "Alpha")
    path_b = _write_plan(tree_b, "alpha", "Alpha")  # byte-identical to path_a
    assert path_a != path_b
    calls = _counting_head_parse(monkeypatch)

    first = resources_module._identify_meta(path_a)
    second = resources_module._identify_meta(path_b)

    assert first == second
    assert first["slug"] == "alpha"
    # Two paths, one content, so one parse.
    assert calls["count"] == 1


def test_distinct_content_at_a_second_path_is_parsed(tmp_path, monkeypatch):
    """A second path holding different bytes is parsed in its own right."""
    tree_a = tmp_path / "a" / "docs"
    tree_b = tmp_path / "b" / "docs"
    path_a = _write_plan(tree_a, "alpha", "Alpha")
    path_b = _write_plan(tree_b, "alpha", "Alpha edited")
    calls = _counting_head_parse(monkeypatch)

    resources_module._identify_meta(path_a)
    resources_module._identify_meta(path_b)

    assert calls["count"] == 2


def test_changed_bytes_at_the_same_path_are_parsed_again(tmp_path, monkeypatch):
    """A rewritten file has a new digest, so the stale parse is not served."""
    docs = tmp_path / "docs"
    path = _write_plan(docs, "alpha", "Alpha")
    calls = _counting_head_parse(monkeypatch)

    assert resources_module._identify_meta(path)["title"] == "Alpha"
    path.write_text(
        _PLAN_DOC.format(slug="alpha", title="Alpha edited"), encoding="utf-8"
    )
    assert resources_module._identify_meta(path)["title"] == "Alpha edited"
    assert calls["count"] == 2


def test_a_write_between_the_read_and_the_store_is_not_served(tmp_path, monkeypatch):
    """The text is cached only under the signature its bytes were read at.

    The digest reads a document's bytes and then hands the decode to a second
    memo. A write landing between those two moments would let the old bytes be
    stored under the new file's stat identity, so every later read would be
    answered with text the file no longer holds. The write here happens on the
    first signature check after the bytes are read — the moment the first
    version cached the stale text.
    """
    docs = tmp_path / "docs"
    path = _write_plan(docs, "alpha", "Alpha")
    edited = _PLAN_DOC.format(slug="alpha", title="Alpha edited")

    real_signature = file_memo.file_signature
    real_read_bytes = Path.read_bytes
    state = {"read": False, "written": False}

    def read_bytes(inner_self):
        data = real_read_bytes(inner_self)
        if Path(inner_self) == path:
            state["read"] = True
        return data

    def signature_then_write(candidate):
        if Path(candidate) == path and state["read"] and not state["written"]:
            path.write_text(edited, encoding="utf-8")
            state["written"] = True
        return real_signature(candidate)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(file_memo, "file_signature", signature_then_write)

    assert resources_module._identify_meta(path)["title"] == "Alpha edited"
    # The write did land, so this case is not passing on an untouched file.
    assert state["written"]


def test_the_index_keeps_no_parse_cache_of_its_own():
    """No second parse cache survives under the index, in name or in source."""
    source = Path(metadata_index.__file__).read_text(encoding="utf-8")
    for name in _INDEX_CACHE_NAMES:
        assert not hasattr(metadata_index, name), name
        assert name not in source, name
    # The byte digest has one owner too, so a second copy cannot drift.
    assert not hasattr(metadata_index, "_content_digest")
    assert hasattr(file_memo, "content_digest")
