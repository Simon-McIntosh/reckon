"""A batch dispatch reads each plan once, however many entries name it.

Twenty entries over four plans whose sections wait on one another queue a
batch. The batch's wait resolution reads each plan's state, its text and its
authored section headings, and each of the three counts is bounded by the
number of distinct plans the batch touches — never by the entries or by the
wait edges they declare. The requests the batch stores carry the same waits and
phases they did before the read was shared.

A second batch of two plans with three sections each measures the plan-document
read itself: six entries read each plan's document once, so the count is two.
That assertion is what holds the scoped read, and reverting the reader to a
plain one raises the count with the entries.

The negative control is the base revision, where each entry and each wait edge
resolves its own copy of its target plan: there the counts grow with the
entries.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import _plan_html, metadata_index
from reckon.crew import dispatch_batch, runs

PROJECT = "proj"
SESSION = "coordinator-batch"

PLANS = ("plan-a", "plan-b", "plan-c", "plan-d")
SECTIONS = ("s1", "s2", "s3", "s4", "s5")

#: Each plan's own sections wait on another plan's, so every entry resolves a
#: foreign section and the walk steps through a further plan behind it. The
#: graph runs a→b→d and a→c→d, so no cycle closes and every target is a
#: distinct plan from the one naming it.
WAITS: dict[str, dict[str, list[str]]] = {
    "plan-a": {
        section: [f"plan-b#{section}", f"plan-c#{section}"] for section in SECTIONS
    },
    "plan-b": {section: [f"plan-d#{section}"] for section in SECTIONS},
    "plan-c": {section: [f"plan-d#{section}"] for section in SECTIONS},
    "plan-d": {},
}


def _config() -> dict:
    return {
        "default_backend": "clive",
        "local_backend": "clive",
        "backends": {
            "clive": {
                "launch": "cli",
                "command": "codex",
                "model": "local-model",
                "effort": "medium",
                "sandbox": "worktree-full",
                "time_budget": "25m",
            }
        },
        "roles": {"implement": {}},
        "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    }


def _plan(
    slug: str,
    sections: tuple[str, ...] = SECTIONS,
    waits: dict[str, list[str]] | None = None,
) -> str:
    quote = '"'
    declarations = json.dumps(dict.fromkeys(sections, "implementable"))
    depends_on = json.dumps(waits or {})
    body = "".join(
        f"<h2 id={quote}{section}{quote}>§{section[1:]}</h2>"
        f"<p>Build {slug} {section}.</p>"
        f"<p>Write paths: <code>mod_{slug.replace('-', '_')}.py</code>.</p>"
        f"<p><strong>Done when</strong> the {slug} {section} check passes.</p>"
        for section in SECTIONS
    )
    return (
        "<!doctype html>\n"
        f"<meta name={quote}docs-project{quote} content={quote}{PROJECT}{quote}>\n"
        f"<meta name={quote}reckon-type{quote} content={quote}plan{quote}>\n"
        f"<meta name={quote}plan-slug{quote} content={quote}{slug}{quote}>\n"
        f"<meta name={quote}plan-status{quote} content={quote}active{quote}>\n"
        f"<meta name={quote}plan-section-declarations{quote} content={quote}"
        f"{declarations.replace(quote, '&quot;')}{quote}>\n"
        f"<meta name={quote}plan-section-depends-on{quote} content={quote}"
        f"{depends_on.replace(quote, '&quot;')}{quote}>\n"
        f"{body}\n"
    )


def _entries(plans: tuple[str, ...], sections: tuple[str, ...]) -> list[dict]:
    """One entry per section of every plan."""
    return [
        {
            "project": PROJECT,
            "plan": slug,
            "section": section,
            "node": f"{slug}-{section}",
            "goal": f"build {slug} {section}",
            "time_budget": "20m",
            "spec_level": "exact",
        }
        for slug in plans
        for section in sections
    ]


def _batch() -> list[dict]:
    """One entry per section of every plan: twenty entries over four plans."""
    return _entries(PLANS, SECTIONS)


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)


def _checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    plans: tuple[str, ...],
    sections: tuple[str, ...],
    waits: dict[str, dict[str, list[str]]],
) -> Path:
    """A throwaway checkout carrying one authored plan file per slug."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repository = tmp_path / "checkout"
    state = repository / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n",
        encoding="utf-8",
    )
    plans_dir = repository / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    for slug in plans:
        (plans_dir / f"{slug}.html").write_text(
            _plan(slug, sections, waits.get(slug, {})), encoding="utf-8"
        )
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "worker@example.invalid")
    _git(repository, "config", "user.name", "Worker")
    _git(repository, "add", "--", "docs")
    _git(repository, "commit", "-q", "-m", "chore: seed checkout")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    return repository


@pytest.fixture()
def batch_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return _checkout(tmp_path, monkeypatch, PLANS, SECTIONS, WAITS)


