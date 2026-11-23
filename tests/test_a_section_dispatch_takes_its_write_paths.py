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

A sentence a batch entry cannot resolve is refused there rather than derived in
a shortened form, and a single dispatch of the same section refuses with that
same reason instead of falling back to the default landing scope. A described
test the done-when does not single out, and a parent-directory token, are the
two forms the batching reader refuses, so each is exercised on both paths and
the two refusals are compared word for word.
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

#: A sentence naming a test only as "the new test", while the done-when names
#: two tests under ``tests/``. The batching reader refuses the ambiguity rather
#: than guessing which path the description stands for, and the single dispatch
#: must refuse with the same reason rather than drop the sentence.
AMBIGUOUS_DONE_WHEN = (
    "<p><strong>Done when.</strong> tests/test_one.py and tests/test_two.py "
    "both pass.</p>"
)
DESCRIBED_TEST_SENTENCE = (
    "<p>Write paths: <code>reckon/one.py</code>, and the new test described above.</p>"
)

#: A token carrying a parent-directory segment, which the batching reader
#: refuses rather than deriving it in a shortened form that resolves elsewhere.
PARENT_SEGMENT_SENTENCE = "<p>Write paths: <code>../escape.py</code>.</p>"


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
                "s4": AMBIGUOUS_DONE_WHEN + DESCRIBED_TEST_SENTENCE + NEGATIVE_CONTROL,
                "s5": DONE_WHEN + PARENT_SEGMENT_SENTENCE + NEGATIVE_CONTROL,
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


def _invoke_dry_run(
    repository: Path,
    section: str,
    node: str,
    monkeypatch: pytest.MonkeyPatch,
    *extra: str,
):
    from click.testing import CliRunner

    monkeypatch.setattr(
        crew_dispatch_commands,
        "_dispatch_resolved_flight",
        lambda *_a, **_k: json.loads(json.dumps(CONFIG)),
    )
    return CliRunner().invoke(
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


def _dry_run(
    repository: Path,
    section: str,
    node: str,
    monkeypatch: pytest.MonkeyPatch,
    *extra: str,
) -> dict:
    from tests import test_dispatch_names_its_backend as backend_tests

    result = _invoke_dry_run(repository, section, node, monkeypatch, *extra)
    payload = backend_tests._payload(result)
    assert result.exit_code == 0, (result.output, payload)
    assert payload["dry_run"] is True, payload
    return payload


def _refusal(
    repository: Path,
    section: str,
    node: str,
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    """The reason a single dry run of ``section`` refuses, with no write path."""
    from tests import test_dispatch_names_its_backend as backend_tests

    result = _invoke_dry_run(repository, section, node, monkeypatch)
    payload = backend_tests._payload(result)
    assert result.exit_code != 0, (result.output, payload)
    assert payload["ok"] is False, payload
    return str(payload["detail"])


def _defaults(node: str) -> set[str]:
    """The default landing scope every dispatch grants a plan node."""
    return {
        f"docs/evidence/fragments/{PLAN}/{node}.html",
        f"docs/figures/{PLAN}/{node}",
    }


def _batch_entry(repository: Path, section: str = "s1"):
    """The batch path's own answer for ``section``: its node, or its refusal."""
    entry = {
        "project": PROJECT,
        "plan": PLAN,
        "section": section,
        "node": f"batch-{section}",
        "goal": "run the section as its batch entry does",
    }
    return dispatch_batch._entry_node(0, entry, plans=dispatch_batch._Plans(repository))


def _batch_entry_write_paths(repository: Path, section: str = "s1") -> list[str]:
    """What a batch entry for ``section`` derives, as the batch path builds it."""
    _project, node, _derived = _batch_entry(repository, section)
    return list(node.write_paths)


def _batch_refusal(repository: Path, section: str) -> str:
    """The reason a batch entry for ``section`` refuses."""
    with pytest.raises(dispatch_batch.BatchEntryRefusedError) as raised:
        _batch_entry(repository, section)
    return str(raised.value)


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


def test_a_single_dispatch_refuses_an_ambiguous_described_test_as_the_batch_does(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    single = _refusal(checkout, "s4", "single-s4", monkeypatch)
    batch = _batch_refusal(checkout, "s4")

    # The single dispatch reports the batch entry's own reason, naming the
    # sentence, rather than deriving the default scope in place of a sentence
    # the batch refuses.
    assert single == batch, (single, batch)
    assert "the new test described above" in single, single


def test_a_single_dispatch_refuses_a_parent_segment_as_the_batch_does(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    single = _refusal(checkout, "s5", "single-s5", monkeypatch)
    batch = _batch_refusal(checkout, "s5")

    assert single == batch, (single, batch)
    assert "parent-directory segment" in single, single
    assert "../escape.py" in single, single
