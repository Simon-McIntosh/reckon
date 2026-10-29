"""An admission pass derives a project's readiness once, not once per launch.

Every queued launch judged its own waits by rebuilding the whole project
roadmap, and the cache that rebuild filled was created inside the batch call it
ran in — so a pass launching three requests in one project derived the same
roadmap three times. Admission throughput, not the lane, then caps how full the
lane gets: passes that should take under two minutes took up to fifteen.

The pass now holds one readiness cache and hands it to every launch's
``dispatch()``, and through that to ``dispatch_waits``,
``_validated_batch_waits`` and ``_readiness_rows``. The entry is keyed by the
project, its docs directory and a fingerprint of the plan tree, so a plan
edited or a landing merged while the pass runs misses the cache rather than
being read as current. A direct dispatch supplies no cache and builds its own.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import plan_review, queue_admission
from reckon.crew.dispatch_admission import LaneHeld
from tests import test_a_queued_request_waits_on_its_dependencies as fixtures

dispatch_module = importlib.import_module("reckon.crew.dispatch")

PROJECT = fixtures.PROJECT
PLAN = fixtures.PLAN
SECTIONS = ("s1", "s2", "s3")


def _plan_html(waits: dict[str, list[str]]) -> str:
    """One plan: three ready sections, a done upstream, and a gate on a fourth.

    The gate is what makes a launch ask for its own section's verdict. A plan
    carrying no gate and no decision is judged from its authored waits alone,
    and never derives the roadmap this measurement counts.
    """
    quote = '"'
    declarations = {
        "s1": "done",
        "s2": "implementable",
        "s3": "implementable",
        "s9": "implementable",
    }
    declared = json.dumps(declarations).replace(quote, "&quot;")
    awaited = json.dumps(waits).replace(quote, "&quot;")
    return (
        "<!doctype html>\n"
        f"<meta name={quote}docs-project{quote} content={quote}{PROJECT}{quote}>\n"
        f"<meta name={quote}reckon-type{quote} content={quote}plan{quote}>\n"
        f"<meta name={quote}plan-slug{quote} content={quote}{PLAN}{quote}>\n"
        f"<meta name={quote}plan-status{quote} content={quote}active{quote}>\n"
        f"<meta name={quote}plan-section-declarations{quote} content={quote}"
        f"{declared}{quote}>\n"
        f"<meta name={quote}plan-section-depends-on{quote} content={quote}"
        f"{awaited}{quote}>\n"
        f"<h2 id={quote}s1{quote}>§1</h2>\n<h2 id={quote}s2{quote}>§2</h2>\n"
        f"<h2 id={quote}s3{quote}>§3</h2>\n<h2 id={quote}s9{quote}>§9</h2>\n"
        '<section data-reckon="gates"><div class="r-gate" data-id="ship" '
        'data-status="open" data-verdict="" data-gated-sections="s9">'
        '<h4 class="r-gate-measure">approve the release</h4></div></section>\n'
    )


def _seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, waits: dict[str, list[str]]
) -> Path:
    """The seeded checkout carrying this measurement's own plan."""
    repository = fixtures.chain_repo.__wrapped__(tmp_path, monkeypatch)
    relative = f"docs/plans/{PLAN}.html"
    (repository / relative).write_text(_plan_html(waits), encoding="utf-8")
    fixtures._run(repository, "add", "--", relative)
    fixtures._run(repository, "commit", "-q", "-m", "chore: author the gated plan")
    return repository


@pytest.fixture()
def pass_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checkout whose three ready sections declare no waits of their own."""
    return _seed(tmp_path, monkeypatch, {})


def _queue(repository: Path) -> dict:
    nodes = [
        fixtures._node(f"n{index}", section) for index, section in enumerate(SECTIONS)
    ]
    return queue_admission.queue_requests(
        nodes,
        project=PROJECT,
        session=fixtures.SESSION,
        repo_root=repository,
        config=fixtures.CONFIG,
        run_ids={f"n{index}": f"r-{section}" for index, section in enumerate(SECTIONS)},
    )


def _builds(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Count every roadmap build, through the one function that derives one."""
    counted: list[Path] = []
    original = plan_review.project_section_readiness

    def counting(project: str, docs_dir: Path) -> dict[str, Any]:
        counted.append(1)
        return original(project, docs_dir)

    monkeypatch.setattr(plan_review, "project_section_readiness", counting)
    return counted


