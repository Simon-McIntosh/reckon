"""A plan, research or evidence document saves as a PDF print of its reader.

Every document row carries a ``download`` naming its canonical route with a
``.pdf`` suffix, the same field a figure row uses to name its file, so the
reader's Save control is keyed on one field for every kind. The server answers
that route by printing the SPA reader through headless Chromium over loopback,
so a client on the far side of a tunnel receives a finished file. A ``.pdf``
path that is not a document's route is served as the file it names.

The route tests stand the renderer in with a recorder, so they assert what the
server asks for and what it sends without a browser. The last test prints a
real document with a figure through Chromium and is skipped where the browser
is not installed.
"""

from __future__ import annotations

import http.client
import json
import re
import struct
import threading
from pathlib import Path

import pytest

from reckon import serve
from reckon.reader_pdf import (
    MARGIN_SIDE_MM,
    PAGE_WIDTH_MM,
    READING_COLUMN_PX,
    FontFace,
    ReaderPdfError,
    ReaderPdfUnavailableError,
    _is_live_status,
    page_scale,
    reader_hash,
)

PROJECT = "sample"
# Enough text to fill several pages, so a reader clipped to its scrolling pane
# prints as one page and the difference is observable.
LONG_BODY = "".join(
    f"<p>Paragraph {n}: the helium signal rose while the gate stayed shut, "
    "and the pressure step fitted the single-volume balance.</p>"
    for n in range(160)
)
ROOT = Path(__file__).resolve().parents[1]


def _png_bytes(width: int, height: int) -> bytes:
    import zlib

    raw = b"".join(b"\x00" + b"\x80\x80\x80" * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _document(docs: Path, root: str, artifact_type: str, slug: str, body: str) -> Path:
    path = docs / root / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    status = (
        '<meta name="plan-status" content="active">' if artifact_type == "plan" else ""
    )
    path.write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f'<meta name="reckon-type" content="{artifact_type}">'
        f'<meta name="plan-slug" content="{slug}">'
        f'<meta name="plan-title" content="The {slug} document">'
        f"{status}<title>{slug}</title></head>"
        f'<body><main class="plan-doc">{body}</main></body></html>',
        encoding="utf-8",
    )
    return path


