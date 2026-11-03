"""One MCP read enters the covered-tree and identity-index scopes once.

Both scopes already exist: ``metadata_index.discovery_file_scope`` shares one
covered-tree observation within a read, and ``resources.resource_scan_scope``
holds one identity index for every resolve inside it. Neither was entered by an
MCP tool, so a single ``read_plan`` revalidated the covered tree 21 times and
rebuilt the identity index once per ref. This file measures the boundary that
enters both, and the covered walk that stops descending into the stored review
records.

The tree is synthetic and mounted in a temporary home: forty plans, five
figures and two thousand directories of review JSON. Every tool is called
through its registered ``_deadline_tool`` wrapper, so the body runs on the
executor thread exactly as it does live — a scope entered in the async wrapper
lands on the event loop's thread and leaves the worker outside it, which is the first
negative control.

It also records the base revision's count for the same call. The base revision
is the same tool with no boundary scope and with the state records back in the
covered walk, which is what its registration and its walk did; measured on this
tree that is more than twenty thousand stats for one ``read_plan``.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

import reckon.mcp as mcp_module
from reckon import mcp_deadlines, mcp_edit_plan, metadata_index, resources, serve

PROJECT = "sample"
PLAN_COUNT = 40
FIGURE_COUNT = 5
REVIEW_DIR_COUNT = 2000
REF_COUNT = 20
STAT_BUDGET = 300
#: The base revision's ``read_plan`` count on this tree, recorded so the head
#: figure has something to be a fraction of. It is measured below rather than
#: asserted from a document, because a number written down is a fuse.
BASE_STAT_PATHS_FLOOR = 1_000


def _plan_html(
    slug: str, *, title: str | None = None, section_deps: object = None
) -> str:
    head = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        f'<meta name="docs-project" content="{PROJECT}">',
        '<meta name="reckon-type" content="plan">',
        f'<meta name="plan-slug" content="{slug}">',
        f'<meta name="plan-title" content="{title or slug}">',
        '<meta name="plan-summary" content="one plan">',
        '<meta name="plan-status" content="active">',
        '<meta name="plan-impl" content="0.25">',
        '<meta name="plan-version" content="1">',
        '<meta name="plan-section-declarations" content="{&quot;s1&quot;: &quot;implementable&quot;}">',
    ]
    if section_deps is not None:
        from html import escape

        head.append(
            '<meta name="plan-section-depends-on" content="'
            + escape(json.dumps(section_deps), quote=True)
            + '">'
        )
    head.append('</head><body><main class="plan-doc">')
    head.append('<h2 id="s1">§1 — the section</h2><p>authored prose</p>')
    head.append("</main></body></html>")
    return "".join(head)


def _write_plan(docs: Path, slug: str, **kwargs: object) -> Path:
    path = docs / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_plan_html(slug, **kwargs), encoding="utf-8")
    return path


class Store:
    """One mounted project and its temporary home."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.docs = root / "repo" / "docs"
        self.home = root / "home"
        self.docs.mkdir(parents=True)
        self.home.mkdir(parents=True)
        (self.docs / "plans").mkdir()
        (self.docs / "figures").mkdir()
        reviews = self.docs / "state" / PROJECT / "reviews"
        reviews.mkdir(parents=True)
        (self.docs / "state" / PROJECT / "timeline.html").write_text(
            "<!doctype html><html><body>timeline</body></html>", encoding="utf-8"
        )
        for number in range(PLAN_COUNT):
            _write_plan(self.docs, f"plan-{number:02d}")
        for number in range(FIGURE_COUNT):
            (self.docs / "figures" / f"figure-{number}.svg").write_text(
                f"<svg><!-- figure {number} --></svg>", encoding="utf-8"
            )
        for number in range(REVIEW_DIR_COUNT):
            directory = reviews / "bulk" / f"d{number:04d}"
            directory.mkdir(parents=True)
            (directory / "review.json").write_text(
                json.dumps({"number": number}), encoding="utf-8"
            )
        self.mounts = self.home / "mounts.json"
        self.mounts.write_text(json.dumps({PROJECT: str(self.docs)}), encoding="utf-8")


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Store:
    state = Store(tmp_path)
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(state.mounts))
    monkeypatch.setenv("RECKON_HOME", str(state.home))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.delenv("RECKON_MCP_READ_DEADLINE_SECONDS", raising=False)
    serve._resolve_paths(state.mounts)
    metadata_index._CACHE.clear()
    mcp_deadlines._INFLIGHT_READS.clear()
    return state


