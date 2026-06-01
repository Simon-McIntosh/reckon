#!/usr/bin/env python3
"""Doc-audit — validate an authored plan/doc HTML against the SPA render contract.

The reckon SPA renders authored HTML *faithfully* via a raw-HTML passthrough
(``docs/ui/plan.jsx``): no markdown is rendered, the doc's ``<head><style>`` is
dropped, and images resolve against the project mount (``/<project>/...``). A
doc that relies on markdown, head-local CSS, or relative image paths renders
wrong. This module flags those problems before they ship.

Contract enforced (see AGENTS.md "agents author HTML directly"):
  - Body fields (comment / followup / question bodies, prose) are HTML —
    NOT markdown. Literal ``**bold**`` / leading ``- ``/``# `` render verbatim.
  - Images must use a project-absolute ``src="/<project>/figures/..."`` —
    relative ``src="figures/..."`` 404s under the no-trailing-slash plan URL.
  - ``<head><style>`` is dropped by the SPA — doc-local CSS never applies.
  - ``<pre>`` lines over ~120 chars wrap (informational — handled by CSS now).
  - Required ``<meta name="plan-*">`` tags must be present.
  - Stub prose ("See state §…", empty reckon sections) is flagged.
  - Internal links (<a href> and plan-* meta slug references) must resolve to
    an existing doc file or in-page anchor id.

Severity: ERROR (exit non-zero) for things that render visibly wrong;
WARN for fragile-but-rendering; INFO for advisory.

Usage:
    python -m reckon.doccheck docs/my-plan.html
    reckon audit-doc docs/my-plan.html            # via the reckon CLI
    reckon audit-doc docs/*.html                  # multiple files
    reckon audit-doc docs/*.html --check-links    # also check internal links
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup

# Required scalar meta tags for a plan doc (research/doc types relax `status`).
_REQUIRED_META = ("plan-slug", "plan-status")
# Body-bearing classes the SPA renders as HTML (markdown there renders verbatim).
_BODY_CLASSES = (
    "r-comment-body",
    "r-fu-body",
    "r-fu-outcome",
    "r-q-body",
    "r-q-resolution",
)
# Markdown tells we should never see in a rendered body / prose paragraph.
_MD_BOLD = re.compile(r"\*\*[^*\n]+\*\*")
_MD_LEADING = re.compile(r"^\s*(?:[-*+]\s+|#{1,6}\s+)", re.MULTILINE)
_STUB_PROSE = re.compile(r"\bSee (?:state|plan) §|^\s*TODO\b|^\s*TBD\b", re.IGNORECASE)
_PRE_LINE_LIMIT = 120

SEVERITIES = ("error", "warn", "info")


@dataclass
class Finding:
    severity: str  # "error" | "warn" | "info"
    code: str
    message: str

    def fmt(self) -> str:
        glyph = {"error": "✗", "warn": "!", "info": "·"}[self.severity]
        return f"  {glyph} [{self.code}] {self.message}"


def _visible_text(el) -> str:
    return el.get_text(" ", strip=True) if el else ""


def audit_html(html_text: str, *, project: str | None = None) -> list[Finding]:
    """Audit one document's HTML, returning findings (worst-first ordering)."""
    soup = BeautifulSoup(html_text or "", "html.parser")
    out: list[Finding] = []

    # Document type — research/doc are non-actionable; plan requires status.
    rt = soup.find("meta", attrs={"name": "reckon-type"})
    doc_type = ((rt.get("content") if rt else "") or "plan").strip().lower()

    # (e) Required meta tags ------------------------------------------------
    present = {
        (m.get("name") or "").lower()
        for m in soup.find_all("meta")
        if (m.get("name") or "").lower().startswith("plan-")
    }
    for req in _REQUIRED_META:
        if req == "plan-status" and doc_type in ("research", "doc"):
            continue
        if req not in present:
            out.append(
                Finding(
                    "error", "meta-missing", f'missing required <meta name="{req}">'
                )
            )

    # Project for image-path checks — meta, then fallback arg.
    dp = soup.find("meta", attrs={"name": "docs-project"})
    proj = ((dp.get("content") if dp else "") or project or "").strip()

    # (a) Image src that won't resolve --------------------------------------
    for img in soup.find_all("img"):
        src = (img.get("src") or "").strip()
        if not src:
            out.append(Finding("warn", "img-no-src", "<img> with empty src"))
            continue
        if re.match(r"^(?:https?:)?//|^data:", src):
            continue  # absolute / data URI — fine
        if src.startswith("/"):
            # Project-absolute. If we know the project, require the prefix.
            if proj and not src.startswith(f"/{proj}/"):
                out.append(
                    Finding(
                        "warn",
                        "img-wrong-project",
                        f'<img src="{src}"> not under /{proj}/ — verify it resolves',
                    )
                )
            continue
        # Relative (e.g. figures/foo.svg) — 404s under the no-slash plan URL.
        fix = f"/{proj}/{src}" if proj else f"/<project>/{src}"
        out.append(
            Finding(
                "error",
                "img-relative-src",
                f'relative <img src="{src}"> will 404 in the SPA — use src="{fix}"',
            )
        )

    # (b) Literal markdown in rendered text ---------------------------------
    body_scopes: list[tuple[str, object]] = []
    for cls in _BODY_CLASSES:
        for el in soup.select(f".{cls}"):
            body_scopes.append((cls, el))
    # Also prose paragraphs in the authored body (outside reckon-owned widgets).
    for p in soup.find_all("p"):
        classes = p.get("class") or []
        if any(c.startswith("r-") or c in ("dec-choice",) for c in classes):
            continue
        body_scopes.append(("p", p))

    for cls, el in body_scopes:
        txt = _visible_text(el)
        if _MD_BOLD.search(txt):
            out.append(
                Finding(
                    "error",
                    "md-bold",
                    f"literal markdown **bold** in <{cls}> — author <strong>…</strong>"
                    f" (renders verbatim): '{_MD_BOLD.search(txt).group()[:40]}…'",
                )
            )
        # Leading list/heading markers only meaningful on the raw inner text.
        raw = el.decode_contents() if hasattr(el, "decode_contents") else txt
        if _MD_LEADING.search(raw):
            out.append(
                Finding(
                    "warn",
                    "md-list-or-heading",
                    f"leading markdown marker (- / # ) in <{cls}> — use <ul>/<li> or <h3>",
                )
            )

    # (c) Reliance on <head><style> -----------------------------------------
    head = soup.find("head")
    if head and head.find("style"):
        out.append(
            Finding(
                "warn",
                "head-style-dropped",
                "<head><style> is dropped by the SPA — doc-local CSS will not apply;"
                " move rules into reckon styles or use inline style= sparingly",
            )
        )

    # (d) <pre> long lines (informational — CSS now wraps these) -------------
    for pre in soup.find_all("pre"):
        longest = max((len(ln) for ln in pre.get_text().splitlines()), default=0)
        if longest > _PRE_LINE_LIMIT:
            out.append(
                Finding(
                    "info",
                    "pre-long-line",
                    f"<pre> has a {longest}-char line (> {_PRE_LINE_LIMIT}); wraps via"
                    " CSS but consider shorter lines for readability",
                )
            )

    # (f) Stub prose / empty reckon sections --------------------------------
    full_text = soup.get_text(" ", strip=True)
    m = _STUB_PROSE.search(full_text)
    if m:
        out.append(
            Finding(
                "warn",
                "stub-prose",
                f"stub/placeholder prose detected: '{m.group().strip()[:40]}'",
            )
        )
    for sec in soup.select("section[data-reckon]"):
        sid = sec.get("data-reckon", "?")
        # A reckon section with a heading but no item rows is an empty stub.
        has_items = bool(sec.select(".r-dec, .r-fu, .r-q, .r-research, .r-comment"))
        if not has_items and not _visible_text(sec).strip().strip(
            "§ Decisions Followups Open questions Research Comments"
        ):
            out.append(
                Finding(
                    "info",
                    "empty-section",
                    f'<section data-reckon="{sid}"> has no items',
                )
            )

    # Worst-first: error, warn, info
    out.sort(key=lambda f: SEVERITIES.index(f.severity))
    return out