@pytest.fixture
def docs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    docs = tmp_path / "docs"
    _document(docs, "plans", "plan", "roadmap", "<h1>Roadmap</h1><p>Work.</p>")
    _document(
        docs,
        "research",
        "research",
        "leak",
        f'<h1>The leak</h1><figure><img src="/{PROJECT}/figures/leak/trace.png">'
        "<figcaption>Trace</figcaption></figure>" + LONG_BODY,
    )
    _document(docs, "evidence", "evidence", "gate", "<h1>Gate</h1><p>Passed.</p>")
    figures = docs / "figures" / "leak"
    figures.mkdir(parents=True)
    (figures / "trace.png").write_bytes(_png_bytes(64, 32))
    (figures / "sheet.pdf").write_bytes(b"%PDF-1.4 an authored figure file\n")

    mounts = tmp_path / "config" / "mounts.json"
    mounts.parent.mkdir(parents=True)
    mounts.write_text(json.dumps({PROJECT: str(docs)}), encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(serve, "_MOUNTS_FILE", mounts)
    monkeypatch.setattr(serve, "_STATE_ROOT", state)
    monkeypatch.setattr(serve, "_SHARED_ROOT", ROOT / "docs" / "_shared")
    return docs


@pytest.fixture
def served(docs: Path):
    server = serve.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(server, path: str, timeout: float = 10) -> tuple[int, dict, bytes]:
    connection = http.client.HTTPConnection(
        "127.0.0.1", server.server_port, timeout=timeout
    )
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


class _Recorder:
    def __init__(self, outcome=None) -> None:
        self.calls: list[dict] = []
        self.outcome = outcome

    def __call__(self, url, *, title, fonts=()):
        self.calls.append({"url": url, "title": title, "fonts": list(fonts)})
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return b"%PDF-1.7 printed reader\n", []


@pytest.fixture
def faces_offline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Answer every pinned face from a local file so no test fetches a font."""
    face = tmp_path / "face.woff2"
    face.write_bytes(b"wOF2 stand-in")
    font_names = {url.rsplit("/", 1)[1] for *_, url, _ in serve.READER_PDF_FACES}
    real = serve._client_asset

    def client_asset(name: str) -> Path:
        return face if name in font_names else real(name)

    monkeypatch.setattr(serve, "_client_asset", client_asset)
    return face


# ── The row contract ──────────────────────────────────────────────────────


def test_every_document_row_downloads_its_canonical_route_as_pdf(docs: Path) -> None:
    inventory = serve.discover_plans(docs, PROJECT, None)["inventory"]

    documents = [
        row for row in inventory if row["type"] in {"plan", "research", "evidence"}
    ]
    assert {row["type"] for row in documents} == {"plan", "research", "evidence"}
    for row in documents:
        # Field against field on the same row, so either one moving fails.
        assert row["download"] == f"{row['canonical_href']}.pdf"


def test_figure_rows_keep_downloading_their_own_file(docs: Path) -> None:
    inventory = serve.discover_plans(docs, PROJECT, None)["inventory"]

    figures = [row for row in inventory if row["type"] == "figure"]
    assert figures
    for row in figures:
        assert row["download"] == row["href"]


# ── The route ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("route", "artifact_type", "slug"),
    [
        ("/sample/plans/roadmap.pdf", "plan", "roadmap"),
        ("/sample/research/leak.pdf", "research", "leak"),
        ("/sample/evidence/gate.pdf", "evidence", "gate"),
    ],
)
def test_a_document_route_with_a_pdf_suffix_sends_its_printed_reader(
    served, faces_offline, monkeypatch: pytest.MonkeyPatch, route, artifact_type, slug
) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(serve, "render_reader_pdf", recorder)

    status, headers, body = _get(served, route)

    assert status == 200
    assert headers["Content-Type"] == "application/pdf"
    assert headers["Content-Disposition"] == f'attachment; filename="{slug}.pdf"'
    assert body == b"%PDF-1.7 printed reader\n"
    [call] = recorder.calls
    # The browser reads the reader from this same server, over loopback.
    assert call["url"] == (
        f"http://127.0.0.1:{served.server_port}/{PROJECT}/#{reader_hash(artifact_type, slug)}"
    )
    assert call["title"] == f"The {slug} document"
    assert [(f.family, f.weight, f.style) for f in call["fonts"]] == [
        (family, weight, style) for family, weight, style, *_ in serve.READER_PDF_FACES
    ]


def test_a_pdf_file_under_figures_is_served_as_the_file(
    served, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(serve, "render_reader_pdf", recorder)

    status, _, body = _get(served, "/sample/figures/leak/sheet.pdf")

    assert status == 200
    assert body == b"%PDF-1.4 an authored figure file\n"
    assert recorder.calls == []


def test_a_pdf_route_naming_no_document_is_not_found(
    served, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(serve, "render_reader_pdf", recorder)

    status, _, _ = _get(served, "/sample/research/absent.pdf")

    assert status == 404
    assert recorder.calls == []


def test_an_absent_renderer_answers_unavailable_with_the_install_command(
    served, faces_offline, monkeypatch: pytest.MonkeyPatch
) -> None:
    refusal = ReaderPdfUnavailableError(
        "PDF export needs the Chromium headless shell; install it with: "
        "uv run playwright install --only-shell chromium"
    )
    monkeypatch.setattr(serve, "render_reader_pdf", _Recorder(refusal))

    status, headers, body = _get(served, "/sample/research/leak.pdf")

    assert status == 503
    assert headers["Content-Type"] == "text/plain"
    assert b"playwright install --only-shell chromium" in body


def test_a_failed_print_answers_bad_gateway_with_its_reason(
    served, faces_offline, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        serve, "render_reader_pdf", _Recorder(ReaderPdfError("reader never settled"))
    )

    status, _, body = _get(served, "/sample/research/leak.pdf")

    assert status == 502
    assert b"reader never settled" in body


def test_a_face_that_cannot_be_fetched_prints_without_it(
    served, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(serve, "render_reader_pdf", recorder)

    def unavailable(name: str) -> Path:
        raise serve.ClientAssetError(f"offline: {name}")

    monkeypatch.setattr(serve, "_client_asset", unavailable)

    status, _, _ = _get(served, "/sample/research/leak.pdf")

    assert status == 200
    assert recorder.calls[0]["fonts"] == []


# ── The renderer's own rules ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("artifact_type", "slug", "archived", "expected"),
    [
        ("plan", "roadmap", False, "plan/roadmap"),
        ("plan", "roadmap", True, "plan/plan%3Aarchive%3Aroadmap"),
        ("research", "leak", False, "research/research%3Aleak"),
        ("evidence", "gate", True, "evidence/evidence%3Aarchive%3Agate"),
    ],
)
def test_the_reader_route_uses_the_rows_navigation_key(
    artifact_type, slug, archived, expected
) -> None:
    assert reader_hash(artifact_type, slug, archived=archived) == expected


def test_the_print_scale_lays_the_reading_column_across_the_page() -> None:
    printable_px = (PAGE_WIDTH_MM - 2 * MARGIN_SIDE_MM) * 96 / 25.4
    assert printable_px / page_scale() == pytest.approx(READING_COLUMN_PX, rel=1e-3)


@pytest.mark.parametrize(
    ("url", "live"),
    [
        ("http://127.0.0.1:1/crew", True),
        ("http://127.0.0.1:1/crew/sample", True),
        ("http://127.0.0.1:1/_changes/sample", True),
        ("http://127.0.0.1:1/crewmate", False),
        ("http://127.0.0.1:1/sample/figures/crew/trace.png", False),
        ("http://127.0.0.1:1/plan/sample/research/leak", False),
    ],
)
def test_only_live_fleet_polls_are_withheld_from_the_print(url, live) -> None:
    assert _is_live_status(url) is live


def test_a_face_rule_selects_exactly_its_weight_and_style() -> None:
    face = FontFace("Geist", 600, "normal", b"wOF2")

    assert 'font-family: "Geist"' in face.rule()
    assert "font-weight: 600;" in face.rule()
    assert "font-style: normal;" in face.rule()
    assert face.descriptor() == 'normal 600 16px "Geist"'


# ── A real print ──────────────────────────────────────────────────────────


def test_a_research_document_prints_with_its_figure(
    served, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Print the reader through Chromium and find the figure in the PDF."""
    real = serve._client_asset
    font_names = {url.rsplit("/", 1)[1] for *_, url, _ in serve.READER_PDF_FACES}

    def cached_faces_only(name: str) -> Path:
        # Faces are used when already cached and never fetched by a test.
        if name in font_names:
            cached = serve._client_cache_root() / name
            if not cached.is_file():
                raise serve.ClientAssetError(f"{name} is not cached")
            return cached
        return real(name)

    monkeypatch.setattr(serve, "_client_asset", cached_faces_only)
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        pytest.skip("playwright is not installed")

    status, headers, body = _get(served, "/sample/research/leak.pdf", timeout=180)

    if status == 503:
        pytest.skip(body.decode())
    assert status == 200, body[:400]
    assert headers["Content-Type"] == "application/pdf"
    assert body.startswith(b"%PDF-")
    pages = re.findall(rb"/Type\s*/Page\b(?!s)", body)
    images = re.findall(rb"/Subtype\s*/Image", body)
    # Unrolled rather than clipped to the reader's scrolling pane.
    assert len(pages) >= 3, f"{len(pages)} page(s): the reader was clipped"
    assert len(images) == 1, "the document's one figure is embedded"
