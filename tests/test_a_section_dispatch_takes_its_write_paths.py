"""A single-section dispatch takes the write paths its section states.

A batch entry fills every field it omits from the section it names, and a single
dispatch reaches the same loop. The write paths were the one field that loop left
alone: a section that named its paths still ran its worker with the default
landing scope, and the worker's committed files were then outside the scope the
dispatch held. The section's own paragraph is the authority on those paths, so a
single dispatch derives them exactly as the batch entry does.

Three cases stand beside each other, because a derivation that always answers
the same list would pass any one of them alone:

- a section stating three write paths derives those three and the default
  landing fragment and figure directory every dispatch grants;
- a section stating no write paths derives the defaults alone;
- a dispatch that names its own write paths keeps exactly those.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import cli, crew_dispatch_commands
from reckon.crew import dispatch_batch

PROJECT = "proj"
SESSION = "coordinator-a"
PLAN = "plan-a"

CONFIG = {
    "default_backend": "worker",
    "backends": {
        "worker": {
            "launch": "cli",
            "command": "codex",
            "sandbox": "worktree-full",
            "time_budget": "20m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "8h", "needs_help_after_failures": 2},
}

#: The three paths a section's write-paths sentence names. Two sit in present
#: directories and one is a test, so the sentence exercises the ordinary path
#: reader rather than the described-test fallback.
SECTION_WRITE_PATHS = (
    "reckon/one.py",
    "reckon/two.py",
    "tests/test_one.py",
)

DONE_WHEN = "<p><strong>Done when.</strong> The three-path check passes.</p>"
NEGATIVE_CONTROL = (
    "<p>Negative control: at the base revision the same dry run derives the "
    "default scope alone.</p>"
)
WRITE_PATHS_SENTENCE = (
    "<p>Write paths: "
    + ", ".join(f"<code>{path}</code>" for path in SECTION_WRITE_PATHS)
    + ".</p>"
)


def _plan(slug: str, sections: dict[str, str]) -> str:
    quote = '"'
    body = "".join(
        f"<h2 id={quote}{name}{quote}>§{name[1:]}</h2>"
        f"<section data-reckon={quote}section{quote} data-id={quote}{name}{quote} "
        f'data-effort-hours="1.0" data-capability-version="1.0" '
        f'data-capability-class="general" data-capability-reasoning="standard" '
        f'data-capability-tool-autonomy="guided" '
        f'data-capability-verification="strict" data-capability-risk="low" '
        f'data-status="implementable"></section>'
        f"{prose}"
        for name, prose in sections.items()
    )
    declarations = dict.fromkeys(sections, "implementable")
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


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True)


@pytest.fixture()
def checkout(tmp_path: Path, home: Path) -> Path:
    """A git checkout carrying one plan under a temporary crew home."""
    repository = tmp_path / "checkout"
    state = repository / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps({"project": PROJECT, "data": {"_version": 0}}) + "\n",
        encoding="utf-8",
    )
    plans = repository / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / f"{PLAN}.html").write_text(
        _plan(
            PLAN,
            {
                "s1": DONE_WHEN + WRITE_PATHS_SENTENCE + NEGATIVE_CONTROL,
                "s2": DONE_WHEN,
                "s3": DONE_WHEN,
            },
        ),
        encoding="utf-8",
    )
    # The sentence's paths must resolve: a present parent directory is enough
    # for the reader, so the two source files need no contents.
    for directory in ("reckon", "tests"):
        (repository / directory).mkdir(parents=True, exist_ok=True)
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "worker@example.invalid")
    _git(repository, "config", "user.name", "Worker")
    _git(repository, "add", "--", "docs", "reckon", "tests")
    _git(repository, "commit", "-q", "-m", "chore: seed checkout")
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    return repository


def _dry_run(
    repository: Path,
    section: str,
    node: str,
    monkeypatch: pytest.MonkeyPatch,
    *extra: str,
) -> dict:
    from click.testing import CliRunner

    from tests import test_dispatch_names_its_backend as backend_tests

    monkeypatch.setattr(
        crew_dispatch_commands,
        "_dispatch_resolved_flight",
        lambda *_a, **_k: json.loads(json.dumps(CONFIG)),
    )
    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "dispatch",
            "--project",
            PROJECT,
            "--plan",
            PLAN,
            "--section",
            section,
            "--node",
            node,
            "--goal",
            f"run {node}",
            "--session",
            SESSION,
            "--repo",
            str(repository),
            "--route",
            "deterministic",
            "--allow-unreviewed-plan",
            "--dry-run",
            *extra,
        ],
    )
    payload = backend_tests._payload(result)
    assert result.exit_code == 0, (result.output, payload)
    assert payload["dry_run"] is True, payload
    return payload


def _defaults(node: str) -> set[str]:
    """The default landing scope every dispatch grants a plan node."""
    return {
        f"docs/evidence/fragments/{PLAN}/{node}.html",
        f"docs/figures/{PLAN}/{node}",
    }


def _batch_entry_write_paths(repository: Path) -> list[str]:
    """What a batch entry for s1 derives, as the batch path builds it."""
    entry = {
        "project": PROJECT,
        "plan": PLAN,
        "section": "s1",
        "node": "batch-s1",
        "goal": "run the section as its batch entry does",
    }
    _project, node, _derived = dispatch_batch._entry_node(
        0, entry, plans=dispatch_batch._Plans(repository)
    )
    return list(node.write_paths)


def test_a_single_dry_run_takes_the_sections_write_paths_beside_the_defaults(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = "single-s1"
    payload = _dry_run(checkout, "s1", node, monkeypatch)
    paths = payload["node"]["write_paths"]

    # The section states the three, and the dispatch grants its own landing
    # fragment and figure directory beside them.
    assert set(SECTION_WRITE_PATHS) <= set(paths), paths
    assert _defaults(node) <= set(paths), paths
    assert set(paths) == set(SECTION_WRITE_PATHS) | _defaults(node), paths


def test_the_derived_write_paths_equal_a_batch_entrys(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = "single-s1"
    payload = _dry_run(checkout, "s1", node, monkeypatch)
    single = list(payload["node"]["write_paths"])
    batch = _batch_entry_write_paths(checkout)

    # A batch entry takes the section's sentence, and the single dispatch's
    # derivation is that same list with the default landing scope on top.
    assert set(batch) == set(SECTION_WRITE_PATHS), batch
    assert set(single) - _defaults(node) == set(batch), (single, batch)


def test_a_section_with_no_write_paths_derives_the_defaults_alone(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = "single-s2"
    payload = _dry_run(checkout, "s2", node, monkeypatch)
    paths = list(payload["node"]["write_paths"])
    assert set(paths) == _defaults(node), paths


def test_a_dispatch_naming_its_own_write_paths_keeps_exactly_those(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = "single-s3"
    own = "reckon/own.py"
    payload = _dry_run(checkout, "s3", node, monkeypatch, "--write-path", own)
    paths = list(payload["node"]["write_paths"])

    # The section states paths for s1; s3 states none, so the named path is the
    # whole of its section-derived scope and nothing is added from elsewhere.
    assert own in paths, paths
    assert set(paths) - _defaults(node) == {own}, paths
