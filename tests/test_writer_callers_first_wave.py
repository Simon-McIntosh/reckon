"""The first wave of callers reproduces exactly what it wrote before.

Every literal in this file was recorded against the revision that preceded the
migration, for one fixed payload, by driving each migrated caller and reading
the file it produced. The migration routes those callers through the shared
atomic writer, which is only a correct refactor if each caller's serialisation,
permission bits, fsync behaviour and locking are unchanged -- so the recorded
bytes and modes are asserted here rather than trusted to the new call.

The delegation tests are the other half: they patch the shared writer on the
caller's own module and assert the caller asks for it with the parameters its
recorded output implies. Patching a name a module does not carry is an
AttributeError, so these also state the landing itself -- a caller that still
owns its own temporary-and-rename cannot pass them.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

CANONICAL_PAYLOAD = {"z": 1, "a": {"n": [1, 2]}, "m": "text", "b": None}

# Recorded byte for byte from the pre-migration writers: json.dumps with
# indent=2 and sorted keys, terminated by one newline.
CANONICAL_TEXT = (
    "{\n"
    '  "a": {\n'
    '    "n": [\n'
    "      1,\n"
    "      2\n"
    "    ]\n"
    "  },\n"
    '  "b": null,\n'
    '  "m": "text",\n'
    '  "z": 1\n'
    "}\n"
)

PLAN_REVIEW_TEXT = (
    "{\n"
    '  "plan_slug": "slug",\n'
    '  "plan_version": 3,\n'
    '  "project": "proj",\n'
    '  "score": 88,\n'
    '  "status": "passed",\n'
    '  "timestamp": "2026-01-02T03:04:05+00:00"\n'
    "}\n"
)

RESOURCES_TEXT = (
    '{\n  "format": 1,\n  "moves": [],\n  "project": "proj",\n  "rewrites": []\n}\n'
)

REVIEW_RECORD = {
    "project": "proj",
    "plan_slug": "slug",
    "plan_version": 3,
    "timestamp": "2026-01-02T03:04:05+00:00",
    "status": "passed",
    "score": 88,
}


def _process_default_mode() -> int:
    """The mode a plain ``Path.write_text`` creates under the current umask.

    The callers that held no explicit permission requirement used the process
    default before the migration; the shared writer reproduces it by passing
    ``mode=None``. The recorded value was ``0o644`` under the recording umask
    of ``0o022``; deriving it here keeps the assertion true on any host.
    """
    current = os.umask(0)
    os.umask(current)
    return 0o666 & ~current


def _written(path: Path) -> tuple[str, int]:
    return path.read_text(encoding="utf-8"), stat.S_IMODE(path.stat().st_mode)


def _capture(monkeypatch, module) -> list[dict]:
    """Replace the module's shared writer with a recorder and return its log."""
    calls: list[dict] = []

    def record(path, payload, **kwargs):
        calls.append({"path": Path(path), "payload": payload, "kwargs": kwargs})
        return Path(path)

    monkeypatch.setattr(module, "write_json_atomically", record)
    return calls


# --------------------------------------------------------------------------
# Byte-for-byte and mode-for-mode reproduction of the recorded base output.
# --------------------------------------------------------------------------


def test_private_json_default_output_unchanged(tmp_path):
    from reckon import _backends

    destination = tmp_path / "private.json"
    _backends._write_private_json(destination, CANONICAL_PAYLOAD)

    text, mode = _written(destination)
    assert text == CANONICAL_TEXT
    assert mode == 0o600


def test_private_json_owner_only_mode_unchanged(tmp_path):
    from reckon import _backends

    destination = tmp_path / "private-owner.json"
    _backends._write_private_json(destination, CANONICAL_PAYLOAD, 0o400)

    text, mode = _written(destination)
    assert text == CANONICAL_TEXT
    assert mode == 0o400


def test_capabilities_output_unchanged(tmp_path, monkeypatch):
    from reckon import capabilities

    monkeypatch.setattr(
        capabilities, "derive_capabilities", lambda *a, **k: CANONICAL_PAYLOAD
    )
    target = tmp_path / "capabilities.json"
    capabilities.rebuild_capabilities(path=target)

    text, mode = _written(target)
    assert text == CANONICAL_TEXT
    assert mode == _process_default_mode()


def test_fleet_supervisor_output_unchanged(tmp_path):
    from reckon.crew import fleet_supervisor

    target = tmp_path / "fleet.json"
    fleet_supervisor._write_json(target, CANONICAL_PAYLOAD)

    text, mode = _written(target)
    assert text == CANONICAL_TEXT
    assert mode == 0o600


def test_placement_reservation_output_unchanged(tmp_path, monkeypatch):
    from reckon.crew import placement

    target = tmp_path / "reservation.json"
    monkeypatch.setattr(placement, "reservation_path", lambda project=None: target)
    placement.publish_reservation(CANONICAL_PAYLOAD, "proj")

    text, mode = _written(target)
    assert text == CANONICAL_TEXT
    assert mode == _process_default_mode()


