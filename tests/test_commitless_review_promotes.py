"""A review's commitless manifest promotes; the same prose never stands in for a commit.

Three local-lane reviews wrote a sentence into ``commits:`` —
``commits: none (review node; no repository change; ...)`` — because a review
has no repository work to commit. ``crew complete`` then read the sentence as a
citation, refused it for not resolving to an object, and the coordinator blanked
the line by hand each time.

A ``commits:`` value that opens with the declaration word ``none`` is an honest
statement that the run has no commit, and it is honoured as such for a run whose
worktree agrees with it. The mirror case is the one that matters for safety: the
same sentence must not stand in for a commit on a run whose manifest names a
path inside its own repository, because there the declaration hides work the
ledger would then say succeeded with nothing pointing at it.

The fixture runs a repository and a pointer under ``tmp_path`` and asserts
afterwards that the workstation's real crew pointer directory is untouched,
because an isolated read does not prove an isolated write.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import _plan_html, crew, ledger
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "commitless-review-fixture"
PLAN = "commitless-review-target"
RUN_IDS = (
    "r-20260928T100000000001-review-commitless",
    "r-20260928T100000000002-implement-commitless",
    "r-20260928T100000000003-review-in-repository-path",
    "r-20260928T100000000004-review-commitless-comma",
    "r-20260928T100000000005-implement-commitless-comma",
    "r-20260928T100000000006-review-comma-absence",
    "r-20260928T100000000007-review-semicolon-absence",
    "r-20260928T100000000008-review-dash-absence",
    "r-20260928T100000000009-implement-comma-absence",
    "r-20260928T100000000010-review-not-a-declaration",
    "r-20260928T100000000011-review-full-stop-absence",
    "r-20260928T100000000012-review-en-dash-absence",
    "r-20260928T100000000013-review-slash-absence",
    "r-20260928T100000000014-review-close-paren-absence",
    "r-20260928T100000000015-implement-full-stop-absence",
)

# The sentence a review writes when it has no repository change to cite, in the
# shape the failing reviews delivered.
REVIEW_COMMITS_PROSE = (
    "none (review node; no repository change; the review is the deliverable)"
)

# The same declaration with a comma inside its parenthetical, which the manifest
# parser splits into several entries.
COMMA_COMMITS_PROSE = "none (review node, no repository change)"

# A declaration word followed by a separator and prose, with no parenthetical.
# The parser empties the bare word ``none``, so a field of this shape loses its
# declaration before anything reads the parsed entries.
BARE_ABSENCE_SHAPES = (
    ("r-20260928T100000000006-review-comma-absence", "none, no repository change"),
    ("r-20260928T100000000007-review-semicolon-absence", "none; review only"),
    ("r-20260928T100000000008-review-dash-absence", "none - review only"),
)

# Sentence punctuation after the declaration word: a full stop, an en dash, a
# slash and a closing parenthesis each end the word, so the value declares an
# absence and promotes. The word must stand alone, and any character that is
# not a letter, digit or underscore ends it.
PUNCTUATION_ABSENCE_SHAPES = (
    ("r-20260928T100000000011-review-full-stop-absence", "none. review only"),
    (
        "r-20260928T100000000012-review-en-dash-absence",
        "none " + chr(0x2013) + " review only",
    ),
    ("r-20260928T100000000013-review-slash-absence", "none/review only"),
    ("r-20260928T100000000014-review-close-paren-absence", "none) review only"),
)


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write_plan(root: Path) -> Path:
    path = root / "docs" / "plans" / f"{PLAN}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    state = {
        "type": "plan",
        "slug": PLAN,
        "title": "Commitless review target",
        "status": "active",
        "version": 0,
        "comments": {},
    }
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def real_crew_home_is_not_a_fixture_target() -> None:
    """No fixture may reach the workstation's real crew pointer directory."""
    real_live = Path.home() / ".config" / "reckon" / "crew" / "live"
    real_pointers = [real_live / f"{run_id}.json" for run_id in RUN_IDS]
    assert not any(path.exists() for path in real_pointers)
    yield
    assert not any(path.exists() for path in real_pointers)


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_hook = tmp_path / "config"
    config_hook.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_hook))
    root = tmp_path / "repo"
    _write_plan(root)
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "candidate.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs", "candidate.txt"),
        ("commit", "-q", "-m", "test: seed repository"),
    ):
        _git(root, *arguments)
    (config_hook / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _manifest(
    tmp_path: Path,
    run_id: str,
    *,
    changed_paths: str,
    commits: str,
) -> Path:
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: commitless-review\n"
        "status: complete\n"
        f"commits: {commits}\n"
        f"changed_paths: {changed_paths}\n"
        "tests: focused commitless-review promotion check passed\n",
        encoding="utf-8",
    )
    return manifest


