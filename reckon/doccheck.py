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

Severity: ERROR (exit non-zero) for things that render visibly wrong;
WARN for fragile-but-rendering; INFO for advisory.

Usage:
    python -m reckon.doccheck docs/my-plan.html
    reckon audit-doc docs/my-plan.html            # via the reckon CLI
    reckon audit-doc docs/*.html                  # multiple files
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


def run(paths: list[str], *, project: str | None = None) -> int:
    """Audit each path; print findings; return process exit code (0 = no errors)."""
    any_error = False
    for raw in paths:
        p = Path(raw).expanduser()
        findings = audit_file(p, project=project)
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
    ns = ap.parse_args(argv)
    return run(ns.paths, project=ns.project)


if __name__ == "__main__":
    sys.exit(main())
