"""A run directory's stream files are listed again only when it changes.

Every producer pass finds every live run's newest stream by listing its run
directory. Every stream file sits directly in that directory, so its mtime
moves when one is created, removed or renamed; until then the listing is kept.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from reckon.crew import recovery_wait


@pytest.fixture()
def listed(monkeypatch) -> list[str]:
    monkeypatch.setattr(recovery_wait, "_RUN_STREAM_NAMES", {}, raising=False)
    calls: list[str] = []
    real = Path.glob

    def counting(self, pattern, *args, **kwargs):
        calls.append(pattern)
        return real(self, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "glob", counting)
    return calls


def test_an_unchanged_directory_is_listed_once(tmp_path, listed):
    (tmp_path / "stream.jsonl").write_text("{}\n")

    first = recovery_wait.stream_paths_newest_first(tmp_path)
    after_first = len(listed)
    for _ in range(5):
        again = recovery_wait.stream_paths_newest_first(tmp_path)

    assert after_first > 0
    assert len(listed) == after_first, listed
    assert again == first == [tmp_path / "stream.jsonl"]


def test_a_new_resume_is_found(tmp_path, listed):
    (tmp_path / "stream.jsonl").write_text("{}\n")
    recovery_wait.stream_paths_newest_first(tmp_path)
    before = os.stat(tmp_path).st_mtime_ns

    resume = tmp_path / "resume-1.jsonl"
    resume.write_text("{}\n")
    later = os.stat(resume).st_mtime_ns + 1_000_000_000
    os.utime(resume, ns=(later, later))
    os.utime(tmp_path, ns=(before + 1_000_000_000, before + 1_000_000_000))

    assert recovery_wait.stream_paths_newest_first(tmp_path)[0] == resume