def _pointer(
    repository: Path, run_id: str, manifest: Path, *, role: str, node_id: str
) -> None:
    head = _git(repository, "rev-parse", "HEAD")
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": head,
            "launch": "in-harness",
            "role": role,
            "backend": "native",
            "created_at": "2026-09-28T10:00:00Z",
            "manifest_path": str(manifest),
            "node": {
                "id": node_id,
                "plan": PLAN,
                "section": "commitless-review",
                "time_budget": "25m",
                "write_paths": ["candidate.txt"],
            },
        },
    )


def _promotes(repository: Path, run_id: str) -> dict:
    """Promote a fixture run, or fail with the refusal text for the record."""
    try:
        return crew.complete(run_id, gate="passed", root=repository)
    except crew.CrewError as refusal:  # pragma: no cover - reported, not swallowed
        raise AssertionError(f"run {run_id!r} was refused: {refusal}") from refusal


def test_review_declaring_no_commits_promotes(repository: Path, tmp_path: Path) -> None:
    """A review's ``commits: none (...)`` is read as an empty list, not a citation."""
    run_id = RUN_IDS[0]
    delivered = tmp_path / "crew" / "reviews" / f"{run_id}.json"
    manifest = _manifest(
        tmp_path,
        run_id,
        changed_paths=str(delivered),
        commits=REVIEW_COMMITS_PROSE,
    )
    _pointer(
        repository,
        run_id,
        manifest,
        role="review",
        node_id=f"review-of-{PLAN}",
    )

    promoted = _promotes(repository, run_id)

    assert promoted["record"]["commits"] == []
    assert not pointer_path(run_id).exists()


def test_implement_declaring_no_commits_over_its_own_path_is_refused(
    repository: Path, tmp_path: Path
) -> None:
    """The same prose on a run that names an in-repository path is refused."""
    run_id = RUN_IDS[1]
    manifest = _manifest(
        tmp_path,
        run_id,
        changed_paths="candidate.txt",
        commits=REVIEW_COMMITS_PROSE,
    )
    _pointer(
        repository,
        run_id,
        manifest,
        role="implement",
        node_id=f"build-{PLAN}",
    )

    with pytest.raises(crew.CrewError, match="manifest field 'commits' is missing"):
        crew.complete(run_id, gate="passed", root=repository)

    assert pointer_path(run_id).is_file()
    assert ledger.runs(PROJECT, root=repository) == []


def test_a_non_implement_role_cannot_hide_a_change_behind_the_prose(
    repository: Path, tmp_path: Path
) -> None:
    """The declaration does not stand in for a commit over a path inside the repo.

    A review run that names an in-repository path has claimed a repository
    change, and the review role is exempt from the review gate, so nothing else
    refuses it: the prose read as a commit lets it promote with the ledger
    recording a change and no commit pointing at it. The implement case is
    refused by the review gate when its manifest declares a change, so it alone
    would not show that the prose itself is refused.
    """
    run_id = RUN_IDS[2]
    manifest = _manifest(
        tmp_path,
        run_id,
        changed_paths="candidate.txt",
        commits=REVIEW_COMMITS_PROSE,
    )
    _pointer(
        repository,
        run_id,
        manifest,
        role="review",
        node_id=f"review-of-{PLAN}",
    )

    with pytest.raises(crew.CrewError, match="manifest field 'commits' is missing"):
        crew.complete(run_id, gate="passed", root=repository)

    assert pointer_path(run_id).is_file()
    assert ledger.runs(PROJECT, root=repository) == []


def test_review_declaring_no_commits_with_a_comma_still_promotes(
    repository: Path, tmp_path: Path
) -> None:
    """A parenthetical comma does not split the declaration into citations.

    The manifest parser splits ``commits:`` on commas, so this sentence arrives
    as two entries and only the first opens with the declaration word. Read
    entry by entry the tail would be refused as an unresolvable citation, which
    is the hand edit this removes.
    """
    run_id = RUN_IDS[3]
    delivered = tmp_path / "crew" / "reviews" / f"{run_id}.json"
    manifest = _manifest(
        tmp_path,
        run_id,
        changed_paths=str(delivered),
        commits=COMMA_COMMITS_PROSE,
    )
    _pointer(
        repository,
        run_id,
        manifest,
        role="review",
        node_id=f"review-of-{PLAN}",
    )

    promoted = _promotes(repository, run_id)

    assert promoted["record"]["commits"] == []
    assert not pointer_path(run_id).exists()


def test_implement_declaring_no_commits_with_a_comma_is_refused(
    repository: Path, tmp_path: Path
) -> None:
    """A role that commits gets no whole-field reading, so the split tail is refused."""
    run_id = RUN_IDS[4]
    manifest = _manifest(
        tmp_path,
        run_id,
        changed_paths="candidate.txt",
        commits=COMMA_COMMITS_PROSE,
    )
    _pointer(
        repository,
        run_id,
        manifest,
        role="implement",
        node_id=f"build-{PLAN}",
    )

    with pytest.raises(crew.CrewError, match="does not resolve to an object"):
        crew.complete(run_id, gate="passed", root=repository)

    assert pointer_path(run_id).is_file()
    assert ledger.runs(PROJECT, root=repository) == []


