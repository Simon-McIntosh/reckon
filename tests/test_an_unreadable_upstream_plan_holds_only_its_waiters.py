"""A malformed plan in one project refuses no dispatches in another.

A dispatch reads the plan a wait names. When that plan belongs to another
project and its section records disagree with its declarations, the plan is
unreadable, and reading it strictly refused every dispatch that resolved the
wait -- including dispatches in the same batch that carry no such wait. The
wait is the only thing that depends on that document, so the failure is held
on the requests that carry it, named by the plan and the validation error, and
every other request queues exactly as it would have. A dry run judges the
waits through the same derivation, so a preview cannot pass where the dispatch
refuses. Repairing the upstream plan releases the held request on the next
admission pass.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from reckon.crew import dispatch_batch, queue_admission, runs
from reckon.crew.runs import crew_home

WORK_PROJECT = "nova"
UPSTREAM_PROJECT = "upstream"
SESSION = "coordinator-a"
PLAN = "nova-plan"
UPSTREAM_PLAN = "upstream-plan"
WAIT_REF = f"{UPSTREAM_PROJECT}:{UPSTREAM_PLAN}#s1"

CONFIG = {
    "default_backend": "clive",
    "local_backend": "clive",
    "backends": {
        "clive": {
            "launch": "cli",
            "command": "codex",
            "model": "local-model",
            "effort": "medium",
        }
    },
    "roles": {"implement": {}},
}


def _run(repository: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)


def _plan(
    *,
    project: str,
    slug: str,
    sections: dict,
    depends_on: dict | None = None,
) -> str:
    """One plan document; each section maps to ``(declaration, record status)``.

    The declaration and the section record are authored separately so a test
    can write the disagreement the schema refuses and later repair exactly that
    field.
    """
    quote = '"'
    declarations = {name: declared for name, (declared, _record) in sections.items()}
    parts = [
        "<!doctype html>\n",
        f"<meta name={quote}docs-project{quote} content={quote}{project}{quote}>\n",
        f"<meta name={quote}reckon-type{quote} content={quote}plan{quote}>\n",
        f"<meta name={quote}plan-status{quote} content={quote}active{quote}>\n",
        f"<meta name={quote}plan-slug{quote} content={quote}{slug}{quote}>\n",
        (
            f"<meta name={quote}plan-section-declarations{quote} content={quote}"
            f"{json.dumps(declarations).replace(quote, '&quot;')}{quote}>\n"
        ),
    ]
    if depends_on:
        parts.append(
            f"<meta name={quote}plan-section-depends-on{quote} content={quote}"
            f"{json.dumps(depends_on).replace(quote, '&quot;')}{quote}>\n"
        )
    for name, (_declared, record_status) in sections.items():
        parts.append(f"<h2 id={quote}{name}{quote}>Section {name}</h2>\n")
        parts.append(
            f"<section data-reckon={quote}section{quote} data-id={quote}{name}{quote} "
            f"data-effort-hours={quote}0.25{quote} "
            f'data-capability-version="1.0" data-capability-class="general" '
            f'data-capability-reasoning="deep" '
            f'data-capability-verification="standard" '
            f'data-capability-risk="low" '
            f"data-status={quote}{record_status}{quote}></section>\n"
            "<p>Write paths: <code>work.py</code>.</p>\n"
            "<p><strong>Done when</strong> the check passes.</p>\n"
        )
    return "".join(parts)


def _seed_repository(root: Path, docs_project: str, documents: dict) -> None:
    state = root / "docs" / "state" / docs_project
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": docs_project, "data": {"_version": 0}}) + "\n",
        encoding="utf-8",
    )
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    for slug, text in documents.items():
        (plans / f"{slug}.html").write_text(text, encoding="utf-8")
    _run(root, "init", "-q", "-b", "main")
    _run(root, "config", "user.email", "worker@example.invalid")
    _run(root, "config", "user.name", "Worker")
    _run(root, "add", "--", "docs")
    _run(root, "commit", "-q", "-m", "chore: seed checkout")


@pytest.fixture()
def two_projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple:
    """One dispatching project and one holding the upstream plan."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    work = tmp_path / "nova"
    upstream = tmp_path / "upstream"
    _seed_repository(
        work,
        WORK_PROJECT,
        {
            PLAN: _plan(
                project=WORK_PROJECT,
                slug=PLAN,
                sections={
                    "s1": ("implementable", "implementable"),
                    "s2": ("implementable", "implementable"),
                },
                depends_on={"s2": [WAIT_REF]},
            )
        },
    )
    _seed_repository(
        upstream,
        UPSTREAM_PROJECT,
        {
            UPSTREAM_PLAN: _plan(
                project=UPSTREAM_PROJECT,
                slug=UPSTREAM_PLAN,
                sections={"s1": ("done", "implementable")},
            )
        },
    )
    (config_home / "mounts.json").write_text(
        json.dumps(
            {
                WORK_PROJECT: str(work / "docs"),
                UPSTREAM_PROJECT: str(upstream / "docs"),
            }
        ),
        encoding="utf-8",
    )
    return work, upstream