class Counter:
    """The observations one call makes, counted through patches."""

    def __init__(self) -> None:
        self.stat = 0
        self.stat_paths: set[str] = set()
        self.reuse = 0
        self.validations = 0
        self.identity_index = 0

    def reset(self) -> None:
        self.stat = self.reuse = self.validations = self.identity_index = 0
        self.stat_paths.clear()

    def as_dict(self) -> dict[str, object]:
        return {
            "stat": self.stat,
            "stat_paths": len(self.stat_paths),
            "reuse": self.reuse,
            "validations": self.validations,
            "identity_index": self.identity_index,
        }


@pytest.fixture
def counted(monkeypatch: pytest.MonkeyPatch) -> Counter:
    counter = Counter()
    real_stat = os.stat
    real_reuse = metadata_index._reuse_covered
    real_once = metadata_index._reuse_covered_once
    real_index = resources._identity_listing

    def counting_stat(*args: object, **kwargs: object):
        counter.stat += 1
        counter.stat_paths.add(os.fspath(args[0]) if args else "")
        return real_stat(*args, **kwargs)

    def counting_reuse(*args: object, **kwargs: object):
        counter.reuse += 1
        return real_reuse(*args, **kwargs)

    def counting_once(*args: object, **kwargs: object):
        if metadata_index._DISCOVERY_FILES.get() is not None:
            counter.validations += 1
        return real_once(*args, **kwargs)

    def counting_index(*args: object, **kwargs: object):
        counter.identity_index += 1
        return real_index(*args, **kwargs)

    monkeypatch.setattr(os, "stat", counting_stat)
    monkeypatch.setattr(metadata_index, "_reuse_covered", counting_reuse)
    monkeypatch.setattr(metadata_index, "_reuse_covered_once", counting_once)
    monkeypatch.setattr(resources, "_identity_listing", counting_index)
    return counter


def _tool(body, *, kind: str, scope: bool, **kwargs):
    return mcp_deadlines._deadline_tool(body, kind=kind, request_scope=scope, **kwargs)


def _call(tool, **kwargs):
    return asyncio.run(tool(**kwargs))


def _read_plan(scope: bool = True, **kwargs):
    return _tool(
        mcp_module._read_plan_tool, kind="read", scope=scope, response_budget=False
    )


def _set_title(state: Store, slug: str, title: str, version: int = 1, **kwargs):
    tool = _tool(mcp_edit_plan._edit_plan_tool, kind="write", scope=False)
    return _call(
        tool,
        project=PROJECT,
        slug=slug,
        expected_version=version,
        ops=[{"op": "set", "path": "title", "value": title}],
        **kwargs,
    )


