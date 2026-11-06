"""The scopes read answers about a path, and its default fits its own row.

Two failures met a coordinator asking who holds one file. The view ignored the
``path`` it was given and returned the project's whole claim set, so the only
way to ask about a file answered about every file; and with enough live claims
that whole set exceeded the row :data:`reckon.mcp_budget.VIEW_BUDGETS` declares
for the view, so the call was refused over budget rather than answered.

The fixture holds more live claims than fit that row. The narrowing case is
compared against the claim set the test computes from the fixture itself, so a
claim that was wrongly kept and one that was wrongly dropped both fail; the
default case is measured against the row the table declares.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from reckon import crew, mcp
from reckon.mcp_budget import VIEW_BUDGETS, serialised_characters

PROJECT = "demo"

#: The row the view's default answer is held to, held beside the fixture so a
#: revision declaring no row is still measured against it.
BUDGET = VIEW_BUDGETS[("crew", "scopes")]

HOLDERS = 60
#: Two files per holder, plus one directory claim, so the default answer holds
#: more paths than it can return inside its row and one path is contained by a
#: directory claim rather than by an identical spelling.
PER_HOLDER = 2

DIRECTORY = "src/package"
HELD_DIRECTORY = "docs/notes"

#: One path held by enough runs that its owners alone fill the view's row,
#: which is the case that must still advance the cursor rather than answer an
#: empty page at the offset it was asked for.
DENSE_PATH = f"{DIRECTORY}/hot.py"
DENSE_HOLDERS = 80


def _held_paths(holder: int) -> list[str]:
    return [
        f"{DIRECTORY}/module_{holder:03d}_{index}.py" for index in range(PER_HOLDER)
    ]


def _snapshot(path: Path) -> tuple[str, ...]:
    if not path.is_dir():
        return ()
    return tuple(sorted(item.name for item in path.iterdir()))


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the crew config home at a temp dir and prove the real one is idle."""
    real_live = crew.live_dir()
    before = _snapshot(real_live)
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    yield config_home
    assert _snapshot(real_live) == before


def _git(repo: Path, *arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments], capture_output=True, check=False
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A real repository with one base commit and a readable plan index."""
    root = tmp_path / "repository"
    (root / DIRECTORY).mkdir(parents=True)
    (root / DIRECTORY / "placeholder.py").write_text("base\n", encoding="utf-8")
    assert _git(root, "init", "-q", "-b", "main").returncode == 0
    _git(
        root,
        "-c",
        "user.name=gate",
        "-c",
        "user.email=gate@example.invalid",
        "add",
        "-A",
    )
    done = _git(
        root,
        "-c",
        "user.name=gate",
        "-c",
        "user.email=gate@example.invalid",
        "commit",
        "-q",
        "-m",
        "base",
    )
    assert done.returncode == 0, done.stderr
    state = root / "docs" / "state" / PROJECT
    state.mkdir(parents=True)
    (state / "index.json").write_text(
        json.dumps(
            {
                "project": PROJECT,
                "doc": "index",
                "data": {"_version": 0, "projects": [{"name": PROJECT}]},
            }
        ),
        encoding="utf-8",
    )
    return root


def _write_pointer(run_id: str, *, repo: Path, paths: list[str]) -> None:
    directory = crew.live_dir()
    directory.mkdir(parents=True, exist_ok=True)
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repo.resolve()),
            "phase": "working",
            # This process, so every claim is judged live and binding without
            # the fixture depending on git or on the node's process table.
            "pid": os.getpid(),
            "node": {"id": f"node-{run_id}", "write_paths": list(paths)},
        },
    )


@pytest.fixture()
def claims(home: Path, repo: Path) -> list[dict[str, Any]]:
    """Write the live pointers and return the claim set they declare."""

    declared: list[dict[str, Any]] = []
    for holder in range(HOLDERS):
        run_id = f"r-holder-{holder:03d}"
        paths = _held_paths(holder)
        _write_pointer(run_id, repo=repo, paths=paths)
        declared.extend(
            {
                "path": path,
                "run_id": run_id,
                "node": f"node-{run_id}",
                "declared_path": path,
                "binding": True,
            }
            for path in paths
        )
    # One path is held by many runs, so its owners alone fill the view's row.
    for holder in range(DENSE_HOLDERS):
        run_id = f"r-dense-{holder:03d}"
        _write_pointer(run_id, repo=repo, paths=[DENSE_PATH])
        declared.append(
            {
                "path": DENSE_PATH,
                "run_id": run_id,
                "node": f"node-{run_id}",
                "declared_path": DENSE_PATH,
                "binding": True,
            }
        )
    # One holder declares a path outside the checkout, which a review record
    # does: it is listed by its absolute spelling, so a suggestion drawn from
    # the claim set must not offer a path the narrowing read would refuse.
    outside = str((repo.parent / "records" / "review.json").resolve())
    _write_pointer("r-outside", repo=repo, paths=[outside])
    declared.append(
        {
            "path": outside,
            "run_id": "r-outside",
            "node": "node-r-outside",
            "declared_path": outside,
            "binding": True,
        }
    )
    # One holder declares a directory, so a path beneath it is claimed by
    # containment rather than by an identical spelling.
    _write_pointer("r-directory", repo=repo, paths=[HELD_DIRECTORY])
    declared.append(
        {
            "path": HELD_DIRECTORY,
            "run_id": "r-directory",
            "node": "node-r-directory",
            "declared_path": HELD_DIRECTORY,
            "binding": True,
        }
    )
    return declared


def _contains(container: str, path: str) -> bool:
    """Whether one normalized path contains another, by path component."""

    container_parts = Path(container).parts
    path_parts = Path(path).parts
    return path_parts[: len(container_parts)] == container_parts


def _expected(declared: list[dict[str, Any]], requested: str) -> dict[str, Any]:
    """The exact claim map the fixture declares for ``requested``."""

    claim_map: dict[str, list[dict[str, Any]]] = {}
    for claim in declared:
        path = claim["path"]
        if not (_contains(path, requested) or _contains(requested, path)):
            continue
        claim_map.setdefault(path, []).append(
            {
                "run_id": claim["run_id"],
                "node": claim["node"],
                "declared_path": claim["declared_path"],
                "binding": claim["binding"],
            }
        )
    return claim_map


def _owners(claim_map: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Strip the prose from each owner so the test compares facts, not wording."""

    return {
        path: [
            {
                "run_id": owner["run_id"],
                "node": owner["node"],
                "declared_path": owner["declared_path"],
                "binding": owner["binding"],
            }
            for owner in owners
        ]
        for path, owners in claim_map.items()
    }