def _config_for(_project: str) -> dict:
    return json.loads(json.dumps(CONFIG))


def _entries() -> list:
    return [
        {
            "project": WORK_PROJECT,
            "plan": PLAN,
            "section": "s2",
            "node": "waiting-node",
            "goal": "run the section that waits on another project",
            "time_budget": "20m",
            "spec_level": "exact",
        },
        {
            "project": WORK_PROJECT,
            "plan": PLAN,
            "section": "s1",
            "node": "plain-node",
            "goal": "run the section that waits on nothing",
            "time_budget": "20m",
            "spec_level": "exact",
        },
    ]


def _queue(work: Path, **options) -> dict:
    return dispatch_batch.queue_batch(
        _entries(),
        session=SESSION,
        config_for=_config_for,
        repo_root=work,
        **options,
    )


def _row(result: dict, node: str) -> dict:
    return next(row for row in result["results"] if str(row.get("node") or "") == node)


def _pointer(node: str) -> dict:
    for pointer in runs.list_live(project=WORK_PROJECT):
        if str((pointer.get("node") or {}).get("id") or "") == node:
            return pointer
    raise AssertionError(f"no stored request for node {node}")


def _home_snapshot() -> dict:
    home = crew_home()
    if not home.exists():
        return {}
    return {
        str(path.relative_to(home)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted(home.rglob("*"))
        if path.is_file()
    }


def _launcher(recorded: list):
    def launch(plan, *, log_path, stderr_path, prompt_path):
        recorded.append(Path(str(prompt_path)).name)
        return os.getpid()

    return launch


def _repair_upstream(upstream: Path) -> None:
    """Make the upstream section's record agree with its declaration."""
    path = upstream / "docs" / "plans" / (UPSTREAM_PLAN + ".html")
    text = path.read_text(encoding="utf-8")
    broken = 'data-status="implementable"'
    assert broken in text, "the fixture's malformed record is what gets repaired"
    path.write_text(text.replace(broken, 'data-status="done"'), encoding="utf-8")


def test_the_unreadable_plan_holds_only_the_request_that_waits_on_it(
    two_projects: tuple,
) -> None:
    work, _upstream = two_projects

    result = _queue(work)

    assert result["ok"] is True, json.dumps(result["results"], indent=2)
    assert _row(result, "waiting-node")["status"] == "awaiting-upstream"
    assert _row(result, "plain-node")["status"] == "queued"

    waiting = _pointer("waiting-node")
    assert waiting["phase"] == "awaiting-upstream"
    findings = [str(item) for item in waiting.get("upstream_obligations") or ()]
    assert findings, "the unreadable plan must be reported on the request's record"
    assert any(WAIT_REF.partition("#")[0] in item for item in findings), findings
    assert any("disagrees" in item for item in findings), findings

    plain = _pointer("plain-node")
    assert plain["phase"] == "queued"
    assert not list(plain.get("upstream_obligations") or ())


def test_the_dry_run_reports_the_rows_the_dispatch_stores(
    two_projects: tuple,
) -> None:
    work, _upstream = two_projects
    before = _home_snapshot()

    preview = _queue(work, dry_run=True)

    assert preview["dry_run"] is True
    assert _home_snapshot() == before, "a preview writes nothing"
    assert list(runs.list_live(project=WORK_PROJECT)) == []
    stored = _queue(work)

    for node in ("waiting-node", "plain-node"):
        dry_row = _row(preview, node)
        real_row = _row(stored, node)
        assert dry_row["would_phase"] == real_row["status"], node
        assert dry_row["wait_holds"] == real_row["wait_holds"], node
    assert _row(preview, "waiting-node")["would_phase"] == "awaiting-upstream"
    assert _row(preview, "plain-node")["would_phase"] == "queued"


def test_repairing_the_upstream_plan_releases_the_held_request(
    two_projects: tuple,
) -> None:
    work, upstream = two_projects
    _queue(work)

    _repair_upstream(upstream)

    launched: list = []
    queue_admission.admit_once(config_for=_config_for, launcher=_launcher(launched))
    assert len(launched) == 2, launched
    phases = {
        str(pointer.get("phase") or "")
        for pointer in runs.list_live(project=WORK_PROJECT)
    }
    assert "awaiting-upstream" not in phases, phases
