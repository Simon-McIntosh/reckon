"""Revisioned URLs and cached responses for the served browser shell."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from reckon.file_memo import file_revision

SHELL_GZIP_MIN_BYTES = 1024
IMMUTABLE_CACHE = "max-age=31536000, immutable"
_LINK = re.compile(
    r'<(?P<tag>script|link)\b[^>]*?\b(?:src|href)="(?P<url>/_'
    r'(?:shared|ui|runtime)/[^"?]+)"'
)


def _revision(
    url: str, shared_root: Path, ui_root: Path, runtimes: Mapping[str, str]
) -> str:
    route, name = url.lstrip("/").split("/", 1)
    if route == "_runtime":
        return runtimes[name]
    source = (shared_root if route == "_shared" else ui_root) / name
    if route == "_ui" and source.suffix == ".js" and not source.is_file():
        source = source.with_suffix(".jsx")
    return file_revision(source.stat())


def revisioned_html(
    html: str, shared_root: Path, ui_root: Path, runtimes: Mapping[str, str]
) -> str:
    """Address each shell script and stylesheet by its serving revision."""

    def add_revision(match: re.Match[str]) -> str:
        tag = match.group("tag")
        if tag == "link" and 'rel="stylesheet"' not in match.group(0):
            return match.group(0)
        url = match.group("url")
        try:
            token = quote(_revision(url, shared_root, ui_root, runtimes), safe="")
        except (OSError, KeyError):
            return match.group(0)
        return match.group(0).replace(f'"{url}"', f'"{url}?rev={token}"')

    return _LINK.sub(add_revision, html)


@dataclass(frozen=True)
class ScriptResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]


def script_response(
    source: Path,
    revision: str,
    requested_revision: str | None,
    if_none_match: str,
    accept_encoding: str,
    load_body: Callable[[], bytes],
    compressor: Callable[[Path, str, bytes], bytes],
) -> ScriptResponse:
    """Answer current and stale script URLs with validators and gzip variants."""
    etag = f'"{revision}"'
    headers = {
        "ETag": etag,
        "Cache-Control": (
            IMMUTABLE_CACHE if requested_revision == revision else "no-cache"
        ),
        "Vary": "Accept-Encoding",
    }
    if etag in if_none_match:
        return ScriptResponse(304, b"", headers)
    body = load_body()
    if len(body) >= SHELL_GZIP_MIN_BYTES and "gzip" in accept_encoding.lower():
        body = compressor(source, revision, body)
        headers["Content-Encoding"] = "gzip"
    headers["Content-Type"] = "application/javascript"
    headers["Content-Length"] = str(len(body))
    return ScriptResponse(200, body, headers)