def _scopes(repo: Path, **arguments: Any) -> dict[str, Any]:
    return mcp._crew(PROJECT, view="scopes", checkout_path=str(repo), **arguments)


def test_a_read_naming_a_path_answers_exactly_that_path(
    claims: list[dict[str, Any]], repo: Path
) -> None:
    requested = _held_paths(17)[1]

    result = _scopes(repo, path=requested)

    expected = _expected(claims, requested)
    assert expected, "the fixture must hold a claim on the path asked about"
    assert _owners(result["claim_map"]) == expected
    for owners in result["claim_map"].values():
        for owner in owners:
            assert "binding" in owner
            assert "disposition_reason" in owner
    assert {claim["path"] for claim in result["claims"]} == set(expected)
    assert result["path"] == requested
    assert result["claim_count"] == sum(len(rows) for rows in expected.values())


def test_a_read_naming_a_path_answers_what_lies_inside_it(
    claims: list[dict[str, Any]], repo: Path
) -> None:
    """A claim on a directory and a claim beneath one answer for that path."""

    for requested in (HELD_DIRECTORY, f"{HELD_DIRECTORY}/deep/inside.md"):
        result = _scopes(repo, path=requested)

        assert _owners(result["claim_map"]) == _expected(claims, requested), requested


def test_a_read_naming_a_directory_answers_the_files_beneath_it(
    claims: list[dict[str, Any]], repo: Path
) -> None:
    result = _scopes(repo, path=DIRECTORY)

    expected = _expected(claims, DIRECTORY)
    assert len(expected) == HOLDERS * PER_HOLDER + 1  # the dense path too
    assert _owners(result["claim_map"]) == expected


def test_a_read_naming_an_absolute_path_answers_the_same_claims(
    claims: list[dict[str, Any]], repo: Path
) -> None:
    """The same file reached by either spelling answers the same claims."""

    held = _held_paths(3)[0]
    expected = _expected(claims, held)

    relative = _scopes(repo, path=held)
    absolute = _scopes(repo, path=str((repo / held).resolve()))

    # Each spelling is compared against the claim set computed from the
    # fixture, so two readings that agree with each other and with neither the
    # fixture nor the rule fail here.
    assert _owners(relative["claim_map"]) == expected
    assert _owners(absolute["claim_map"]) == expected
    assert absolute["path"] == relative["path"] == held