def audit_file(path: Path, *, project: str | None = None) -> list[Finding]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return [Finding("error", "io", f"cannot read {path}: {e}")]
    return audit_html(text, project=project)


# ── Dangling internal-link check (corpus-aware) ────────────────────────────
#
# Internal links that reference a plan slug or in-page anchor id that does not
# exist produce a 404 in the SPA.  This is a corpus-level check: we first scan
# all HTML files in a docs directory to build a slug→file and file→id map, then
# check each doc's links against it.
#
# Link forms recognised (per grep of the existing docs corpus):
#   /<project>/<slug>.html       → must resolve to a known slug
#   /<project>/<slug>            → same (no extension)
#   archive/<slug>.html          → relative to docs/; archive/ is a valid target
#   <slug>.html                  → relative to the same directory as the source
#   #anchor                      → must match an id attribute in the same file
#   <slug>.html#anchor           → slug must resolve; anchor must be in that file
#
# Meta slug references (plan-depends-on, plan-blocks, plan-informs) are
# comma-separated slug lists that are also checked for resolution.
#
# Skipped: http(s):// · // · mailto: · data: · /_shared/ · /_ui/ · bare
# non-slug hrefs (like index.html which is the SPA shell).

_SKIP_HREF_PREFIXES = ("http:", "https:", "//", "mailto:", "data:", "/_shared/", "/_ui/")
# Infrastructure filenames the SPA generates (not real plan slugs).
_INFRA_FILENAMES = frozenset(
    [
        "index.html",
        "sprint.html",
        "sprints.html",
        "milestones.html",
        "decisions.html",
        "inventory.html",
        "blockers.html",
        "implementation.html",
        "questions.html",
        "home.html",
        "project.html",
        "plan.html",
        "README.html",
    ]
)
# Meta fields that hold comma-separated plan-slug references.
_SLUG_META_FIELDS = ("plan-depends-on", "plan-blocks", "plan-informs")


