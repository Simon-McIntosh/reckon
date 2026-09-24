"""An append to a plan survives a write that meets it on the same file.

Two writers reach one plan HTML: an edit that appends a comment, and the
landing write a promotion performs. The plan records a lost update whose
version counter still advanced, so nothing raised a conflict and nothing could
be noticed: the meeting was silent and the counter agreed the write had landed.
"""

from __future__ import annotations

import contextlib
import json
import threading
from pathlib import Path

import pytest

from reckon import _plan_html, _store

PROJECT = "append-fixture"
PLAN = "append-target"
ANCHOR = "s5"


def _seed(root: Path) -> Path:
    path = root / "docs" / "plans" / f"{PLAN}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title></head>"
        '<body><main class="plan-doc"></main></body></html>\n'
    )
    state = {
        "type": "plan",
        "slug": PLAN,
        "title": "Append target",
        "status": "active",
        "version": 0,
        "comments": {},
    }
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")
    return path


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    _seed(root)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _comment(ident: str) -> dict:
    return {
        "id": ident,
        "who": "reckon-build",
        "when": "2026-09-18T00:00:00Z",
        "body": f"<p>{ident}</p>",
    }


def _append(root: Path, ident: str, *, retries: int = 0) -> int:
    """Append one section comment the way a landing write does.

    ``retries`` mirrors the promotion path, which re-reads and re-appends after
    a version conflict instead of discarding its comment.
    """
    for _attempt in range(retries + 1):
        state, version = _store.read_plan(PROJECT, PLAN, root, artifact_type="plan")
        comments = {
            key: list(items) for key, items in (state.get("comments") or {}).items()
        }
        comments.setdefault(ANCHOR, []).append(_comment(ident))
        try:
            return _store.write_plan(
                PROJECT,
                PLAN,
                {**state, "comments": comments},
                version,
                root,
                artifact_type="plan",
            )
        except _store.VersionConflict:
            continue
    raise AssertionError(f"append {ident!r} never landed")


def _identifiers(root: Path) -> tuple[list[str], int]:
    state, version = _store.read_plan(PROJECT, PLAN, root, artifact_type="plan")
    return [item["id"] for item in state["comments"].get(ANCHOR, [])], version


def test_an_append_meeting_a_landing_write_is_not_lost(
    repository: Path, monkeypatch: Path
) -> None:
    # Rendezvous both writers at the render step, so each has proven its own
    # version check before either replaces the file. This is the meeting the
    # plan measured: both pass, both write, one write is discarded.
    gate = threading.Barrier(2)
    render = _plan_html.write_state

    def rendering(html_text, state):
        # A peer that already passed the barrier leaves a later wait to time out;
        # render anyway, so the retry path is measured.

        with contextlib.suppress(threading.BrokenBarrierError):
            gate.wait(timeout=2.0)
        return render(html_text, state)

    monkeypatch.setattr(_plan_html, "write_state", rendering)

    outcomes: dict[str, object] = {}

    def run(name: str, write, *arguments, **keywords) -> None:
        try:
            outcomes[name] = write(*arguments, **keywords)
        except (AssertionError, OSError) as exc:  # the meeting's own failure modes
            outcomes[name] = exc

    landing = threading.Thread(
        target=run,
        args=("landing", _append, str(repository), "c-landing"),
        kwargs={"retries": 3},
    )
    edit = threading.Thread(
        target=run,
        args=("edit", _append, str(repository), "c-edit"),
    )
    landing.start()
    edit.start()
    landing.join(timeout=30)
    edit.join(timeout=30)

    identifiers, version = _identifiers(repository)
    assert set(identifiers) == {"c-landing", "c-edit"}, (
        f"a comment was discarded on the meeting: {identifiers} at version "
        f"{version}; outcomes {outcomes}"
    )
    # Both appends landed, so the counter advanced once per landed write. A
    # single increment here is the lost-update shape: two writes, one counted.
    assert version == 2, f"version {version} does not count both landed writes"

    # Positive control: a sequential append still succeeds and still advances
    # the version, so a fence that refuses everything cannot pass this test.
    assert _append(repository, "c-after") == 3
    identifiers, version = _identifiers(repository)
    assert version == 3
    assert set(identifiers) == {"c-landing", "c-edit", "c-after"}


def test_a_stale_append_that_disagrees_about_the_document_is_refused(
    repository: Path,
) -> None:
    stale, stale_version = _store.read_plan(
        PROJECT, PLAN, repository, artifact_type="plan"
    )
    _store.write_plan(
        PROJECT,
        PLAN,
        {**stale, "status": "blocked"},
        stale_version,
        repository,
        artifact_type="plan",
    )

    comments = {ANCHOR: [_comment("c-late")]}
    with pytest.raises(_store.VersionConflict):
        _store.write_plan(
            PROJECT,
            PLAN,
            {**stale, "comments": comments},
            stale_version,
            repository,
            artifact_type="plan",
        )

    identifiers, _version = _identifiers(repository)
    assert "c-late" not in identifiers
