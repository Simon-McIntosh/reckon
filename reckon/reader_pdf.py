"""Print a document's reader view to PDF through headless Chromium.

The PDF is a print of the SPA reader itself, not of a second composition of
the document, so a reader saves what they were reading: the research or
evidence banner, the authored body, the decisions and gates rendered from
state, and every figure. The reader's print stylesheet removes the app chrome
and unrolls the scrolling panes; this module only drives the browser.

The browser is Playwright's Chromium headless shell. It is a separate install
from the Python package, so an absent browser is reported as
``ReaderPdfUnavailableError`` naming the command that installs it, rather than as a
render failure.
"""

from __future__ import annotations

import base64
import html
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

# The reader's text column on screen: its 820px measure less 26px padding on
# each side. The PDF scale maps the printable page width onto this column, so
# a line wraps where it wraps on screen.
READING_COLUMN_PX = 768
PAGE_WIDTH_MM = 210.0  # A4
MARGIN_SIDE_MM = 14.0
MARGIN_TOP_MM = 14.0
MARGIN_BOTTOM_MM = 16.0
CSS_PX_PER_MM = 96 / 25.4

# Each render holds a browser of a few hundred megabytes, and the host is
# shared, so concurrent exports queue rather than multiply.
_RENDER_SLOTS = threading.BoundedSemaphore(2)

INSTALL_HINT = "uv run playwright install --only-shell chromium"


class ReaderPdfUnavailableError(RuntimeError):
    """The PDF renderer is not installed on the serving host."""


class ReaderPdfError(RuntimeError):
    """The reader could not be printed."""


def reader_hash(artifact_type: str, slug: str, *, archived: bool = False) -> str:
    """Return the SPA route fragment that opens one document in the reader.

    The key is the inventory row's ``nav_key`` as the state loader derives it:
    a live plan is keyed by its slug, and every other document by its type,
    an ``archive:`` marker when archived, and its slug.
    """
    if artifact_type == "plan" and not archived:
        key = slug
    else:
        key = f"{artifact_type}:{'archive:' if archived else ''}{slug}"
    return f"{artifact_type}/{quote(key, safe='')}"


def page_scale() -> float:
    """Return the print scale that lays the reading column across the page."""
    printable_px = (PAGE_WIDTH_MM - 2 * MARGIN_SIDE_MM) * CSS_PX_PER_MM
    return round(printable_px / READING_COLUMN_PX, 3)


@dataclass(frozen=True)
class FontFace:
    """One WOFF2 face of a family the reader's stylesheets name."""

    family: str
    weight: int
    style: str
    payload: bytes

    def rule(self) -> str:
        encoded = base64.b64encode(self.payload).decode("ascii")
        return (
            "@font-face {"
            f' font-family: "{self.family}";'
            f' src: url(data:font/woff2;base64,{encoded}) format("woff2");'
            f" font-weight: {self.weight}; font-style: {self.style};"
            " font-display: block; }"
        )

    def descriptor(self) -> str:
        """The CSS font shorthand that selects exactly this face."""
        return f'{self.style} {self.weight} 16px "{self.family}"'


def _footer_template(title: str) -> str:
    # Chromium renders header and footer templates outside the page's own
    # styles, at a default size of zero, so every rule here is explicit.
    return (
        '<div style="width:100%; padding:0 14mm; box-sizing:border-box;'
        " display:flex; justify-content:space-between; gap:12px;"
        " font:7.5px 'Geist', 'DejaVu Sans', sans-serif; color:#8a8a84;\">"
        '<span style="overflow:hidden; white-space:nowrap; text-overflow:ellipsis;">'
        f"{html.escape(title)}</span>"
        '<span><span class="pageNumber"></span> / <span class="totalPages"></span></span>'
        "</div>"
    )


_LOAD_FACES = """
descriptors => Promise.all(descriptors.map(descriptor => document.fonts.load(descriptor)))
"""


def _is_live_status(url: str) -> bool:
    """Report whether a request polls live fleet state the print never shows.

    The reader's in-flight band and the top bar's live indicator poll these,
    and both are hidden in print; an unanswered poll still open when the
    browser closes would otherwise break its pipe in the server's log.
    """
    path = urlsplit(url).path
    return path == "/crew" or path.startswith(("/crew/", "/_changes/"))


# Resolves once the reader has settled and every image in it has loaded or
# failed. Images are switched to eager first: a lazy image below the fold is
# never fetched by a page that is printed without being scrolled.
_SETTLE_IMAGES = """
async () => {
  const images = [...document.querySelectorAll("article.r-reading img")];
  images.forEach(image => { image.loading = "eager"; });
  await Promise.all(images.map(image => image.complete ? null : new Promise(done => {
    image.addEventListener("load", done, { once: true });
    image.addEventListener("error", done, { once: true });
  })));
  await document.fonts.ready;
  return images.filter(image => !image.naturalWidth).map(image => image.currentSrc || image.src);
}
"""


def render_reader_pdf(
    url: str,
    *,
    title: str,
    fonts: Sequence[FontFace] = (),
    timeout_s: float = 90.0,
) -> tuple[bytes, list[str]]:
    """Print the reader at ``url`` and return the PDF with any unloaded images.

    ``fonts`` supplies faces of the families the reader's stylesheets name, so
    the page sets in the typeface it asks for rather than whatever the serving
    host has installed.
    """
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ReaderPdfUnavailableError(
            f"PDF export needs the playwright package: {exc}"
        ) from exc

    timeout_ms = timeout_s * 1000
    with _RENDER_SLOTS, sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except PlaywrightError as exc:
            if "Executable doesn't exist" in str(exc):
                raise ReaderPdfUnavailableError(
                    f"PDF export needs the Chromium headless shell; install it with: {INSTALL_HINT}"
                ) from exc
            raise ReaderPdfError(f"could not launch Chromium: {exc}") from exc
        try:
            page = browser.new_page(
                viewport={"width": 1280, "height": 900}, color_scheme="light"
            )
            page.set_default_timeout(timeout_ms)
            page.route(_is_live_status, lambda route: route.abort())
            page.goto(url, wait_until="load")
            if fonts:
                page.add_style_tag(content="\n".join(face.rule() for face in fonts))
                # A face is fetched when layout first asks for it, and a print
                # does not wait for a fetch it starts itself, so load each one
                # before printing rather than let the print fall back.
                page.evaluate(_LOAD_FACES, [face.descriptor() for face in fonts])
            page.wait_for_selector(
                'article.r-reading[data-reader-ready="true"]', state="attached"
            )
            missing = page.evaluate(_SETTLE_IMAGES)
            pdf = page.pdf(
                format="A4",
                print_background=True,
                scale=page_scale(),
                margin={
                    "top": f"{MARGIN_TOP_MM}mm",
                    "bottom": f"{MARGIN_BOTTOM_MM}mm",
                    "left": f"{MARGIN_SIDE_MM}mm",
                    "right": f"{MARGIN_SIDE_MM}mm",
                },
                display_header_footer=True,
                header_template="<span></span>",
                footer_template=_footer_template(title),
            )
        except PlaywrightError as exc:
            raise ReaderPdfError(f"could not print {url}: {exc}") from exc
        finally:
            browser.close()
    return pdf, list(missing)