#: A second batch shape: two plans, three sections each. Small enough that the
#: plan-document read count is one per plan and nothing else, so a regression
#: that re-reads a plan for a second section is visible as a third read.
SMALL_PLANS = ("plan-x", "plan-y")
SMALL_SECTIONS = ("s1", "s2", "s3")
SMALL_WAITS: dict[str, dict[str, list[str]]] = {
    "plan-x": {section: [f"plan-y#{section}"] for section in SMALL_SECTIONS},
    "plan-y": {},
}


@pytest.fixture()
def small_batch_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return _checkout(tmp_path, monkeypatch, SMALL_PLANS, SMALL_SECTIONS, SMALL_WAITS)


def _queue(repository: Path, entries: list[dict] | None = None) -> dict:
    def config_for(_project: str) -> dict:
        return json.loads(json.dumps(_config()))

    return dispatch_batch.queue_batch(
        entries if entries is not None else _batch(),
        session=SESSION,
        config_for=config_for,
        repo_root=repository,
    )


def test_a_batch_reads_each_plan_once(
    batch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    states: list[str] = []
    original_state = metadata_index.discovery_state

    def counted_state(*args, **kwargs):
        states.append(str(args[0]) if args else "")
        return original_state(*args, **kwargs)

    headings: list[str] = []
    original_headings = _plan_html.authored_section_headings

    def counted_headings(html_text: str):
        headings.append(html_text[:40])
        return original_headings(html_text)

    monkeypatch.setattr(metadata_index, "discovery_state", counted_state)
    monkeypatch.setattr(_plan_html, "authored_section_headings", counted_headings)

    result = _queue(batch_repo)

    distinct = len(PLANS)
    assert result["refused"] == 0, result["results"]
    assert result["queued"] == 20
    assert len(states) <= distinct, (
        "each distinct plan's state is read at most once for the whole batch, "
        f"however many of its sections and wait edges are resolved: {len(states)} "
        f"state reads for {distinct} plans"
    )
    assert len(headings) <= distinct, (
        "each distinct plan's section headings are parsed at most once for the "
        f"whole batch: {len(headings)} parses for {distinct} plans"
    )

    # The requests the batch stores carry the waits the plans declare, so a
    # shared read changed what the batch paid, not what it wrote.
    stored = {
        str((pointer.get("node") or {}).get("id") or ""): pointer
        for pointer in runs.list_live(project=PROJECT)
    }
    assert len(stored) == 20, sorted(stored)
    a_one = stored["plan-a-s1"]
    assert a_one["phase"] == "awaiting-upstream"
    assert sorted(a_one["waits"]) == ["plan-b#s1", "plan-c#s1"]
    d_one = stored["plan-d-s1"]
    assert d_one.get("waits") in (None, []), (
        "a section that declares no wait is queueable at once"
    )


def test_a_batch_reads_each_plan_document_once(
    small_batch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One plan document is read once however many of its sections a batch names.

    Two plans with three sections each queue six entries, and the plan's own
    document is read exactly twice for the whole batch: once for the plan whose
    sections wait, once for the plan they wait on. Reverting the scoped read to
    a plain one raises the count with the entries, which is what makes this
    assertion the one that holds the saving.
    """
    from reckon.crew.admission import release as release_module

    documents: list[str] = []
    original_document = release_module._plan_html

    def counted_document(repo_root, plan):
        documents.append(str(plan))
        return original_document(repo_root, plan)

    headings: list[str] = []
    original_headings = _plan_html.authored_section_headings

    def counted_headings(html_text: str):
        headings.append(html_text[:40])
        return original_headings(html_text)

    monkeypatch.setattr(release_module, "_plan_html", counted_document)
    monkeypatch.setattr(_plan_html, "authored_section_headings", counted_headings)

    result = _queue(small_batch_repo, _entries(SMALL_PLANS, SMALL_SECTIONS))

    assert result["refused"] == 0, result["results"]
    assert result["queued"] == 6
    assert len(documents) == 2, (
        "six entries over two plans read each plan document once, so the count "
        f"is one per plan and never per entry or per wait edge: {len(documents)} "
        f"reads of {sorted(documents)}"
    )
    assert sorted(documents) == sorted(SMALL_PLANS)
    assert len(headings) == 2, (
        "each distinct plan text is parsed for its section headings once for "
        f"the whole batch: {len(headings)} parses"
    )

    stored = {
        str((pointer.get("node") or {}).get("id") or ""): pointer
        for pointer in runs.list_live(project=PROJECT)
    }
    assert len(stored) == 6, sorted(stored)
    x_one = stored["plan-x-s1"]
    assert x_one["phase"] == "awaiting-upstream"
    assert x_one["waits"] == ["plan-y#s1"]
    assert stored["plan-y-s1"].get("waits") in (None, [])
