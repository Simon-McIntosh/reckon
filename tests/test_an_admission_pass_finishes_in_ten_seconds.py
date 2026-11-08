"""Admission reads named plans and remembers a proved checkout image."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import checkout_trust, mcp_discovery, serve
from reckon.crew import plan_review, queue_admission, runs
from tests import test_a_queued_request_waits_on_its_dependencies as fixtures


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repository = fixtures.chain_repo.__wrapped__(tmp_path, monkeypatch)
    plans = repository / "docs" / "plans"
    (plans / "plan-a.html").unlink()
    for index in range(960):
        slug = f"upstream-{index}"
        status = ("done", "implementable", "blocked")[index % 3]
        (plans / f"{slug}.html").write_text(_plan(slug, status), encoding="utf-8")
    for index in range(40):
        slug = f"work-{index}"
        (plans / f"{slug}.html").write_text(
            _plan(slug, "implementable", f"upstream-{index}#s1"),
            encoding="utf-8",
        )
    return repository


def _plan(slug: str, status: str, wait: str = "") -> str:
    waits = f"{{&quot;s1&quot;: [&quot;{wait}&quot;]}}" if wait else "{}"
    return (
        '<!doctype html><html><head><meta name="docs-project" content="proj">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{slug}">'
        '<meta name="plan-status" content="active">'
        f'<meta name="plan-section-declarations" content="{{&quot;s1&quot;: &quot;{status}&quot;}}">'
        f'<meta name="plan-section-depends-on" content="{waits}">'
        '</head><body><h2 id="s1">Work</h2></body></html>'
    )


def _requests(repository: Path) -> list[dict]:
    pointers = []
    for index in range(40):
        pointer = {
            "run_id": f"r-{index}",
            "project": "proj",
            "repo": str(repository),
            "phase": queue_admission.AWAITING_UPSTREAM_PHASE,
            "node": {"id": f"work-{index}", "plan": f"work-{index}", "section": "s1"},
            "waits": [f"upstream-{index}#s1"],
        }
        queue_admission._write_pointer(pointer["run_id"], pointer)
        pointers.append(pointer)
    return pointers


def test_pass_reads_named_plans_and_sees_an_edit(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pointers = _requests(project)
    counts = {"discovery": 0, "discover_plans": 0, "readiness": 0}

    def discovery(*args: object, **kwargs: object) -> None:
        counts["discovery"] += 1
        raise AssertionError("project discovery entered admission")

    def readiness(*args: object, **kwargs: object) -> None:
        counts["readiness"] += 1
        raise AssertionError("whole-project readiness entered admission")

    def discover_plans(*args: object, **kwargs: object) -> None:
        counts["discover_plans"] += 1
        raise AssertionError("plan discovery entered admission")

    monkeypatch.setattr(mcp_discovery, "_discover_project", discovery)
    monkeypatch.setattr(serve, "discover_plans", discover_plans)
    monkeypatch.setattr(plan_review, "project_section_readiness", readiness)
    started = time.monotonic()
    queue_admission.advance_waiting_requests(pointers)
    first_seconds = time.monotonic() - started
    phases = [runs.read_pointer(pointer["run_id"])["phase"] for pointer in pointers]
    expected = [
        (
            queue_admission.QUEUED_PHASE,
            queue_admission.AWAITING_UPSTREAM_PHASE,
            queue_admission.BLOCKED_UPSTREAM_PHASE,
        )[index % 3]
        for index in range(40)
    ]
    assert phases == expected
    assert counts == {"discovery": 0, "discover_plans": 0, "readiness": 0}

    edited = project / "docs" / "plans" / "upstream-1.html"
    edited.write_text(_plan("upstream-1", "done"), encoding="utf-8")
    stat = edited.stat()
    os.utime(edited, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
    started = time.monotonic()
    queue_admission.advance_waiting_requests(pointers)
    second_seconds = time.monotonic() - started
    print(
        f"readiness pass seconds: first={first_seconds:.3f} second={second_seconds:.3f}"
    )
    assert runs.read_pointer("r-1")["phase"] == queue_admission.QUEUED_PHASE
    assert counts == {"discovery": 0, "discover_plans": 0, "readiness": 0}


def test_queued_placements_never_walks_project_discovery(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    counts = {"fresh": 0, "walk": 0}

    def fresh(*args: object, **kwargs: object) -> None:
        counts["fresh"] += 1
        raise AssertionError("fresh discovery entered queue placement")

    def walk(*args: object, **kwargs: object) -> None:
        counts["walk"] += 1
        raise AssertionError("discovery signature walked queue placement")

    monkeypatch.setattr(
        mcp_discovery, "_docs_dir_for_project", lambda *_: project / "docs"
    )
    monkeypatch.setattr(serve, "_fresh_discovery", fresh)
    monkeypatch.setattr(serve, "_walk_discovery_signature", walk)
    now = datetime.now(UTC)
    pointers = [
        {
            **pointer,
            "phase": queue_admission.QUEUED_PHASE,
            "session": "one",
            "backend": "local",
            "queued_at": now.isoformat(),
            "plan": f"work-{index}",
            "section": "s1",
        }
        for index, pointer in enumerate(_requests(project))
    ]
    assert len(queue_admission.queued_placements(pointers, now=now)) == 40
    assert counts == {"fresh": 0, "walk": 0}


def test_code_stamp_is_probed_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probes = []
    queue_admission._TRUSTED_CODE_STAMPS.clear()
    monkeypatch.setattr(queue_admission, "_source_root", lambda: tmp_path)
    monkeypatch.setattr(queue_admission, "_admit_code_stamp", lambda: "known-stamp")
    monkeypatch.setattr(
        checkout_trust, "tracked_reckon_tree_is_dirty", lambda _: (False, "")
    )
    monkeypatch.setattr(checkout_trust, "published_reload_image", lambda _: False)

    def probe(root: Path) -> tuple[bool, str]:
        probes.append(root)
        return True, ""

    monkeypatch.setattr(checkout_trust, "_probe_imports", probe)
    assert queue_admission._checkout_code_is_trusted()
    assert queue_admission._checkout_code_is_trusted()
    assert probes == [tmp_path]