def test_each_read_validates_the_covered_tree_once(
    store: Store, counted: Counter, monkeypatch: pytest.MonkeyPatch
):
    """Every read-only tool validates the tree at most once, and stats few paths.

    ``edit_plan`` is a write, so it enters the scopes around its reads either
    side of the write and validates at most twice. A tool that reads no document
    reaches the covered-tree memo not at all, which is the stronger statement —
    a reused validation and an absent scope entry both read as zero validations,
    so the count of calls to the memo separates them.
    """

    calls = {
        "read_plan": (
            _read_plan(),
            {"project": PROJECT, "view": "summary", "limit": PLAN_COUNT},
            ("read", 1),
        ),
        "edit_plan": (
            _tool(mcp_edit_plan._edit_plan_tool, kind="write", scope=False),
            {
                "project": PROJECT,
                "slug": "plan-00",
                "expected_version": 1,
                "ops": [{"op": "set", "path": "title", "value": "Written title"}],
            },
            ("write", 2),
        ),
        "roadmap": (
            _tool(
                mcp_module._roadmap_tool, kind="read", scope=True, response_budget=False
            ),
            {"project": PROJECT, "view": "summary"},
            ("read", 1),
        ),
        "audit": (
            _tool(
                mcp_module._audit_tool,
                kind="read",
                scope=True,
                response_budget=False,
            ),
            {"project": PROJECT},
            ("read", 1),
        ),
        "crew": (
            _tool(mcp_module._crew, kind=mcp_deadlines._crew_call_kind, scope=True),
            {"view": "live"},
            ("read", 0),
        ),
    }

    observed: dict[str, dict[str, int]] = {}
    for name, (tool, kwargs, (kind, reuse_limit)) in calls.items():
        counted.reset()
        if kind == "write":
            asyncio.run(tool(**kwargs))
        else:
            _call(tool, **kwargs)
        observed[name] = counted.as_dict()
        assert observed[name]["stat_paths"] < STAT_BUDGET, (name, observed[name])
        assert observed[name]["validations"] <= reuse_limit, (name, observed[name])

    # The crew live view reads no document, so it reaches the covered-tree memo
    # not at all: a zero validation count alone would also hold if the boundary
    # scope were simply missing, and this count cannot.
    assert observed["crew"]["reuse"] == 0, observed
    assert observed["crew"]["validations"] == 0, observed
    assert observed["read_plan"]["reuse"] == 1, observed

    # The base revision's count for the same call: no boundary scope and the
    # state records back inside the covered walk, which is what the base
    # registration and walk did on this tree.
    base_store = Store(store.root / "base")
    try:
        monkeypatch.setenv("RECKON_MOUNTS_PATH", str(base_store.mounts))
        serve._resolve_paths(base_store.mounts)
        monkeypatch.setattr(metadata_index, "_skips_infra_dir", lambda _rel: False)

        class NoStore(dict):
            def get(self, *_args, **_kwargs):
                return None

        # A fresh process per lookup: the base revision's read rebuilt what it
        # needed each time rather than reusing an in-process build, which is
        # what made one read validate the tree twenty-one times.
        monkeypatch.setattr(metadata_index, "_CACHE", NoStore())
        counted.reset()
        _call(
            _read_plan(scope=False), project=PROJECT, view="summary", limit=PLAN_COUNT
        )
        base = counted.as_dict()
    finally:
        serve._resolve_paths(store.mounts)
    observed["read_plan_base"] = base
    # The base revision walked the stored review records and the head does not:
    # the contrast is in the paths reached, which is the walk this section
    # changes. Production reached 86,098 stat calls because a real tree carries
    # 2,369 review directories and the read validated the tree 21 times; this
    # synthetic tree reaches about 2,050 paths and one in-process validation, so
    # the figure asserted here is the paths, not the production call count.
    assert base["stat_paths"] > 2 * BASE_STAT_PATHS_FLOOR, observed


def test_twenty_section_refs_build_one_identity_index(store: Store, counted: Counter):
    """One read resolving twenty refs identifies the plans directory once."""

    deps = {"s1": [f"plan-{number:02d}#s1" for number in range(REF_COUNT)]}
    _write_plan(store.docs, "holder", section_deps=deps)
    counted.reset()
    result = _call(_read_plan(), project=PROJECT, slug="holder")

    waits = sum(
        len(row.get("waits_on") or []) for row in result.get("section_blocking") or []
    )
    assert waits == REF_COUNT, result.get("section_blocking")
    assert counted.identity_index == 1, counted.as_dict()


def test_figures_stay_in_the_index_and_the_listing(store: Store):
    """The figure root is the one infra directory the covered set keeps."""

    _call(_read_plan(), project=PROJECT, view="summary")
    rows = metadata_index.index_rows(store.docs, PROJECT)
    figures = [row for row in rows if row.get("type") == "figure"]
    assert len(figures) == FIGURE_COUNT, rows

    listed = metadata_index._covered_files_for_project(store.docs, PROJECT)
    listed_figures = [
        relative for relative, _path in listed if relative.startswith("figures/")
    ]
    assert len(listed_figures) == FIGURE_COUNT, listed


def test_state_records_stay_out_of_the_document_views(store: Store):
    """A document under the state tree is not a plan, a roadmap row or an audit."""

    hidden = store.docs / "state" / PROJECT / "later" / "hidden.html"
    hidden.parent.mkdir(parents=True)
    hidden.write_text(_plan_html("hidden"), encoding="utf-8")

    listing = _call(_read_plan(), project=PROJECT, view="summary", limit=200)
    slugs = {row["slug"] for row in listing.get("resources") or []}
    assert "hidden" not in slugs, slugs

    roadmap = _call(
        _tool(mcp_module._roadmap_tool, kind="read", scope=True, response_budget=False),
        project=PROJECT,
        view="summary",
    )
    assert "hidden" not in json.dumps(roadmap)

    audit = _call(
        _tool(mcp_module._audit_tool, kind="read", scope=True, response_budget=False),
        project=PROJECT,
    )
    assert "hidden" not in json.dumps(audit)


