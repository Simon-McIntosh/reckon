"""An admission pass, a landing pass and a dispatch each observe the tree once.

The synthetic project below holds 40 plans, and 30 awaiting-upstream requests
naming 70 refs across 12 of those plans. Each pass reads plan state through the
shared memo, so the project's readiness is derived once however many requests
and refs ask for it, and the passes bound how many paths they observe rather
than one per lookup.
"""

from __future__ import annotations

import json
import os
import subprocess
from contextlib import contextmanager, nullcontext
from pathlib import Path

import pytest

from reckon import _plan_html, metadata_index
from reckon.crew import plan_review, queue_admission, queue_landing, runs
from reckon.crew.dispatch import TaskNode, plan_dispatch
from tests import test_a_queued_request_waits_on_its_dependencies as fixtures

UPSTREAM_PLANS = 28
WORK_PLANS = 12
UPSTREAM_SECTIONS = 3
REQUESTS = 30
REF_COUNT = 70
PATH_BUDGET = 300
READ_STATE_BUDGET = 40

UPSTREAM_STATES = ("done", "implementable", "blocked")


def _attribute(value: object) -> str:
    return json.dumps(value).replace('"', "&quot;")


def _plan(slug: str, declarations: dict[str, str], waits: dict[str, list[str]]) -> str:
    headings = "".join(f'<h2 id="{section}">Work</h2>' for section in declarations)
    return (
        '<!doctype html><html><head><meta name="docs-project" content="proj">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{slug}">'
        '<meta name="plan-status" content="active">'
        f'<meta name="plan-section-declarations" content="{_attribute(declarations)}">'
        f'<meta name="plan-section-depends-on" content="{_attribute(waits)}">'
        f"</head><body>{headings}</body></html>"
    )


def _upstream_refs() -> list[str]:
    return [
        f"upstream-{plan}#s{section + 1}"
        for plan in range(UPSTREAM_PLANS)
        for section in range(UPSTREAM_SECTIONS)
    ]


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repository = fixtures.chain_repo.__wrapped__(tmp_path, monkeypatch)
    plans = repository / "docs" / "plans"
    for path in plans.glob("*.html"):
        path.unlink()
    for index in range(UPSTREAM_PLANS):
        slug = f"upstream-{index}"
        declarations = {
            f"s{section + 1}": UPSTREAM_STATES[(index + section) % 3]
            for section in range(UPSTREAM_SECTIONS)
        }
        (plans / f"{slug}.html").write_text(
            _plan(slug, declarations, {}), encoding="utf-8"
        )
    refs = _upstream_refs()
    for index in range(WORK_PLANS):
        slug = f"work-{index}"
        declarations = {f"s{section + 1}": "implementable" for section in range(5)}
        waits = {
            f"s{section + 1}": [refs[(index * 5 + section) % len(refs)]]
            for section in range(5)
        }
        (plans / f"{slug}.html").write_text(
            _plan(slug, declarations, waits), encoding="utf-8"
        )
    assert len(list(plans.glob("*.html"))) == UPSTREAM_PLANS + WORK_PLANS
    for arguments in (
        ("add", "--", "docs"),
        ("commit", "-q", "-m", "chore: seed the synthetic plan store"),
    ):
        subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
        )
    return repository


def _requests(repository: Path) -> list[dict]:
    refs = _upstream_refs()
    per_request = REF_COUNT // REQUESTS
    requests = []
    for index in range(REQUESTS):
        named = [
            refs[(index * per_request + offset) % len(refs)]
            for offset in range(per_request)
        ]
        requests.append(
            {
                "run_id": f"r-{index:02d}",
                "project": fixtures.PROJECT,
                "repo": str(repository),
                "phase": queue_admission.AWAITING_UPSTREAM_PHASE,
                "node": {
                    "id": f"work-{index:02d}",
                    "plan": f"work-{index % WORK_PLANS}",
                    "section": f"s{index % 5 + 1}",
                },
                "waits": named,
            }
        )
    for request in requests:
        queue_admission._write_pointer(request["run_id"], request)
    return requests


class Counts:
    """The observations one call makes, counted through patches."""

    def __init__(self) -> None:
        self.paths: set[str] = set()
        self.read_state = 0

    def reset(self) -> None:
        self.paths.clear()
        self.read_state = 0

    @property
    def path_count(self) -> int:
        return len(self.paths)


