"""Persisted readers on different formats keep their own cache files."""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import compliance, metadata_index, serve


@pytest.mark.parametrize(
    "kind", ["metadata", "creation", "last_modified", "compliance"]
)
def test_alternating_writers_reuse_their_own_format(
    kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    repo = tmp_path / "repository"
    docs = repo / "docs"
    docs.mkdir(parents=True)
    (repo / ".git").mkdir()
    project = "sample"
    key = (str(repo.resolve()), "docs")

    if kind == "metadata":
        owner, version_name = metadata_index, "_VERSION"

        def path():
            return metadata_index._index_path(docs, project)

        def read():
            return metadata_index._load_persisted(docs, project)

        def write(version):
            metadata_index._store_persisted(
                docs, project, [{"path": f"writer-{version}.html"}]
            )

        def marker(value, version):
            return f"writer-{version}.html" in value

    elif kind == "compliance":
        owner, version_name = compliance, "_VERSION"

        def path():
            return compliance._store_path(docs, project)

        def read():
            return compliance.stored_verdicts(docs, project)

        def write(version):
            compliance._write_entries(
                docs, project, {f"writer-{version}.html": {"check": {}}}
            )

        def marker(value, version):
            return f"writer-{version}.html" in value

    else:
        owner = serve
        if kind == "creation":
            version_name = "_GIT_CREATION_SCHEMA_VERSION"
            path_fn = serve._git_creation_cache_path
            read_fn = serve._load_git_creation_cache
            write_fn = serve._store_git_creation_cache
        else:
            version_name = "_GIT_LAST_MODIFIED_SCHEMA_VERSION"
            path_fn = serve._git_last_modified_cache_path
            read_fn = serve._load_git_last_modified_cache
            write_fn = serve._store_git_last_modified_cache

        def path():
            return path_fn(key)

        def read():
            return read_fn(key)

        def write(version):
            write_fn(key, serve._GitCreationEntry("stable", {"writer.html": version}))

        def marker(value, version):
            return value.times["writer.html"] == version

    rebuilds = {1: 0, 2: 0}
    paths: dict[int, Path] = {}
    for _ in range(3):
        for version in (1, 2):
            monkeypatch.setattr(owner, version_name, version)
            cached = read()
            if not cached:
                rebuilds[version] += 1
                write(version)
            else:
                assert marker(cached, version)
            paths[version] = path()
            assert paths[version].is_file()

    assert paths[1] != paths[2]
    assert rebuilds == {1: 1, 2: 1}
    assert "Ignoring incompatible" not in caplog.text
