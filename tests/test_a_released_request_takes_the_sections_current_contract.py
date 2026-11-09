"""A released request takes the section's current contract.

A batch entry that omits a field takes the section's value, so the stored
request records which of its node fields the section supplied. A request held
because the plan moved is released onto the text it moved to: each field the
section supplied is re-derived from the section at the release base, and the
pointer records the old and new digest of every re-derived field. A field the
dispatcher supplied, and a field ``crew queue edit`` set, are the request's own
and are kept through the same change.

The fixture is a synthesised mounted project under ``RECKON_HOME``, so nothing
here reads or writes the operator's crew home.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import flight
from reckon.crew import dispatch_batch, plan_review, queue_admission, queue_edit, runs

PROJECT = "proj"
SESSION = "coordinator-a"
PLAN = "plan-a"
RUN_ID = "r-released"

OLD_DONE_WHEN = "pytest -q tests/test_old.py reports 2 passed"
NEW_DONE_WHEN = "pytest -q tests/test_new.py reports 5 passed"
SUPPLIED_DONE_WHEN = "pytest -q tests/test_supplied.py reports 1 passed"
EDITED_DONE_WHEN = "pytest -q tests/test_edited.py reports 3 passed"

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
    "roles": {"implement": {}, "review": {"backend": "clive"}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    # The hold is taken only in this mode, and a hold is what the release path
    # this node changes answers.
    "plan_review_gate": "enforce",
}


def _run(repository: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)


def _words(prefix: str, count: int = 12) -> str:
    return " ".join(f"{prefix}{index:02d}" for index in range(count))


def _plan_html(done_when: str) -> str:
    declarations = json.dumps({"s2": "implementable"}).replace('"', "&quot;")
    return (
        "<!doctype html>\n<html><head>\n"
        f'<meta name="docs-project" content="{PROJECT}">\n'
        '<meta name="reckon-type" content="plan">\n'
        f'<meta name="plan-slug" content="{PLAN}">\n'
        '<meta name="plan-title" content="Plan A">\n'
        '<meta name="plan-status" content="active">\n'
        '<meta name="plan-version" content="3">\n'
        f'<meta name="plan-section-declarations" content="{declarations}">\n'
        "</head><body>\n"
        '<h2 id="s2">§2</h2>'
        f"<p>{_words('s2')}</p>"
        "<p>Write paths: <code>src/one.py</code>.</p>"
        f"<p><strong>Done when</strong>: {done_when}.</p>\n"
        "</body></html>\n"
    )


def _write_plan(repository: Path, done_when: str) -> None:
    (repository / "docs" / "plans" / f"{PLAN}.html").write_text(
        _plan_html(done_when), encoding="utf-8"
    )


def _commit(repository: Path, message: str) -> None:
    _run(repository, "add", "--", "docs")
    _run(repository, "commit", "-q", "-m", message)


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_VELOCITY_CACHE", str(config_home / "cache" / "velocity"))
    monkeypatch.setattr(
        flight, "resolve", lambda *args, **kwargs: SimpleNamespace(config=CONFIG)
    )

    repository = tmp_path / "checkout"
    state = repository / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n",
        encoding="utf-8",
    )
    (repository / "docs" / "plans").mkdir(parents=True)
    _write_plan(repository, OLD_DONE_WHEN)
    _run(repository, "init", "-q", "-b", "main")
    _run(repository, "config", "user.email", "worker@example.invalid")
    _run(repository, "config", "user.name", "Worker")
    _commit(repository, "chore: seed the plan")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}),
        encoding="utf-8",
    )
    _store_review(repository / "docs" / "plans" / f"{PLAN}.html", run_id="review-one")
    return repository


def _entry(**extra) -> dict:
    entry = {
        "project": PROJECT,
        "plan": PLAN,
        "section": "s2",
        "node": "released-node",
        "goal": "Deliver the section's measure.",
        "time_budget": "20m",
        "spec_level": "exact",
        "negative_control": "none: this request writes no check",
    }
    entry.update(extra)
    return entry


def _queue(repository: Path, **extra) -> str:
    result = dispatch_batch.queue_batch(
        [_entry(**extra)],
        session=SESSION,
        config_for=lambda _project: json.loads(json.dumps(CONFIG)),
        repo_root=repository,
    )
    assert result["refused"] == 0, json.dumps(result)
    return str(result["results"][0]["run_id"])


def _pointer(run_id: str) -> dict:
    return runs.read_pointer(run_id)


def _digest(value) -> str:
    canonical = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _store_review(path: Path, *, run_id: str) -> None:
    from reckon import _plan_html
    from reckon.crew import recovery

    subject = {
        "subject": "plan",
        "project": PROJECT,
        "plan_slug": PLAN,
        "plan_path": str(path),
        "repo": str(path.parents[2]),
        "session": SESSION,
        "rubric": "design",
        "local": True,
        "run_id": run_id,
    }
    fields = recovery._review_dispatch_fields(subject)
    sidecard = json.loads(Path(fields["sidecar"]).read_text(encoding="utf-8"))
    sidecard["plan_version"] = int(
        _plan_html.read_state(path.read_text(encoding="utf-8"))["version"]
    )
    Path(sidecard["report_path"]).write_text("RUBRIC wiring: pass\n", encoding="utf-8")
    plan_review.store_plan_review(
        {**sidecard, "findings": [], "responses": {}, "review_run_id": run_id}
    )


def _config_for():
    return lambda _pointer: CONFIG


def _launcher(recorded: list[str]):
    def launch(plan, *, log_path, stderr_path, prompt_path):
        recorded.append(Path(str(prompt_path)).name)
        return 1_000_000

    return launch


def _move_the_section(repository: Path, done_when: str) -> None:
    """Change the section's done-when and commit it, leaving it unreviewed."""
    _write_plan(repository, done_when)
    _commit(repository, "chore: steer the section")


