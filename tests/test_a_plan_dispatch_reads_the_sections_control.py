"""A plan-level dispatch reads a section's negative control from the section.

A plan section states the mutation its check must fail against in its own text.
Before this, a whole-plan dispatch read the section's done-when and write paths
and nothing else, so every section whose scope includes a test file was refused
at queue time and could only be queued by a hand-written batch entry repeating
the sentence. These tests drive the plan-level dispatch over a fixture plan
across the four shapes the section distinguishes: a stated control is carried
onto the request; a section with a check and no stated control is refused,
naming the section and the paragraph it lacks; a control declared ``none`` with
a reason is carried as that declaration; and a batch entry that names its own
control overrides the section's.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon.crew import dispatch_batch, runs
from reckon.crew.node import negative_control_is_none, negative_control_reason

PROJECT = "proj"
SESSION = "coordinator-a"
PLAN = "plan-a"
OTHER_PLAN = "plan-b"

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

#: The sentence the fixture's first section states as its negative control. It
#: is the whole declaration, so a request carrying it carries the paragraph.
STATED_CONTROL = "Reverting the guard makes its test fail."

#: The reason the fixture's third section declares no mutation applies.
NONE_REASON = "the check is a fixture asserting no production path changes"

#: The control a batch entry names for itself, which must win over the
#: section's own paragraph.
ENTRY_CONTROL = "Reverting the entry's own guard makes its test fail."


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "--no-pager", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
    )


def _section(*, done_when: str, write_paths: str, control: str | None) -> str:
    paragraphs = [
        f"<p><strong>Done when.</strong> <code>{done_when}</code> passes.</p>",
        f"<p><strong>Write paths:</strong> <code>{write_paths}</code>.</p>",
    ]
    if control is not None:
        paragraphs.append(f"<p><strong>Negative control.</strong> {control}</p>")
    return "".join(paragraphs)


def _plan_html(slug: str, sections: dict[str, str]) -> str:
    quote = '"'
    declarations = json.dumps(dict.fromkeys(sections, "implementable"))
    declarations = declarations.replace(quote, "&quot;")
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
        f"{declarations}{quote}>\n"
        f"{body}\n"
    )


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    root = tmp_path / "checkout"
    scripts = root / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).parents[1] / "skills" / "reckon-build" / "scripts"
    (scripts / "worktree_fleet.py").write_text(
        (source / "worktree_fleet.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    # The derived write paths must sit in a present directory, exactly as a
    # section naming a test file in this repository's own tree does.
    (root / "tests").mkdir()
    (root / "docs" / "state" / PROJECT / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n",
        encoding="utf-8",
    )
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / f"{PLAN}.html").write_text(
        _plan_html(
            PLAN,
            {
                "s1": _section(
                    done_when="tests/test_one.py",
                    write_paths="tests/test_one.py",
                    control=STATED_CONTROL,
                ),
                "s2": _section(
                    done_when="tests/test_two.py",
                    write_paths="tests/test_two.py",
                    control=None,
                ),
                "s3": _section(
                    done_when="tests/test_three.py",
                    write_paths="tests/test_three.py",
                    control=f"None, because {NONE_REASON}.",
                ),
                "s4": _section(
                    done_when="tests/test_four.py",
                    write_paths="tests/test_four.py",
                    control=STATED_CONTROL,
                ),
            },
        ),
        encoding="utf-8",
    )
    (plans / f"{OTHER_PLAN}.html").write_text(
        _plan_html(
            OTHER_PLAN,
            {
                "s1": _section(
                    done_when="tests/test_other.py",
                    write_paths="tests/test_other.py",
                    control=STATED_CONTROL,
                ),
            },
        ),
        encoding="utf-8",
    )
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "worker@example.invalid")
    _git(root, "config", "user.name", "Worker")
    _git(root, "add", "--", "docs", "skills")
    _git(root, "commit", "-q", "-m", "chore: seed checkout")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _config_for(_project: str) -> dict:
    return json.loads(json.dumps(CONFIG))


def _whole_plan(repository: Path, plan: str = PLAN) -> dict:
    return dispatch_batch.queue_plan(
        PROJECT,
        plan,
        session=SESSION,
        config_for=_config_for,
        repo_root=repository,
    )


def _row(result: dict, section: str) -> dict:
    return next(row for row in result["results"] if row["section"] == section)


def _node(run_id: str) -> dict:
    return dict(runs.read_pointer(run_id).get("node") or {})


def test_a_section_that_states_its_control_is_queued_with_that_sentence(
    repository: Path,
) -> None:
    """A plan-level dispatch carries the section's paragraph onto the request."""
    result = _whole_plan(repository)
    row = _row(result, "s1")
    assert row["status"] == "queued", result
    node = _node(row["run_id"])
    assert node.get("negative_control") == STATED_CONTROL, node
    assert runs.read_pointer(row["run_id"]).get("dispatch_width") == "plan"


def test_a_section_with_a_check_and_no_control_is_refused_by_name(
    repository: Path,
) -> None:
    """The refusal names the section and the paragraph it lacks."""
    result = _whole_plan(repository)
    row = _row(result, "s2")
    assert row["status"] == "refused", result
    reason = str(row.get("reason") or "")
    assert "s2" in reason, reason
    assert "negative-control paragraph" in reason, reason
    # No request was stored, so the refusal is the only record of the attempt.
    assert row.get("run_id") in (None, ""), row


def test_a_section_that_declares_none_is_queued_with_that_declaration(
    repository: Path,
) -> None:
    """A ``none`` declaration carries its reason onto the request."""
    result = _whole_plan(repository)
    row = _row(result, "s3")
    assert row["status"] == "queued", result
    declaration = str(_node(row["run_id"]).get("negative_control") or "")
    assert negative_control_is_none(declaration), declaration
    assert NONE_REASON in negative_control_reason(declaration), declaration


def test_a_batch_entry_naming_its_own_control_overrides_the_section(
    repository: Path,
) -> None:
    """The entry's control wins over the paragraph the section states."""
    entries = [
        {
            "project": PROJECT,
            "plan": OTHER_PLAN,
            "section": "s1",
            "node": f"{OTHER_PLAN}-s1",
            "goal": "build the other section",
            "spec_level": "exact",
            "negative_control": ENTRY_CONTROL,
            "time_budget": "20m",
        }
    ]
    result = queue_batch_fixture(repository, entries)
    row = _row(result, "s1")
    assert row["status"] == "queued", result
    assert _node(row["run_id"]).get("negative_control") == ENTRY_CONTROL


def queue_batch_fixture(repository: Path, entries: list[dict]) -> dict:
    return dispatch_batch.queue_batch(
        entries,
        session=SESSION,
        config_for=_config_for,
        repo_root=repository,
        dispatch_width="batch",
    )