def _admit(launcher: Any = None) -> tuple[dict, list[str]]:
    recorded: list[str] = []
    result = queue_admission.admit_once(
        config_for=fixtures._config_for(),
        launcher=launcher if launcher is not None else fixtures._launcher(recorded),
    )
    return result, recorded


def _advance_mtime(path: Path) -> None:
    """Move a plan's mtime forward, as an edit landing mid-pass does."""
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))


def test_a_pass_launching_three_requests_builds_the_readiness_once(
    pass_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three launches in one project derive that project's roadmap once."""
    stored = _queue(pass_repo)
    assert [stored[f"r-{section}"]["phase"] for section in SECTIONS] == [
        queue_admission.QUEUED_PHASE
    ] * len(SECTIONS)
    builds = _builds(monkeypatch)

    result, launched = _admit()

    assert sorted(result["launched"]) == ["r-s1", "r-s2", "r-s3"], result
    assert len(launched) == 3
    assert len(builds) == 1, builds


def test_a_plan_touched_between_two_launches_forces_a_rebuild(
    pass_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An edit landing mid-pass is read, not answered from the pass's cache."""
    _queue(pass_repo)
    builds = _builds(monkeypatch)
    plan = pass_repo / "docs" / "plans" / f"{PLAN}.html"
    recorded: list[str] = []
    plain = fixtures._launcher(recorded)

    def launch(
        document: Any, *, log_path: Any, stderr_path: Any, prompt_path: Any
    ) -> int:
        if not recorded:
            _advance_mtime(plan)
        return plain(
            document,
            log_path=log_path,
            stderr_path=stderr_path,
            prompt_path=prompt_path,
        )

    result, _launched = _admit(launch)

    assert sorted(result["launched"]) == ["r-s1", "r-s2", "r-s3"], result
    assert len(recorded) == 3
    assert len(builds) == 2, builds


def test_a_direct_dispatch_outside_a_pass_builds_its_own(
    pass_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dispatch no pass holds a cache for derives its own readiness."""
    _queue(pass_repo)
    builds = _builds(monkeypatch)
    _admit()
    assert len(builds) == 1, builds

    with pytest.raises(LaneHeld):
        dispatch_module.dispatch(
            node=fixtures._node("direct", "s9"),
            project=PROJECT,
            repo=pass_repo,
            config=fixtures.CONFIG,
            session=fixtures.SESSION,
            run_id="r-direct",
            check_budget=False,
            watch_override=True,
            unreviewed_plan_override=True,
            local=True,
            launcher=lambda *args, **kwargs: pytest.fail(
                "a gated dispatch must not launch"
            ),
        )

    # The pass built once; this dispatch builds its own twice, through the two
    # caches it creates for itself — the section verdict it reads and the waits
    # it resolves. What it must not do is read the pass's cache.
    assert len(builds) == 3, builds


def test_the_launches_read_the_waits_an_uncached_computation_gives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Caching the readiness changes how often it is built, not what it says."""
    repository = _seed(
        tmp_path, monkeypatch, {"s2": [f"{PLAN}#s1"], "s3": [f"{PLAN}#s1"]}
    )
    _queue(repository)
    original = queue_admission.dispatch_waits
    seen: list[tuple[list[str], dict[str, str], list[str]]] = []

    def recording(*args: Any, **kwargs: Any) -> Any:
        answer = original(*args, **kwargs)
        seen.append(answer)
        return answer

    monkeypatch.setattr(queue_admission, "dispatch_waits", recording)

    result, launched = _admit()

    assert sorted(result["launched"]) == ["r-s1", "r-s2", "r-s3"], result
    assert len(launched) == 3
    assert len(seen) == 3, seen
    # A launch that waits on nothing compares [] with [] and shows nothing, so
    # this arm requires the comparison to have had a real wait in it.
    assert any(refs for refs, _projects, _unmet in seen), seen

    monkeypatch.setattr(queue_admission, "dispatch_waits", original)
    uncached = [
        original(
            fixtures._node(f"n{index}", section), repo_root=repository, project=PROJECT
        )
        for index, section in enumerate(SECTIONS)
    ]

    assert sorted(seen) == sorted(uncached)
