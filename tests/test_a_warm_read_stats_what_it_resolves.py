"""A warm plan read stats the plans it resolves, not the whole directory.

Resolving a plan by slug asks who else in its own canonical directory claims the
same identity, and that index was revalidated on every resolve by enumerating
the directory and stamping every sibling. So a read of one plan paid the
metadata of every plan the directory held, whether or not the read named it: on
shared storage each of those stamps is a round trip every client on the node
shares. The listing a held index is revalidated against is now taken once per
directory change, held against the directory's own stat identity.

The instrument counts ``os.stat`` calls whose path is an entry of the store's
``plans`` directory, so the stat of the directory itself is not counted. Each
count is taken on a detail-shaped read: the named plan plus the three plans its
document declares as references, resolved exactly as a read of that plan does.
The first read in a store is the cold one, and it is the positive control that
the instrument sees the directory at all — it stamps every entry, so it reads
twenty and two hundred. The second read is the warm one, and its count is what
the bound is about.

The rewrite case holds down the other halves of the contract: a plan rewritten
in place is returned with its new content by the next read, and a store holding
two files that claim one canonical identity still refuses.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from reckon import file_memo, metadata_index, resources

PROJECT = "sample"
SMALL = 20
LARGE = 200
TARGET = "target"
REFERENCES = ("ref-alpha", "ref-beta", "ref-gamma")
_REF_RE = re.compile(r"""data-plan-ref=["'](?P<slug>[^"']+)["']""")


def _plan_doc(slug: str, *, title: str | None = None) -> str:
    refs = "".join(
        f'<a class="plan-ref" data-plan-ref="{reference}">'
        f'href="/{PROJECT}/plans/{reference}"</a>'
        for reference in (REFERENCES if slug == TARGET else ())
    )
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">\n'
        f'<meta name="docs-project" content="{PROJECT}">\n'
        '<meta name="reckon-type" content="plan">\n'
        f'<meta name="plan-slug" content="{slug}">\n'
        f'<meta name="plan-title" content="{title or slug}">\n'
        '<meta name="plan-status" content="active">\n'
        f"<title>{title or slug}</title></head>"
        f'<body><main class="plan-doc">{refs}</main></body></html>\n'
    )


def _write_plan(docs: Path, slug: str, *, title: str | None = None) -> Path:
    path = docs / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_plan_doc(slug, title=title), encoding="utf-8")
    return path


def _build_store(root: Path, count: int) -> Path:
    """Write a store of ``count`` plans whose target references three of them."""
    docs = root / "docs"
    _write_plan(docs, TARGET)
    for slug in REFERENCES:
        _write_plan(docs, slug)
    for number in range(count - 1 - len(REFERENCES)):
        _write_plan(docs, f"plan-{number:03d}")
    return docs


def _references(docs: Path) -> list[str]:
    """Return the references the target document declares, in document order."""
    text = (docs / "plans" / f"{TARGET}.html").read_text(encoding="utf-8")
    return [match.group("slug") for match in _REF_RE.finditer(text)]


def _detail_read(docs: Path) -> dict[str, dict]:
    """Resolve the named plan and the three plans its document references."""
    with metadata_index.discovery_file_scope(), resources.resource_scan_scope():
        return {
            slug: resources.read_plan_record(docs, PROJECT, slug)
            for slug in (TARGET, *_references(docs))
        }


def _stat_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int
) -> tuple[int, int, int]:
    """Return the cold count, the warm count and the resolved plans' count."""
    docs = _build_store(tmp_path / f"store-{count}", count)
    plans_dir = str(docs / "plans")
    counted = {"entries": 0}

    real_stat = os.stat

    def counting_stat(path: object, *args: object, **kwargs: object):
        observed = os.fspath(path)  # type: ignore[arg-type]
        if os.path.dirname(observed) == plans_dir:
            counted["entries"] += 1
        return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "stat", counting_stat)

    _detail_read(docs)
    cold_count = counted["entries"]
    counted["entries"] = 0
    warm = _detail_read(docs)
    return cold_count, counted["entries"], len(warm)


def test_a_warm_read_stats_the_plans_it_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_memo.clear()
    small_cold, small_warm, small_resolved = _stat_counts(tmp_path, monkeypatch, SMALL)
    large_cold, large_warm, large_resolved = _stat_counts(tmp_path, monkeypatch, LARGE)

    assert small_resolved == large_resolved == 1 + len(REFERENCES), (
        "the read did not resolve the target and the plans it references: "
        f"{small_resolved} and {large_resolved}"
    )
    # The instrument must see the directory it counts, or the equality below is
    # an absence of measurement rather than a bound: every entry is reached at
    # least once, and the store ten times larger is reached far more.
    assert small_cold >= SMALL and large_cold >= LARGE, (
        f"the cold read reached {small_cold} and {large_cold} entries, not at "
        f"least {SMALL} and {LARGE}"
    )
    assert large_cold > small_cold
    assert large_warm < large_cold, (
        f"the warm read stamped {large_warm} of the directory's {LARGE} entries"
    )
    assert small_warm == large_warm, (
        "a warm read's stats on the plans directory grew with the plans it does "
        f"not name: {small_warm} in {SMALL} plans, {large_warm} in {LARGE}"
    )


def test_a_plan_rewritten_in_place_is_read_with_its_new_content(
    tmp_path: Path,
) -> None:
    file_memo.clear()
    docs = _build_store(tmp_path / "store", SMALL)
    assert _detail_read(docs)[TARGET]["title"] == TARGET

    rewritten = _write_plan(docs, TARGET, title="rewritten target")

    second = _detail_read(docs)[TARGET]
    assert second["title"] == "rewritten target", second
    assert rewritten.read_text(encoding="utf-8") == _plan_doc(
        TARGET, title="rewritten target"
    )


def test_two_files_claiming_one_identity_are_refused(tmp_path: Path) -> None:
    file_memo.clear()
    docs = tmp_path / "docs"
    _write_plan(docs, "alpha")
    _write_plan(docs, "beta")
    beta = docs / "plans" / "beta.html"
    beta.write_text(_plan_doc("alpha"), encoding="utf-8")

    with pytest.raises(resources.ResourceCollision) as refused:
        resources.resolve_resource(docs, PROJECT, "alpha")

    message = str(refused.value)
    assert "duplicate resource" in message, message
    assert "plans/alpha.html" in message and "plans/beta.html" in message, message