def _collect_corpus(docs_dir: Path) -> tuple[dict[str, Path], dict[Path, set[str]]]:
    """Scan a docs directory and return:

    - slug_to_file: {slug → Path} for every HTML doc (including archive/ targets).
    - file_to_ids: {Path → set(id)} collecting element id attributes per file.

    Archive files are valid link targets even though they are excluded from the
    live inventory; they are included here so links into docs/archive/ resolve.
    """
    slug_to_file: dict[str, Path] = {}
    file_to_ids: dict[Path, set[str]] = {}

    for html_file in sorted(docs_dir.rglob("*.html")):
        if html_file.name in _INFRA_FILENAMES:
            continue
        try:
            text = html_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        soup = BeautifulSoup(text, "html.parser")

        # Determine slug: prefer <meta name="plan-slug">, fall back to stem.
        slug_meta = soup.find("meta", attrs={"name": "plan-slug"})
        slug = (
            (slug_meta.get("content") or "").strip() if slug_meta else ""
        ) or html_file.stem
        # First file wins (mirrors _resolve_plan_file behaviour in serve.py).
        if slug not in slug_to_file:
            slug_to_file[slug] = html_file
        # Also index by stem so relative <slug>.html links resolve.
        stem = html_file.stem
        if stem not in slug_to_file:
            slug_to_file[stem] = html_file

        # Collect all id= attributes in the document.
        ids: set[str] = set()
        for el in soup.find_all(id=True):
            eid = (el.get("id") or "").strip()
            if eid:
                ids.add(eid)
        file_to_ids[html_file] = ids

    return slug_to_file, file_to_ids


def _resolve_href(
    href: str,
    source_file: Path,
    docs_dir: Path,
    project: str | None,
    slug_to_file: dict[str, Path],
) -> tuple[Path | None, str | None]:
    """Parse an href and return (target_path, anchor) — target_path is None if unresolvable.

    Skips external hrefs; returns (None, None) for hrefs that should be ignored.
    Returns (False, None) to signal "link recognised but target file not found".
    """
    if not href or any(href.startswith(p) for p in _SKIP_HREF_PREFIXES):
        return None, None

    # Split off #anchor.
    if "#" in href:
        file_part, anchor = href.split("#", 1)
    else:
        file_part, anchor = href, None

    # Bare anchor (same-file link: "#id").
    if not file_part:
        return source_file, anchor

    # Strip leading / for project-absolute links: /<project>/<slug>.html
    if file_part.startswith("/") and project:
        prefix = f"/{project}/"
        if file_part.startswith(prefix):
            file_part = file_part[len(prefix):]
        elif file_part.startswith("/"):
            # /<other-project>/... — can't validate cross-project from here.
            return None, None

    # Normalise: strip .html suffix to get slug/relative-path stem.
    stem = file_part.removesuffix(".html")

    # Infra files are always valid (they are served by the SPA engine).
    base_name = Path(file_part).name
    if base_name in _INFRA_FILENAMES:
        return None, None

    # Try: slug_to_file lookup (covers both root slugs and archive/ targets).
    resolved = slug_to_file.get(stem) or slug_to_file.get(Path(stem).name)
    if resolved is not None:
        return resolved, anchor

    # Try relative resolution from source directory.
    candidate = (source_file.parent / file_part).resolve()
    if candidate.is_file():
        return candidate, anchor

    # Couldn't resolve.
    return False, anchor  # type: ignore[return-value]