def test_the_review_store_and_state_pages_are_still_read(store: Store):
    """The review store keeps its own readers, and the state pages stay served."""

    from reckon import _store
    from reckon.crew import plan_review as review_module

    real_home_reviews = Path.home() / ".config" / "reckon" / "crew" / "reviews"
    untouched_before = (
        sorted(real_home_reviews.glob("**/*")) if real_home_reviews.is_dir() else []
    )

    before = _set_title(store, "plan-00", "Before the review")
    assert before.get("ok"), before
    owed_before = {row["unit"] for row in before.get("review_owed") or []}
    assert "s1" in owed_before, before

    plan_path = store.docs / "plans" / "plan-00.html"
    # The record covers the content as it stands now, so the next write makes
    # the section owed again. A single request then has to report that from its
    # own post-write read; a read that served the pre-write memo would report
    # nothing owed.
    state = review_module._as_document(plan_path)
    text = plan_path.read_text(encoding="utf-8")
    digests = review_module._section_digests(text)
    prose = review_module._prose_texts(text)
    declarations = review_module._snapshot_declarations(text)
    record = {
        "project": PROJECT,
        "plan_slug": "plan-00",
        "plan_version": 2,
        "rubric": "plan_review",
        "reviewed_blob_sha": "a" * 40,
        "plan_fingerprint": review_module._fingerprint_forms(state)[0],
        "section_digests": digests,
        "prose_texts": prose,
        "declarations": declarations,
        "findings": [],
        "responses": {},
        "status": "ready",
        "review_run_id": "r-every-read-enters-the-existing-scopes",
    }
    stored = review_module.store_plan_review(record)
    assert Path(stored).is_file(), stored
    assert str(stored).startswith(str(_store._config_home())), stored

    answer = _set_title(store, "plan-00", "Written in the same request", version=2)
    assert answer.get("ok"), answer
    owed_after = {row["unit"] for row in answer.get("review_owed") or []}
    # A title write changes no section's text, so the section stays reviewed
    # and the document itself becomes owed. What matters is that the owed set
    # is non-empty: a post-write read serving the pre-write memo reports none.
    assert "_document" in owed_after, answer
    assert owed_after != owed_before, (owed_before, owed_after)

    timeline = serve.safe_join(store.docs, f"state/{PROJECT}/timeline.html")
    assert timeline is not None and timeline.is_file(), timeline

    untouched_after = (
        sorted(real_home_reviews.glob("**/*")) if real_home_reviews.is_dir() else []
    )
    assert untouched_after == untouched_before, "the real crew home was written"


def test_a_new_plan_is_seen_by_the_next_read(store: Store):
    """The walk's skip of the state tree does not freeze the plan listing."""

    first = _call(_read_plan(), project=PROJECT, view="summary", limit=200)
    assert "added-later" not in json.dumps(first)

    _write_plan(store.docs, "added-later")
    second = _call(_read_plan(), project=PROJECT, view="summary", limit=200)
    slugs = {row["slug"] for row in second.get("resources") or []}
    assert "added-later" in slugs, slugs


def test_edit_reads_after_its_write(store: Store, monkeypatch: pytest.MonkeyPatch):
    """The post-write read group sees the file the write produced."""

    seen: dict[str, object] = {}
    real_owed = mcp_edit_plan._review_owed_fields

    def spying_owed(project, slug, written_path, **kwargs):
        read = _call(_read_plan(), project=project, slug=slug, view="summary")
        seen["title"] = json.dumps(read)
        return real_owed(project, slug, written_path, **kwargs)

    monkeypatch.setattr(mcp_edit_plan, "_review_owed_fields", spying_owed)
    answer = _set_title(store, "plan-00", "Written title")

    assert answer.get("ok"), answer
    assert "Written title" in str(seen.get("title")), seen