@pytest.mark.parametrize(("run_id", "commits_text"), BARE_ABSENCE_SHAPES)
def test_a_bare_absence_shape_promotes_on_a_review(
    repository: Path, tmp_path: Path, run_id: str, commits_text: str
) -> None:
    """A value opening with an absence word then any separator declares absence.

    The list reader empties a field holding only ``none``, so ``none, no
    repository change`` arrives as ``['no repository change']`` — the declaration
    gone. It is read from the raw field the node wrote, so the shape promotes
    without the hand edit.
    """
    delivered = tmp_path / "crew" / "reviews" / f"{run_id}.json"
    manifest = _manifest(
        tmp_path,
        run_id,
        changed_paths=str(delivered),
        commits=commits_text,
    )
    _pointer(
        repository,
        run_id,
        manifest,
        role="review",
        node_id=f"review-of-{PLAN}",
    )

    promoted = _promotes(repository, run_id)

    assert promoted["record"]["commits"] == []
    assert not pointer_path(run_id).exists()


def test_an_implement_run_refuses_a_bare_absence_shape(
    repository: Path, tmp_path: Path
) -> None:
    """The bare declaration word is not a whole-field absence for a committing role."""
    run_id = RUN_IDS[8]
    manifest = _manifest(
        tmp_path,
        run_id,
        changed_paths="candidate.txt",
        commits="none, no repository change",
    )
    _pointer(
        repository,
        run_id,
        manifest,
        role="implement",
        node_id=f"build-{PLAN}",
    )

    with pytest.raises(crew.CrewError, match="does not resolve to an object"):
        crew.complete(run_id, gate="passed", root=repository)

    assert pointer_path(run_id).is_file()
    assert ledger.runs(PROJECT, root=repository) == []


def test_a_word_that_only_begins_with_an_absence_word_is_not_a_declaration(
    repository: Path, tmp_path: Path
) -> None:
    """A longer word is a citation attempt, not a declared absence.

    ``nonesuch`` begins with the letters of ``none``; the absence word must
    stand alone, so this is refused as the unresolvable citation it is.
    """
    run_id = RUN_IDS[9]
    delivered = tmp_path / "crew" / "reviews" / f"{run_id}.json"
    manifest = _manifest(
        tmp_path,
        run_id,
        changed_paths=str(delivered),
        commits="nonesuch, prose",
    )
    _pointer(
        repository,
        run_id,
        manifest,
        role="review",
        node_id=f"review-of-{PLAN}",
    )

    with pytest.raises(crew.CrewError, match="does not resolve to an object"):
        crew.complete(run_id, gate="passed", root=repository)

    assert pointer_path(run_id).is_file()
    assert ledger.runs(PROJECT, root=repository) == []


@pytest.mark.parametrize(("run_id", "commits_text"), PUNCTUATION_ABSENCE_SHAPES)
def test_sentence_punctuation_after_the_absence_word_promotes_on_a_review(
    repository: Path, tmp_path: Path, run_id: str, commits_text: str
) -> None:
    """Any non-word character ends the absence word, so the shape promotes.

    The declaration word must stand alone, and a letter, digit or underscore is
    what would extend it into a different word. A full stop, an en dash, a slash
    and a closing parenthesis are none of those, so each ends the word and the
    value declares the absence it opens with.
    """
    delivered = tmp_path / "crew" / "reviews" / f"{run_id}.json"
    manifest = _manifest(
        tmp_path,
        run_id,
        changed_paths=str(delivered),
        commits=commits_text,
    )
    _pointer(
        repository,
        run_id,
        manifest,
        role="review",
        node_id=f"review-of-{PLAN}",
    )

    promoted = _promotes(repository, run_id)

    assert promoted["record"]["commits"] == []
    assert not pointer_path(run_id).exists()


def test_an_implement_run_refuses_a_full_stop_after_the_absence_word(
    repository: Path, tmp_path: Path
) -> None:
    """The punctuation rule is the commitless reading; a committing role keeps citations."""
    run_id = RUN_IDS[14]
    manifest = _manifest(
        tmp_path,
        run_id,
        changed_paths="candidate.txt",
        commits="none. no repository change",
    )
    _pointer(
        repository,
        run_id,
        manifest,
        role="implement",
        node_id=f"build-{PLAN}",
    )

    with pytest.raises(crew.CrewError, match="does not resolve to an object"):
        crew.complete(run_id, gate="passed", root=repository)

    assert pointer_path(run_id).is_file()
    assert ledger.runs(PROJECT, root=repository) == []
