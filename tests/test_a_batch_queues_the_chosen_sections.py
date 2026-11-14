"""A coordinator queues the sections it chose as one batch call.

A batch of six entries over two fixture plans shows that queuing the chosen
work is cheap and that the section stays the authority: an entry's omitted
fields come from its own section, its given fields win, an entry may wait on
another entry of the same batch, a section that names no write paths is queued
with no fence rather than refused, a repeat of an already-queued section is
reported rather than stored twice, each plan is parsed once and the project's
roadmap is derived once for the whole batch.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import _plan_html, roadmap
from reckon.crew import dispatch_batch, plan_review, queue_admission, runs

PROJECT = "proj"
SESSION = "coordinator-a"
PLAN_A = "plan-a"
PLAN_B = "plan-b"

CONFIG = {
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

#: The six sections the batch takes: three from one plan, two with a
#: write-paths sentence and one without, and a third with its own sentence.
SECTIONS_A = ("s1", "s2", "s3")
SECTIONS_B = ("s1", "s2")


def _plan(slug: str, sections: dict[str, str], declarations: dict[str, str]) -> str:
    quote = '"'
    body = "".join(
        f"<h2 id={quote}{name}{quote}>§{name[1:]}</h2>{prose}"
        for name, prose in sections.items()
    )
    return (
        "<!doctype html>\n"
        f"<meta name={quote}docs-project{quote} content={quote}{PROJECT}{quote}>\n"
        f"<meta name={quote}reckon-type{quote} content={quote}plan{quote}>\n"
        f"<meta name={quote}plan-slug{quote} content={quote}{slug}{quote}>\n"
        f"<meta name={quote}plan-status{quote} content={quote}active{quote}>\n"
        f"<meta name={quote}plan-section-declarations{quote} content={quote}"
        f"{json.dumps(declarations).replace(quote, '&quot;')}{quote}>\n"
        f"{body}\n"
    )


def _section(title: str, paths: str | None) -> str:
    prose = f"<p>{title}.</p>"
    if paths is not None:
        prose += f"<p>Write paths: {paths}.</p>"
    # A later paragraph naming files: the write-paths sentence ends at its full
    # stop, so neither this path nor these abbreviations are the section's.
    prose += "<p>See e.g. notes.py for v1.2.</p>"
    prose += f"<p><strong>Done when</strong> the {title.lower()} check passes.</p>"
    return prose


def _batch() -> list[dict]:
    return [
        {
            "project": PROJECT,
            "plan": PLAN_A,
            "section": "s1",
            "node": "n-a1",
            "goal": "build one",
            "time_budget": "20m",
            "spec_level": "exact",
        },
        {
            "project": PROJECT,
            "plan": PLAN_A,
            "section": "s2",
            "node": "n-a2",
            "goal": "build two",
            "time_budget": "20m",
            "spec_level": "exact",
            "after": ["n-a1"],
        },
        {
            # Omits done-when and write paths: both come from the section.
            "project": PROJECT,
            "plan": PLAN_A,
            "section": "s3",
            "node": "n-a3",
            "goal": "build three",
            "time_budget": "20m",
            "spec_level": "exact",
        },
        {
            # Gives its own write paths: they win over the section's.
            "project": PROJECT,
            "plan": PLAN_B,
            "section": "s1",
            "node": "n-b1",
            "goal": "build b-one",
            "time_budget": "20m",
            "spec_level": "exact",
            "write_paths": ["custom/own_module.py"],
        },
        {
            # Its section names no write paths and the entry gives none.
            "project": PROJECT,
            "plan": PLAN_B,
            "section": "s2",
            "node": "n-b2",
            "goal": "build b-two",
            "time_budget": "20m",
            "spec_level": "exact",
        },
        {
            "project": PROJECT,
            "plan": PLAN_B,
            "section": "s1",
            "node": "n-b3",
            "goal": "build b-one again",
            "time_budget": "20m",
            "spec_level": "exact",
            "write_paths": ["custom/second_module.py"],
        },
    ]


def _run(repository: Path, *arguments: str) -> None:
    import subprocess

    subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)


@pytest.fixture()
def batch_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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
    plans = repository / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / f"{PLAN_A}.html").write_text(
        _plan(
            PLAN_A,
            {
                # Names one file, then prose abbreviations a token scan would
                # mistake for paths if it accepted any short suffix.
                "s1": _section(
                    "One", "<code>one.py</code>, the v1.2 copy, e.g. the rest"
                ),
                "s2": _section("Two", "<code>two.py</code>"),
                "s3": _section("Three", "<code>three.py</code>"),
            },
            dict.fromkeys(SECTIONS_A, "implementable"),
        ),
        encoding="utf-8",
    )
    (plans / f"{PLAN_B}.html").write_text(
        _plan(
            PLAN_B,
            {
                "s1": _section("B one", "<code>b_one.py</code>"),
                "s2": _section("B two", None),
            },
            dict.fromkeys(SECTIONS_B, "implementable"),
        ),
        encoding="utf-8",
    )
    # A third plan the batch never names: it makes the plan count differ from
    # the entry count, so a parse that scaled with entries could not pass as
    # one-per-named-plan.
    (plans / "plan-c.html").write_text(
        _plan(
            "plan-c",
            {"s1": _section("C one", "<code>c_one.py</code>")},
            {"s1": "implementable"},
        ),
        encoding="utf-8",
    )
    _run(repository, "init", "-q", "-b", "main")
    _run(repository, "config", "user.email", "worker@example.invalid")
    _run(repository, "config", "user.name", "Worker")
    _run(repository, "add", "--", "docs")
    _run(repository, "commit", "-q", "-m", "chore: seed checkout")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    return repository


def _queue(repository: Path) -> dict:
    def config_for(_project: str) -> dict:
        return json.loads(json.dumps(CONFIG))

    return dispatch_batch.queue_batch(
        _batch(),
        session=SESSION,
        config_for=config_for,
        repo_root=repository,
    )


def _pointers() -> dict[str, dict]:
    found: dict[str, dict] = {}
    for pointer in runs.list_live(project=PROJECT):
        node = dict(pointer.get("node") or {})
        found[f"{node.get('plan')}#{node.get('section')}:{node.get('id')}"] = pointer
    return found


def test_a_batch_of_six_entries_queues_the_chosen_sections(batch_repo: Path) -> None:
    result = _queue(batch_repo)
    rows = {row["node"]: row for row in result["results"]}

    assert result["refused"] == 0, (
        "no entry is refused: the worktree is the scope when a section names "
        "no write paths"
    )
    assert rows["n-b2"]["status"] == "queued"
    assert rows["n-b2"]["plan"] == PLAN_B and rows["n-b2"]["section"] == "s2"
    for node_id in ("n-a1", "n-a2", "n-a3", "n-b1", "n-b3"):
        assert rows[node_id]["status"] in {"queued", "awaiting-upstream"}, rows[node_id]

    pointers = _pointers()

    # 0. A section that states no write-paths sentence supplies none, so the
    #    entry is stored with an empty fence rather than refused for lacking
    #    the sentence its section does not carry.
    assert pointers[f"{PLAN_B}#s2:n-b2"]["node"]["write_paths"] == []

    # 1. An entry waiting on another entry of the batch is stored
    #    awaiting-upstream on that entry's stored run id.
    assert rows["n-a2"]["status"] == "awaiting-upstream"
    upstream = rows["n-a1"]["run_id"]
    assert rows["n-a2"]["waits"] == [f"run:{upstream}"]
    assert pointers[f"{PLAN_A}#s2:n-a2"]["phase"] == (
        queue_admission.AWAITING_UPSTREAM_PHASE
    )

    # 2. An entry that omits its done-when and write paths is stored with the
    #    section's own.
    stored = dict(pointers[f"{PLAN_A}#s3:n-a3"]["node"])
    assert stored["done_when"].endswith("the three check passes.")
    assert stored["write_paths"] == ["three.py"]

    # 2b. The section's write-paths sentence is read to its own full stop and
    #     only carries paths: the later paragraph's file and the abbreviations
    #     inside the sentence are neither.
    assert pointers[f"{PLAN_A}#s1:n-a1"]["node"]["write_paths"] == ["one.py"]

    # 3. An entry that gives its own write paths keeps them over the section's.
    assert pointers[f"{PLAN_B}#s1:n-b1"]["node"]["write_paths"] == [
        "custom/own_module.py"
    ]

    # 4. An entry for a section that already has a queued request is reported
    #    as already queued and not stored twice.
    again = _queue(batch_repo)
    again_rows = {row["node"]: row for row in again["results"]}
    assert again_rows["n-a1"]["status"] == "already-queued"
    assert again_rows["n-a1"]["run_id"] == rows["n-a1"]["run_id"]
    same = [p for p in runs.list_live(project=PROJECT) if p.get("run_id") == upstream]
    assert len(same) == 1, "a repeat of a queued section stores no second request"


def test_a_batch_parses_each_plan_once_and_builds_one_roadmap(
    batch_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parsed: list[str] = []
    original = _plan_html.section_prose

    def counted(document, **kwargs):
        parsed.append(str(len(document)))
        return original(document, **kwargs)

    reads: list[str] = []
    original_readiness = plan_review.project_section_readiness

    def recording_readiness(project, docs_dir):
        reads.append(project)
        return original_readiness(project, docs_dir)

    built: list[set[str]] = []
    original_build = roadmap.build_roadmap

    def recording_build(_project, inventory, *args, **kwargs):
        built.append({str(item.get("slug")) for item in inventory})
        return original_build(_project, inventory, *args, **kwargs)

    monkeypatch.setattr(_plan_html, "section_prose", counted)
    monkeypatch.setattr(plan_review, "project_section_readiness", recording_readiness)
    monkeypatch.setattr(roadmap, "build_roadmap", recording_build)

    result = _queue(batch_repo)

    assert len(parsed) == 2, (
        "each of the two named plans is parsed once for the whole batch, "
        f"however many of its sections are chosen: {len(parsed)} parses"
    )
    assert reads == [], (
        "wait derivation reads each entry's section through the per-section "
        f"readiness, never the project's whole readiness: {reads}"
    )
    # The count itself is the measured property: a batch that derives the
    # project's roadmap per stored request, or again for each section whose
    # waits it checks, grows with the sections it queues. One derivation serves
    # every reader inside the batch, so six entries over one project cost one.
    assert len(built) == 1, (
        "the batch derives its project's roadmap exactly once for the whole "
        f"call, however many entries it queues: {len(built)} builds "
        f"({result['queued']} stored) {built}"
    )
