"""Tests for reckon.doccheck — single-file checks and corpus-level link audit."""

from __future__ import annotations

from pathlib import Path

from reckon.doccheck import audit_html, audit_links


# ── audit_html (single-file checks) ──────────────────────────────────────────


def _bare(body: str = "") -> str:
    return (
        '<!doctype html><html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="plan-slug" content="test">'
        '<meta name="plan-status" content="active">'
        '<title>test</title></head>'
        f'<body><main class="plan-doc">{body}</main></body></html>'
    )


def test_audit_html_clean():
    findings = audit_html(_bare())
    assert findings == []


def test_audit_html_missing_meta():
    html = (
        '<!doctype html><html><head><meta charset="utf-8">'
        "<title>x</title></head><body></body></html>"
    )
    codes = {f.code for f in audit_html(html)}
    assert "meta-missing" in codes


def test_audit_html_md_bold_in_body():
    findings = audit_html(_bare('<div class="r-fu-body">**bold** text</div>'))
    codes = [f.code for f in findings]
    assert "md-bold" in codes


def test_audit_html_relative_img_src():
    findings = audit_html(_bare('<img src="figures/foo.png">'), project="proj")
    codes = [f.code for f in findings]
    assert "img-relative-src" in codes


# ── audit_links (corpus-aware dangling-link check) ────────────────────────────


def _write_plan(docs_dir: Path, slug: str, body: str = "", *, ids: str = "") -> Path:
    """Write a minimal plan HTML with the given slug and body into docs_dir."""
    content = (
        '<!doctype html><html lang="en"><head>'
        '<meta charset="utf-8">'
        f'<meta name="plan-slug" content="{slug}">'
        f'<meta name="plan-status" content="active">'
        '<meta name="docs-project" content="proj">'
        f'<title>{slug}</title></head>'
        f'<body><main class="plan-doc">{ids}{body}</main></body></html>'
    )
    path = docs_dir / f"{slug}.html"
    path.write_text(content, encoding="utf-8")
    return path


def test_no_dangling_links(tmp_path):
    """A plan with an href that resolves to another plan → no findings."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    _write_plan(docs_dir, "alpha")
    beta = _write_plan(docs_dir, "beta", body='<a href="/proj/alpha.html">Alpha</a>')
    result = audit_links([beta], docs_dir, project="proj")
    assert result == {}


def test_dangling_link_slug_not_found(tmp_path):
    """A plan with an href to a non-existent slug → dangling-link warning."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    bad = _write_plan(docs_dir, "bad", body='<a href="/proj/ghost.html">Ghost</a>')
    result = audit_links([bad], docs_dir, project="proj")
    assert bad in result
    codes = [f.code for f in result[bad]]
    assert "dangling-link" in codes


def test_dangling_anchor_not_found(tmp_path):
    """An href that resolves to a real file but with an unknown anchor → dangling-anchor."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    _write_plan(docs_dir, "target", ids='<section id="real-section"></section>')
    src = _write_plan(
        docs_dir, "source",
        body='<a href="/proj/target.html#no-such-section">x</a>',
    )
    result = audit_links([src], docs_dir, project="proj")
    assert src in result
    codes = [f.code for f in result[src]]
    assert "dangling-anchor" in codes


def test_valid_anchor_resolves(tmp_path):
    """An href with a correct anchor → no findings."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    _write_plan(docs_dir, "target", ids='<section id="good-section"></section>')
    src = _write_plan(
        docs_dir, "source",
        body='<a href="/proj/target.html#good-section">x</a>',
    )
    result = audit_links([src], docs_dir, project="proj")
    assert result == {}


def test_same_page_anchor_good(tmp_path):
    """A bare #anchor link that resolves within the same file → no findings."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    plan = _write_plan(
        docs_dir, "self",
        body='<section id="sec1"></section><a href="#sec1">jump</a>',
    )
    result = audit_links([plan], docs_dir, project="proj")
    assert result == {}


def test_same_page_anchor_bad(tmp_path):
    """A bare #anchor that doesn't exist in the same file → dangling-anchor warning."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    plan = _write_plan(docs_dir, "self", body='<a href="#ghost">jump</a>')
    result = audit_links([plan], docs_dir, project="proj")
    assert plan in result
    codes = [f.code for f in result[plan]]
    assert "dangling-anchor" in codes


def test_archive_target_resolves(tmp_path):
    """A relative link into archive/ → resolves if the file exists."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    archive_dir = docs_dir / "archive"
    archive_dir.mkdir()
    archive_file = archive_dir / "old-plan-shipped.html"
    archive_file.write_text(
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="plan-slug" content="old-plan-shipped">'
        '<title>old</title></head><body></body></html>',
        encoding="utf-8",
    )
    src = _write_plan(
        docs_dir, "src",
        body='<a href="archive/old-plan-shipped.html">history</a>',
    )
    result = audit_links([src], docs_dir, project="proj")
    assert result == {}


def test_external_links_skipped(tmp_path):
    """http:// links are never flagged."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    plan = _write_plan(
        docs_dir, "plan",
        body='<a href="https://example.com/no-such-page">ext</a>',
    )
    result = audit_links([plan], docs_dir, project="proj")
    assert result == {}


def test_dangling_slug_in_meta_depends_on(tmp_path):
    """plan-depends-on referencing a non-existent slug → dangling-slug-ref warning."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    content = (
        '<!doctype html><html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="plan-slug" content="myplan">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-depends-on" content="ghost-plan">'
        '<meta name="docs-project" content="proj">'
        '<title>myplan</title></head>'
        '<body><main class="plan-doc"></main></body></html>'
    )
    plan = docs_dir / "myplan.html"
    plan.write_text(content, encoding="utf-8")
    result = audit_links([plan], docs_dir, project="proj")
    assert plan in result
    codes = [f.code for f in result[plan]]
    assert "dangling-slug-ref" in codes


def test_valid_slug_in_meta_depends_on(tmp_path):
    """plan-depends-on referencing a known slug → no findings."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    _write_plan(docs_dir, "dep-plan")
    content = (
        '<!doctype html><html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="plan-slug" content="myplan">'
        '<meta name="plan-status" content="active">'
        '<meta name="plan-depends-on" content="dep-plan">'
        '<meta name="docs-project" content="proj">'
        '<title>myplan</title></head>'
        '<body><main class="plan-doc"></main></body></html>'
    )
    plan = docs_dir / "myplan.html"
    plan.write_text(content, encoding="utf-8")
    result = audit_links([plan], docs_dir, project="proj")
    assert result == {}