def audit_links(
    paths: list[Path],
    docs_dir: Path,
    *,
    project: str | None = None,
) -> dict[Path, list[Finding]]:
    """Corpus-aware dangling internal-link check.

    Scans all HTML files in ``docs_dir`` to build a slug/id corpus, then checks
    each path in ``paths`` for links that reference slugs or anchors that do not
    exist.

    Returns {path → [Finding, …]} — only paths with findings are included.
    """
    slug_to_file, file_to_ids = _collect_corpus(docs_dir)

    results: dict[Path, list[Finding]] = {}
    for path in paths:
        findings: list[Finding] = []

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            findings.append(Finding("error", "io", f"cannot read {path}: {e}"))
            results[path] = findings
            continue

        soup = BeautifulSoup(text, "html.parser")

        # Infer project from meta if not supplied.
        dp = soup.find("meta", attrs={"name": "docs-project"})
        proj = ((dp.get("content") if dp else "") or project or "").strip() or None

        # (g) Check <a href> internal links.
        for a in soup.find_all("a", href=True):
            href = (a.get("href") or "").strip()
            target, anchor = _resolve_href(href, path, docs_dir, proj, slug_to_file)
            if target is None:
                continue  # external or infra — skip
            if target is False:
                findings.append(
                    Finding(
                        "warn",
                        "dangling-link",
                        f'<a href="{href}"> — target file not found',
                    )
                )
                continue
            # Check anchor resolution in target file.
            if anchor:
                known_ids = file_to_ids.get(target, set())
                if anchor not in known_ids:
                    findings.append(
                        Finding(
                            "warn",
                            "dangling-anchor",
                            f'<a href="{href}"> — anchor #{anchor} not found in target',
                        )
                    )

        # (g) Check plan-depends-on / plan-blocks / plan-informs slug references.
        for meta_name in _SLUG_META_FIELDS:
            m = soup.find("meta", attrs={"name": meta_name})
            if not m:
                continue
            raw = (m.get("content") or "").strip()
            if not raw:
                continue
            for slug_ref in [s.strip() for s in raw.split(",") if s.strip()]:
                if slug_ref not in slug_to_file:
                    findings.append(
                        Finding(
                            "warn",
                            "dangling-slug-ref",
                            f'<meta name="{meta_name}"> references unknown slug "{slug_ref}"',
                        )
                    )

        if findings:
            results[path] = findings

    return results


def run(paths: list[str], *, project: str | None = None, check_links: bool = False) -> int:
    """Audit each path; print findings; return process exit code (0 = no errors)."""
    path_objs = [Path(raw).expanduser() for raw in paths]

    # Build corpus for link check if requested.
    link_findings: dict[Path, list[Finding]] = {}
    if check_links:
        # Infer docs_dir from the paths: use their common ancestor if they share one,
        # otherwise fall back to each file's parent.
        parents = {p.parent for p in path_objs}
        docs_dir = parents.pop() if len(parents) == 1 else Path(".")
        link_findings = audit_links(path_objs, docs_dir, project=project)

    any_error = False
    for p in path_objs:
        findings = audit_file(p, project=project)
        # Merge in link findings for this path.
        findings = findings + link_findings.get(p, [])
        # Re-sort worst-first.
        findings.sort(key=lambda f: SEVERITIES.index(f.severity))

        errors = [f for f in findings if f.severity == "error"]
        warns = [f for f in findings if f.severity == "warn"]
        infos = [f for f in findings if f.severity == "info"]
        if not findings:
            print(f"{p}: OK")
            continue
        status = "FAIL" if errors else "warn"
        print(
            f"{p}: {status} ({len(errors)} error, {len(warns)} warn, {len(infos)} info)"
        )
        for f in findings:
            print(f.fmt())
        if errors:
            any_error = True
    return 1 if any_error else 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="reckon-audit-doc",
        description="Validate authored plan/doc HTML against the SPA render contract.",
        epilog="example:  python -m reckon.doccheck docs/my-plan.html",
    )
    ap.add_argument("paths", nargs="+", help="HTML file(s) to audit")
    ap.add_argument(
        "--project",
        default=None,
        help="project key for image-path checks (default: <meta name=docs-project>)",
    )
    ap.add_argument(
        "--check-links",
        action="store_true",
        default=False,
        help="also check internal links for dangling targets (corpus-aware; requires"
        " all docs to be in the same directory)",
    )
    ns = ap.parse_args(argv)
    return run(ns.paths, project=ns.project, check_links=ns.check_links)


if __name__ == "__main__":
    sys.exit(main())
