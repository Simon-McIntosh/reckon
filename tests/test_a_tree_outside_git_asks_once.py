"""A docs root outside any repository lets git look once, not once per plan.

The lifecycle audit derives each plan's age from the last commit touching a
path. For a docs root with no repository that lookup spawns ``git rev-parse
HEAD``, which fails and yields no commit, and nothing used to remember the
failure, so the read paid one failed process per plan. The HEAD lookup now
records a root git reports as outside any repository, keyed by the root's path
and directory stamp, so later age and HEAD lookups return no commit without
spawning again.

Only git's own verdict that a root is under no repository is remembered. A
timeout, a spawn error, or any other exit says nothing about the tree, and
remembering one would freeze a wrong answer into a long-lived server: a docs
directory below its repository's root has no ``.git`` at that level, so a
single storage-slow lookup would make every later age there read as untracked.

Git translates the message that verdict is read from, so the one query function
every git process goes through pins the C locale itself and the classification
matches one text wherever the server runs.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

from reckon import doccheck, serve
from reckon.doccheck import derived_plan_age
from reckon.serve import _git_head

PLANS = 12
COMMIT_TS = datetime(2026, 1, 5, 12, 0, 0, tzinfo=UTC).timestamp()
TODAY = date(2026, 6, 1)


def _plan_html(slug: str, modified: str) -> str:
    return (
        "<!doctype html><html><head>\n"
        '<meta name="docs-project" content="probe">\n'
        '<meta name="reckon-type" content="plan">\n'
        f'<meta name="plan-slug" content="{slug}">\n'
        f'<meta name="plan-title" content="Plan {slug}">\n'
        '<meta name="plan-summary" content="a short summary">\n'
        '<meta name="plan-status" content="active">\n'
        f'<meta name="plan-modified" content="{modified}">\n'
        "</head><body></body></html>\n"
    )


def _build_tree(root: Path, modified: str = "2026-01-05") -> Path:
    """Write a docs root holding ``PLANS`` plans and return its docs directory."""

    docs = root / "docs"
    (docs / "plans").mkdir(parents=True)
    for index in range(PLANS):
        plan = docs / "plans" / f"p{index:02d}.html"
        plan.write_text(_plan_html(f"p{index:02d}", modified), encoding="utf-8")
        os.utime(plan, (COMMIT_TS, COMMIT_TS))
    return docs


def _git(cwd: Path, *args: str, env: dict | None = None) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, env=env)


def _commit_docs(repo: Path, subtree: str) -> None:
    """Commit ``subtree`` at ``COMMIT_TS``, giving the plans a known date."""

    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "probe@example.invalid")
    _git(repo, "config", "user.name", "Probe")
    _git(repo, "add", subtree)
    stamp = f"{int(COMMIT_TS)} +0000"
    _git(
        repo,
        "commit",
        "-q",
        "-m",
        "add docs",
        env={**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp},
    )


def _run_audit(docs: Path) -> list:
    return doccheck.audit_lifecycle(project="probe", docs_dir=docs, now_ts=COMMIT_TS)


@contextlib.contextmanager
def _isolated_state(tmp_path: Path, monkeypatch):
    """Point every persisted cache and in-process memo at this test's tmp tree.

    The caches default to the operator's config home. A test that reads or
    writes it reports on the machine rather than on the code, so the config
    home is redirected into the temporary tree and the memos are cleared.
    """

    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    monkeypatch.setattr("reckon.serve._GIT_OUTSIDE_MEMO", {})
    monkeypatch.setattr("reckon.serve._GIT_HEAD_MEMO", {})
    monkeypatch.setattr("reckon.serve._GIT_LAST_MODIFIED_CACHE", {})
    monkeypatch.setattr("reckon.serve._GIT_CREATION_CACHE", {})
    yield


@contextlib.contextmanager
def _counting_git_spawns(monkeypatch):
    """Count every git process the read starts, whatever its call site.

    A plan's age and the HEAD lookup both reach git through the shared
    ``_run_git`` query, and each call is exactly one subprocess, so a counter
    wrapped around it counts processes without a shim on ``PATH``.
    """

    calls = {"count": 0}
    real_run_git = serve._run_git

    def run_git(*args, **kwargs):
        calls["count"] += 1
        return real_run_git(*args, **kwargs)

    monkeypatch.setattr(serve, "_run_git", run_git)
    yield lambda: calls["count"]


@contextlib.contextmanager
def _git_that_times_out_once(monkeypatch):
    """Make the next git process raise ``TimeoutExpired``, then behave normally."""

    real_run = subprocess.run
    state = {"pending": 1}

    def flaky(args, *rest, **kwargs):
        argv = list(args) if isinstance(args, (list, tuple)) else [args]
        if state["pending"] and argv and os.path.basename(str(argv[0])) == "git":
            state["pending"] -= 1
            raise subprocess.TimeoutExpired(cmd=str(argv[0]), timeout=1)
        return real_run(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "run", flaky)
    yield


def test_no_repository_root_asks_git_at_most_once(tmp_path, monkeypatch):
    """A no-repo docs root starts at most one git process for the whole audit."""

    with _isolated_state(tmp_path, monkeypatch):
        docs = _build_tree(tmp_path / "loose")
        assert not (tmp_path / "loose" / ".git").exists()

        with _counting_git_spawns(monkeypatch) as spawns:
            _run_audit(docs)
            count = spawns()

        assert count <= 1, f"the no-repository audit started {count} git processes"


def test_no_repository_root_answers_no_commit_without_spawning(tmp_path, monkeypatch):
    """A remembered outside root answers HEAD with no commit and no process."""

    with _isolated_state(tmp_path, monkeypatch):
        docs = _build_tree(tmp_path / "loose")
        repo_dir = docs.parent

        assert _git_head(repo_dir) is None
        assert serve._GIT_OUTSIDE_MEMO, "git's outside verdict must be remembered"

        with _counting_git_spawns(monkeypatch) as spawns:
            assert _git_head(repo_dir) is None
            assert _git_head(repo_dir) is None
            assert spawns() == 0, "a remembered outside root must not spawn git again"


def test_timeout_is_not_remembered_as_outside(tmp_path, monkeypatch):
    """A timed-out HEAD lookup is not an answer, so nothing is remembered."""

    with _isolated_state(tmp_path, monkeypatch):
        docs = _build_tree(tmp_path / "loose")
        repo_dir = docs.parent

        with _git_that_times_out_once(monkeypatch):
            assert _git_head(repo_dir) is None

        assert serve._GIT_OUTSIDE_MEMO == {}, "a timeout must not be remembered"

        with _counting_git_spawns(monkeypatch) as spawns:
            assert _git_head(repo_dir) is None
            assert spawns() == 1, "the root must be asked again after a timeout"


def test_outside_root_is_remembered_under_a_non_c_locale(tmp_path, monkeypatch):
    """A translated environment does not stop the verdict being remembered.

    The verdict is read from git's own stderr, which git translates when the
    environment asks for a language, so an environment naming one must not cost
    the outside answer. This holds on a host with no such catalogue installed,
    because what it exercises is the code setting the locale, not the host
    supplying one.
    """

    with _isolated_state(tmp_path, monkeypatch):
        monkeypatch.setenv("LC_ALL", "fr_FR.UTF-8")
        monkeypatch.setenv("LANGUAGE", "fr")
        monkeypatch.delenv("LANG", raising=False)
        docs = _build_tree(tmp_path / "loose")
        repo_dir = docs.parent
        assert not (repo_dir / ".git").exists()

        with _counting_git_spawns(monkeypatch) as spawns:
            assert _git_head(repo_dir) is None
            assert spawns() == 1

        assert serve._GIT_OUTSIDE_MEMO, "the outside verdict must be remembered"

        with _counting_git_spawns(monkeypatch) as spawns:
            assert _git_head(repo_dir) is None
            assert spawns() == 0, "a remembered outside root must not spawn git again"


def test_git_is_asked_in_the_c_locale(tmp_path, monkeypatch):
    """The message the verdict is read from is never a translated one.

    Git translates the text the classification matches, so a message from a
    translated catalogue would read as a different failure and the outside
    verdict would be lost. The query pins the locale on the spawn itself while
    leaving the rest of the environment alone, which is what this asserts; it
    is meaningful on a host with no catalogue installed, since it checks the
    request rather than the answer.
    """

    seen: list[dict] = []
    real_run = subprocess.run

    def capturing(args, *rest, **kwargs):
        argv = list(args) if isinstance(args, (list, tuple)) else [args]
        if argv and os.path.basename(str(argv[0])) == "git":
            seen.append(dict(kwargs.get("env") or {}))
        return real_run(args, *rest, **kwargs)

    with _isolated_state(tmp_path, monkeypatch):
        monkeypatch.setenv("LC_ALL", "fr_FR.UTF-8")
        monkeypatch.setenv("LANGUAGE", "fr")
        monkeypatch.setattr(subprocess, "run", capturing)
        docs = _build_tree(tmp_path / "loose")

        assert _git_head(docs.parent) is None

    assert seen, "the HEAD lookup must spawn git"
    assert all(env.get("LC_ALL") == "C" for env in seen), seen
    assert all(env.get("LANGUAGE") == "fr" for env in seen), (
        "the rest of the environment must reach git unchanged"
    )


def test_repository_root_ages_are_commit_derived(tmp_path, monkeypatch):
    """A docs root inside a repository still reads commit-derived ages."""

    with _isolated_state(tmp_path, monkeypatch):
        repo = tmp_path / "checkout"
        docs = _build_tree(repo)
        _commit_docs(repo, "docs")

        docs = docs.resolve()
        plan = docs / "plans" / "p00.html"
        age_days, source = derived_plan_age(
            "2026-01-05",
            fallback_path=plan,
            repo_dir=docs.parent,
            docs_dir=docs,
            today=TODAY,
        )
        committed_day = datetime.fromtimestamp(COMMIT_TS, tz=UTC).date()
        assert source == "commit"
        assert age_days == (TODAY - committed_day).days


def test_nested_docs_root_below_repository_keeps_resolving_head(tmp_path, monkeypatch):
    """A docs root below its repository's root keeps resolving HEAD after a timeout.

    The repository's ``.git`` sits one level above the directory the server is
    handed, so no HEAD identity can be read there and every lookup consults
    git. Git resolves this root to the repository's commit, and a timed-out
    lookup must not be taken for its verdict that the root is under no
    repository: remembering one would replace that commit with none for the
    life of the server.
    """

    with _isolated_state(tmp_path, monkeypatch):
        repo = tmp_path / "checkout"
        docs = _build_tree(repo / "code")
        repo_dir = docs.parent
        assert not (repo_dir / ".git").exists()
        _commit_docs(repo, "code")

        expected = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert expected
        assert _git_head(repo_dir) == expected

        with _git_that_times_out_once(monkeypatch):
            assert _git_head(repo_dir) is None

        assert serve._GIT_OUTSIDE_MEMO == {}, "a timeout must not be remembered"

        with _counting_git_spawns(monkeypatch) as spawns:
            assert _git_head(repo_dir) == expected
            assert spawns() == 1, (
                "the root must be asked again rather than read as outside"
            )