def test_plan_review_output_unchanged(tmp_path):
    from reckon.crew import plan_review

    written = plan_review.store_plan_review(
        dict(REVIEW_RECORD), base_dir=tmp_path / "reviews"
    )

    text, mode = _written(Path(written))
    assert text == PLAN_REVIEW_TEXT
    assert mode == _process_default_mode()


def test_resources_manifest_output_unchanged(tmp_path, monkeypatch):
    from reckon import resources

    monkeypatch.setattr(
        resources,
        "build_migration_manifest",
        lambda docs_dir, project: {
            "format": 1,
            "project": "proj",
            "moves": [],
            "rewrites": [],
        },
    )
    docs = tmp_path / "docs"
    docs.mkdir()
    resources.migrate_typed_layout(docs, "proj")

    target = docs / resources.MANIFEST_PATH
    text, mode = _written(target)
    assert text == RESOURCES_TEXT
    assert mode == _process_default_mode()


def _serve_cache(tmp_path, monkeypatch, module, store_name, path_name, payload_name):
    target = tmp_path / f"{store_name}.json"
    repository = tmp_path / f"repo-{store_name}"
    (repository / ".git").mkdir(parents=True)
    entry = module._GitCreationEntry(head="deadbeef", times={"a": 1})
    monkeypatch.setattr(module, path_name, lambda key: target)
    monkeypatch.setattr(module, payload_name, lambda key, entry: CANONICAL_PAYLOAD)
    getattr(module, store_name)((str(repository), "docs"), entry)
    return target


def test_serve_creation_cache_output_unchanged(tmp_path, monkeypatch):
    from reckon import serve

    target = _serve_cache(
        tmp_path,
        monkeypatch,
        serve,
        "_store_git_creation_cache",
        "_git_creation_cache_path",
        "_git_creation_payload",
    )
    text, mode = _written(target)
    assert text == CANONICAL_TEXT
    assert mode == 0o600


def test_serve_last_modified_cache_output_unchanged(tmp_path, monkeypatch):
    from reckon import serve

    target = _serve_cache(
        tmp_path,
        monkeypatch,
        serve,
        "_store_git_last_modified_cache",
        "_git_last_modified_cache_path",
        "_git_last_modified_payload",
    )
    text, mode = _written(target)
    assert text == CANONICAL_TEXT
    assert mode == 0o600


# --------------------------------------------------------------------------
# The numeric decoders: one shared decoder, the private helpers gone.
# --------------------------------------------------------------------------

DECODER_CASES = [
    (True, None),
    (False, None),
    (0, 0.0),
    (1.5, 1.5),
    ("1", None),
    (None, None),
    ([1], None),
]


@pytest.mark.parametrize("value, expected", DECODER_CASES)
def test_optional_number_matches_the_recorded_expectations(value, expected):
    from reckon._observations import optional_number

    assert optional_number(value) == expected


def test_private_number_helpers_are_gone():
    from reckon import _backends
    from reckon.crew import carryover_census

    assert not hasattr(_backends, "_number")
    assert not hasattr(carryover_census, "_measured")


# --------------------------------------------------------------------------
# Delegation: each migrated caller asks the shared writer for what it needs.
# --------------------------------------------------------------------------


def test_backends_private_json_delegates(tmp_path, monkeypatch):
    from reckon import _backends

    calls = _capture(monkeypatch, _backends)
    _backends._write_private_json(tmp_path / "p.json", CANONICAL_PAYLOAD)

    assert len(calls) == 1
    kwargs = calls[0]["kwargs"]
    assert kwargs["indent"] == 2
    assert kwargs["sort_keys"] is True
    assert kwargs["fsync"] is False
    assert kwargs["mode"] == 0o600


def test_backends_private_json_keeps_the_owner_only_ceiling(tmp_path, monkeypatch):
    from reckon import _backends

    calls = _capture(monkeypatch, _backends)
    _backends._write_private_json(tmp_path / "p.json", CANONICAL_PAYLOAD, 0o400)

    assert calls[0]["kwargs"]["mode"] == 0o400


def test_capabilities_delegates(tmp_path, monkeypatch):
    from reckon import capabilities

    calls = _capture(monkeypatch, capabilities)
    monkeypatch.setattr(
        capabilities, "derive_capabilities", lambda *a, **k: CANONICAL_PAYLOAD
    )
    capabilities.rebuild_capabilities(path=tmp_path / "capabilities.json")

    assert len(calls) == 1
    kwargs = calls[0]["kwargs"]
    assert kwargs["indent"] == 2
    assert kwargs["sort_keys"] is True
    assert kwargs["fsync"] is False
    assert kwargs["mode"] is None


def test_fleet_supervisor_delegates_and_refuses_to_create_the_parent(
    tmp_path, monkeypatch
):
    from reckon.crew import fleet_supervisor

    calls = _capture(monkeypatch, fleet_supervisor)
    fleet_supervisor._write_json(tmp_path / "fleet.json", CANONICAL_PAYLOAD)

    assert len(calls) == 1
    kwargs = calls[0]["kwargs"]
    assert kwargs["mode"] == 0o600
    assert kwargs["create_parents"] is False


