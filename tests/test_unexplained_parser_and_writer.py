"""Pin the two direct copies the code-depth census found carrying no reason.

The calendar-date coercion in the quota-weight ledger and the JSON envelope
write behind the served POST are the two sites the post-migration census
counted with no stated reason. The first is pinned by a behaviour table whose
expectations were recorded against the base revision, so the retained direct
read cannot drift silently. The second is pinned by interrupting the
serialisation halfway and asserting the destination is still a whole envelope —
either the previous one or the new one, never a partial blend the reader could
observe.
"""

from __future__ import annotations

import contextlib
import io
import json
from datetime import date

import pytest

from reckon import serve
from reckon.crew.quota_weight import _as_date

# (input, expected) recorded against the base revision. The direct
# ``date.fromisoformat`` read accepts exactly a calendar date, so a datetime
# string, a zone-carrying datetime string, a bare epoch integer and a malformed
# value all stay refused rather than being read as instants.
_AS_DATE_TABLE = (
    (date(2026, 10, 2), date(2026, 10, 2)),
    ("2026-10-02", date(2026, 10, 2)),
    ("2026-10-02T12:00:00", None),
    ("2026-10-02T00:00:00Z", None),
    ("not-a-date", None),
    (20261002, None),
    (None, None),
)


@pytest.mark.parametrize(("value", "expected"), _AS_DATE_TABLE)
def test_as_date_calendar_read_is_unchanged(
    value: object, expected: date | None
) -> None:
    assert _as_date(value) == expected


class _RecordingHandler(serve.Handler):
    """A POST handler driven without a socket, recording what it would send."""

    def __init__(self, path: str, body: bytes, headers: dict[str, str]) -> None:
        self.path = path
        self.headers = headers
        self.rfile = io.BytesIO(body)
        self.responses: list[tuple[object, object]] = []

    def _send_json(self, status: object, payload: object) -> None:
        self.responses.append((status, payload))


def test_serve_post_interrupted_write_leaves_a_whole_envelope(
    tmp_path, monkeypatch
) -> None:
    """A write cut short mid-serialisation leaves a whole old or new envelope.

    The write is interrupted by making ``json.dump`` — the serialiser every
    write path shares — emit a prefix and then fail. A writer that builds a
    sibling temporary and renames it over the destination survives this with
    the previous envelope untouched; a writer that truncates and writes the
    destination in place is left holding the prefix, which the read-back below
    refuses to parse.
    """
    state_root = tmp_path / "state"
    project_dir = state_root / "alpha"
    project_dir.mkdir(parents=True)
    target = project_dir / "settings.json"
    old_envelope = {
        "updated": "2026-10-02T00:00:00",
        "project": "alpha",
        "doc": "settings",
        "data": {"_version": 3, "theme": "old"},
    }
    target.write_text(json.dumps(old_envelope, indent=2) + "\n", encoding="utf-8")
    monkeypatch.setattr(serve, "_STATE_ROOT", state_root)

    real_dumps = json.dumps

    def interrupted_dump(payload, handle, **kwargs):
        handle.write(real_dumps(payload, **kwargs)[:12])
        raise RuntimeError("injected mid-write failure")

    monkeypatch.setattr(json, "dump", interrupted_dump)

    body = real_dumps({"theme": "new"}).encode("utf-8")
    handler = _RecordingHandler(
        "/state/alpha/settings.json",
        body,
        {"Content-Length": str(len(body)), "If-Match": "3"},
    )
    # The injected failure is expected to escape the handler; what this check
    # is about is the file the handler left behind.
    with contextlib.suppress(RuntimeError):
        handler._handle_post()

    after = json.loads(target.read_text(encoding="utf-8"))
    assert after["data"]["_version"] in (old_envelope["data"]["_version"], 4)