def test_a_path_outside_the_checkout_is_refused_naming_the_reason(
    claims: list[dict[str, Any]], repo: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "elsewhere" / "file.py"

    result = _scopes(repo, path=str(outside))

    assert result["ok"] is False
    assert result["error"] == "invalid_path"


def test_the_default_read_fits_its_row_and_names_how_to_narrow(
    claims: list[dict[str, Any]], repo: Path
) -> None:
    full = {
        "claims": claims,
        "claim_map": _expected(claims, ""),
    }
    assert serialised_characters(full) > BUDGET, (
        "the fixture must hold more claims than the row allows, or the fit is "
        "measured against nothing"
    )

    result = _scopes(repo)

    size = serialised_characters(result)
    assert size <= BUDGET, f"{size} characters over the {BUDGET} row"
    assert result.get("error") != "response_over_budget"
    assert result["summary"]["claims"] == len(claims)
    assert result["summary"]["paths"] == len({claim["path"] for claim in claims})
    assert result["summary"]["returned_claims"] < len(claims)
    assert len(result["claims"]) == result["summary"]["returned_claims"]
    suggestions = {item["argument"]: item["value"] for item in result["narrow_with"]}
    assert "path" in suggestions
    # The example has to be answerable by the call it invites: a claim on a
    # stored record outside the checkout is spelled absolutely and refused.
    narrowed = _scopes(repo, path=suggestions["path"])
    assert narrowed["ok"] is True
    assert narrowed["claim_map"]


def test_the_default_read_pages_to_every_path(
    claims: list[dict[str, Any]], repo: Path
) -> None:
    seen: set[str] = set()
    cursor: str | None = None
    for _page in range(len({claim["path"] for claim in claims}) + 1):
        result = _scopes(repo, cursor=cursor)
        assert serialised_characters(result) <= BUDGET
        seen.update(result["claim_map"])
        cursor = result["pagination"]["next_cursor"]
        if cursor is None:
            break
    assert cursor is None
    assert seen == {claim["path"] for claim in claims}


def test_a_path_whose_owners_fill_the_row_still_advances_the_cursor(
    claims: list[dict[str, Any]], repo: Path
) -> None:
    """A page never answers the cursor it was given, and the walk ends.

    One path here is held by enough runs that its owners alone exceed the row.
    A page that dropped entries until the answer fitted would come back empty
    at that offset and hand the caller the same cursor back, reading the same
    empty page forever; the walk must cover every path, naming the one it could
    not return whole.
    """

    every_path = {claim["path"] for claim in claims}
    exact = _scopes(repo, path=DENSE_PATH)
    assert len(exact["claim_map"][DENSE_PATH]) == DENSE_HOLDERS
    assert (
        serialised_characters(
            {"claim_map": exact["claim_map"], "claims": exact["claims"]}
        )
        > BUDGET
    ), "the fixture must hold one path whose owners alone fill the row"
    seen: set[str] = set()
    shortened_entry: dict[str, Any] | None = None
    shortened_owners: list[dict[str, Any]] = []
    walked: set[str | None] = {None}
    cursor: str | None = None
    for _page in range(len(every_path) + 2):
        result = _scopes(repo, cursor=cursor)
        assert serialised_characters(result) <= BUDGET
        next_cursor = result["pagination"]["next_cursor"]
        if next_cursor is not None:
            assert next_cursor != cursor, "a page returned the cursor it was given"
            assert next_cursor not in walked, "the cursor did not advance"
            walked.add(next_cursor)
        seen.update(result["claim_map"])
        for entry in result.get("over_budget_paths") or []:
            if entry["path"] == DENSE_PATH:
                shortened_entry = entry
                shortened_owners = result["claim_map"][DENSE_PATH]
        cursor = next_cursor
        if cursor is None:
            break
    assert cursor is None, "the walk did not end"
    assert seen == every_path
    # The dense path was answered, and answered in its summary form, because
    # its full record does not fit the row.
    assert shortened_entry is not None
    assert shortened_entry["owners"] == DENSE_HOLDERS
    assert shortened_entry["owners_returned"] == len(shortened_owners)
    assert shortened_owners
    assert all("disposition_reason" not in owner for owner in shortened_owners)
    assert {owner["run_id"] for owner in shortened_owners} <= {
        claim["run_id"] for claim in claims if claim["path"] == DENSE_PATH
    }