def test_placement_delegates(tmp_path, monkeypatch):
    from reckon.crew import placement

    calls = _capture(monkeypatch, placement)
    placement.publish_reservation(CANONICAL_PAYLOAD, "proj")

    assert len(calls) == 1
    kwargs = calls[0]["kwargs"]
    assert kwargs["indent"] == 2
    assert kwargs["sort_keys"] is True
    assert kwargs["mode"] is None


def test_plan_review_delegates(tmp_path, monkeypatch):
    from reckon.crew import plan_review

    calls = _capture(monkeypatch, plan_review)
    plan_review.store_plan_review(dict(REVIEW_RECORD), base_dir=tmp_path / "reviews")

    assert len(calls) == 1
    kwargs = calls[0]["kwargs"]
    assert kwargs["indent"] == 2
    assert kwargs["sort_keys"] is True
    assert kwargs["mode"] is None


def test_resources_manifest_delegates(tmp_path, monkeypatch):
    from reckon import resources

    monkeypatch.setattr(
        resources,
        "build_migration_manifest",
        lambda docs_dir, project: {
            "format": 1,
            "project": "proj",
            "moves": [],
            "rewrites": [],
        },
    )
    calls = _capture(monkeypatch, resources)
    docs = tmp_path / "docs"
    docs.mkdir()
    resources.migrate_typed_layout(docs, "proj")

    assert len(calls) == 1
    kwargs = calls[0]["kwargs"]
    assert kwargs["indent"] == 2
    assert kwargs["sort_keys"] is True
    assert kwargs["mode"] is None


@pytest.mark.parametrize(
    "store_name, path_name, payload_name",
    [
        (
            "_store_git_creation_cache",
            "_git_creation_cache_path",
            "_git_creation_payload",
        ),
        (
            "_store_git_last_modified_cache",
            "_git_last_modified_cache_path",
            "_git_last_modified_payload",
        ),
    ],
)
def test_serve_caches_delegate(
    tmp_path, monkeypatch, store_name, path_name, payload_name
):
    from reckon import serve

    calls = _capture(monkeypatch, serve)
    target = tmp_path / f"{store_name}.json"
    repository = tmp_path / f"repo-{store_name}"
    (repository / ".git").mkdir(parents=True)
    entry = serve._GitCreationEntry(head="deadbeef", times={"a": 1})
    monkeypatch.setattr(serve, path_name, lambda key: target)
    monkeypatch.setattr(serve, payload_name, lambda key, entry: CANONICAL_PAYLOAD)
    getattr(serve, store_name)((str(repository), "docs"), entry)

    assert len(calls) == 1
    kwargs = calls[0]["kwargs"]
    assert kwargs["indent"] == 2
    assert kwargs["sort_keys"] is True
    assert kwargs["mode"] == 0o600


# --------------------------------------------------------------------------
# The shared writer: new parameters, atomicity on failure, directory fsync.
# --------------------------------------------------------------------------


def test_shared_writer_accepts_the_new_parameters(tmp_path):
    from reckon._store import write_json_atomically

    target = tmp_path / "custom.json"
    write_json_atomically(
        target,
        CANONICAL_PAYLOAD,
        indent=None,
        sort_keys=False,
        fsync=False,
        mode=0o640,
        fsync_directory=False,
    )

    text, mode = _written(target)
    assert text == json.dumps(CANONICAL_PAYLOAD, indent=None, sort_keys=False) + "\n"
    assert mode == 0o640


def test_a_failure_mid_write_leaves_old_or_new_content(tmp_path, monkeypatch):
    from reckon import _store

    target = tmp_path / "target.json"
    target.write_text("OLD\n", encoding="utf-8")

    def exploding_dump(obj, handle, **kwargs):
        handle.write("PARTIAL")
        raise RuntimeError("injected failure")

    monkeypatch.setattr(_store.json, "dump", exploding_dump)

    with pytest.raises(RuntimeError):
        _store.write_json_atomically(target, CANONICAL_PAYLOAD)

    assert target.read_text(encoding="utf-8") == "OLD\n"
    assert list(tmp_path.iterdir()) == [target]


def test_fsync_directory_flushes_the_parent(tmp_path, monkeypatch):
    from reckon import _store

    flushes: list[int | None] = []
    real_fsync = os.fsync

    def recording_fsync(fd):
        try:
            flushes.append(stat.S_IFMT(os.fstat(fd).st_mode))
        except OSError:
            flushes.append(None)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)

    nested = tmp_path / "nested"
    nested.mkdir()
    _store.write_json_atomically(nested / "f.json", {"a": 1}, fsync_directory=True)

    assert stat.S_IFDIR in flushes


def test_fsync_directory_off_by_default_touches_no_directory(tmp_path, monkeypatch):
    from reckon import _store

    flushes: list[int | None] = []
    real_fsync = os.fsync

    def recording_fsync(fd):
        try:
            flushes.append(stat.S_IFMT(os.fstat(fd).st_mode))
        except OSError:
            flushes.append(None)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)

    _store.write_json_atomically(tmp_path / "f.json", {"a": 1})

    assert stat.S_IFDIR not in flushes