def _release(repository: Path, run_id: str) -> None:
    """Hold the request on the moved section, review the move, then admit it."""
    queue_admission.admit_once(config_for=_config_for(), launcher=_launcher([]))
    assert _pointer(run_id)["phase"] == queue_admission.HELD_PHASE, _pointer(run_id)
    _store_review(repository / "docs" / "plans" / f"{PLAN}.html", run_id="review-two")
    launched: list[str] = []
    result = queue_admission.admit_once(
        config_for=_config_for(), launcher=_launcher(launched)
    )
    assert result["released_plan_review"] == [run_id], result
    assert launched, "the released request launches on the pass that releases it"


def test_a_section_derived_done_when_is_re_derived_at_release(project: Path) -> None:
    run_id = _queue(project)
    pointer = _pointer(run_id)
    before = str(pointer["node"]["done_when"])
    assert OLD_DONE_WHEN in before, before
    assert pointer["node"]["section_derived"]["done_when"] == _digest(before), pointer

    _move_the_section(project, NEW_DONE_WHEN)
    _release(project, run_id)

    pointer = _pointer(run_id)
    after = str(pointer["node"]["done_when"])
    assert NEW_DONE_WHEN in after, after
    assert after != before
    recorded = pointer["section_rederivation"]["done_when"]
    assert recorded["old"] == _digest(before), recorded
    assert recorded["new"] == _digest(after), recorded


def test_a_supplied_done_when_is_kept_through_the_same_change(project: Path) -> None:
    run_id = _queue(project, done_when=SUPPLIED_DONE_WHEN)
    pointer = _pointer(run_id)
    assert pointer["node"]["done_when"] == SUPPLIED_DONE_WHEN
    assert "done_when" not in (pointer["node"].get("section_derived") or {})

    _move_the_section(project, NEW_DONE_WHEN)
    _release(project, run_id)

    pointer = _pointer(run_id)
    assert pointer["node"]["done_when"] == SUPPLIED_DONE_WHEN, pointer["node"]
    assert "done_when" not in (pointer.get("section_rederivation") or {})


def test_a_done_when_set_by_a_queue_edit_is_kept(project: Path) -> None:
    run_id = _queue(project)
    queue_edit.edit(run_id, done_when=EDITED_DONE_WHEN)
    pointer = _pointer(run_id)
    assert pointer["node"]["done_when"] == EDITED_DONE_WHEN
    assert "done_when" not in (pointer["node"].get("section_derived") or {})

    _move_the_section(project, NEW_DONE_WHEN)
    _release(project, run_id)

    pointer = _pointer(run_id)
    assert pointer["node"]["done_when"] == EDITED_DONE_WHEN, pointer["node"]


def test_the_real_crew_home_is_untouched(project: Path) -> None:
    """Every record these cases write lives under the temporary crew home."""
    real_home = Path.home() / ".config" / "reckon" / "crew"
    before = sorted(path.name for path in (real_home / "live").glob("*.json"))
    run_id = _queue(project)
    _move_the_section(project, NEW_DONE_WHEN)
    _release(project, run_id)

    assert runs.crew_home() != real_home
    after = sorted(path.name for path in (real_home / "live").glob("*.json"))
    assert after == before
    assert not (real_home / "live" / f"{run_id}.json").exists()