@pytest.fixture()
def counts(monkeypatch: pytest.MonkeyPatch) -> Counts:
    counted = Counts()
    real_stat = os.stat
    real_read_state = _plan_html.read_state

    def counting_stat(*args: object, **kwargs: object):
        counted.paths.add(os.fspath(args[0]) if args else "")
        return real_stat(*args, **kwargs)

    def counting_read_state(html_text: str) -> dict:
        counted.read_state += 1
        return real_read_state(html_text)

    monkeypatch.setattr(os, "stat", counting_stat)
    monkeypatch.setattr(_plan_html, "read_state", counting_read_state)
    return counted


def _pass(
    monkeypatch: pytest.MonkeyPatch,
    requests: list[dict],
    *,
    shared: bool = True,
) -> list[str]:
    with (
        queue_admission.readiness_pass() if shared else nullcontext(),
    ):
        queue_admission.advance_waiting_requests(requests)
    return [runs.read_pointer(request["run_id"])["phase"] for request in requests]


def test_one_pass_reads_the_project_once(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    counts: Counts,
) -> None:
    """Case one: one readiness read, and at most one parse per plan."""

    requests = _requests(project)
    builds = 0
    real = plan_review.project_section_readiness

    def counting(project_name: str, docs_dir: Path) -> dict:
        nonlocal builds
        builds += 1
        return real(project_name, docs_dir)

    counts.reset()
    with monkeypatch.context() as patch:
        patch.setattr(plan_review, "project_section_readiness", counting)
        _pass(monkeypatch, requests)
    assert builds == 1, f"project readiness derived {builds} times"
    assert counts.read_state <= READ_STATE_BUDGET, counts.read_state


def test_phases_match_the_per_ref_read(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case two: the shared read answers each request's phase alike."""

    shared = _pass(monkeypatch, _requests(project))
    per_ref = _pass(monkeypatch, _requests(project), shared=False)
    assert shared == per_ref


def test_a_plan_edited_between_passes_is_seen(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Case three: the second pass misses its cache for the edited plan."""

    requests = _requests(project)
    before = _pass(monkeypatch, requests)
    path = project / "docs" / "plans" / "upstream-0.html"
    path.write_text(
        _plan("upstream-0", {"s1": "done", "s2": "done", "s3": "done"}, {}),
        encoding="utf-8",
    )
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
    after = _pass(monkeypatch, requests)
    assert after != before


def test_a_dispatch_preview_observes_the_tree_once(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    counts: Counts,
) -> None:
    """Case four: one dry-run stats fewer than 300 paths, inside the boundary."""

    node = TaskNode(
        id="preview",
        goal="preview one section",
        plan="work-0",
        section="s1",
        done_when="pytest reports passed",
        write_paths=["a.txt"],
        spec_level="exact",
    )
    counts.reset()
    entered: list[bool] = []
    real_scope = metadata_index.discovery_file_scope

    @contextmanager
    def watched():
        with real_scope():
            entered.append(metadata_index._DISCOVERY_FILES.get() is not None)
            yield

    with monkeypatch.context() as patch:
        patch.setattr(metadata_index, "discovery_file_scope", watched)
        plan_dispatch(
            node=node,
            config=json.loads(json.dumps(fixtures.CONFIG)),
            project=fixtures.PROJECT,
            repo=project,
            base="HEAD",
            session=fixtures.SESSION,
        )
    assert entered and all(entered), "the preview ran outside the observation boundary"
    assert counts.path_count < PATH_BUDGET, counts.path_count


def test_a_landing_pass_with_nothing_to_land_observes_the_tree_once(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    counts: Counts,
) -> None:
    """Case five: a pass that lands nothing stats fewer than 300 paths.

    Nothing in this store is delivered, so the pass reads no document at all:
    the stat count alone would hold at the base revision too. The case
    therefore also asserts that the pass ran inside the observation boundary,
    which is what a removed scope entry takes away.
    """

    _requests(project)
    counts.reset()
    entered: list[bool] = []
    real_scope = metadata_index.discovery_file_scope

    @contextmanager
    def watched():
        with real_scope():
            entered.append(metadata_index._DISCOVERY_FILES.get() is not None)
            yield

    with monkeypatch.context() as patch:
        patch.setattr(metadata_index, "discovery_file_scope", watched)
        result = queue_landing.land_once()
    assert result["landed"] == []
    assert entered and all(entered), "the landing pass ran outside the boundary"
    assert counts.path_count < PATH_BUDGET, counts.path_count
