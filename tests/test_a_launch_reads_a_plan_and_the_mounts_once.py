"""A launch reads each plan's headings and the mount table once, not per check.

A profile of the admit service on 14 November put a fifth of each launch in
re-parsing the headings of every plan a request's waits name, and a tenth in
resolving every mount point to decide whether a path is node-local. Both
answers depend only on their inputs, so an admission pass launching many
requests repeated the same work for each of them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import _plan_html
from reckon.crew import dispatch_admission

PLAN = """<html><body><main class="plan-doc">
<h2 id="s1">§1 — One</h2><p>a</p>
<h2 id="s2">§2 — Two</h2><p>b</p>
</main></body></html>"""


def test_one_plan_text_is_parsed_once(monkeypatch: pytest.MonkeyPatch):
    cached = getattr(_plan_html, "_plan_headings_of", None)
    if cached is not None:
        cached.cache_clear()
    calls: list[int] = []
    real = _plan_html._structural_spans

    def counting(text):
        calls.append(1)
        return real(text)

    monkeypatch.setattr(_plan_html, "_structural_spans", counting)
    first = _plan_html.authored_section_headings(PLAN)
    for _ in range(4):
        assert _plan_html.authored_section_headings(PLAN) == first
    assert [identity for identity, _ in first] == ["s1", "s2"]
    assert len(calls) == 1
    _plan_html.authored_section_headings(PLAN.replace("Two", "Deux"))
    assert len(calls) == 2


@pytest.fixture
def mountinfo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[int]:
    reads: list[int] = []
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    table = (
        f"22 1 0:21 / / rw - ext4 /dev/root rw\n"
        f"40 22 0:40 / {scratch} rw - tmpfs tmpfs rw\n"
    )
    real_read = Path.read_text

    def read_text(self, *args, **kwargs):
        if str(self) == "/proc/self/mountinfo":
            reads.append(1)
            return table
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(dispatch_admission, "_MOUNT_TABLE", {}, raising=False)
    return reads


def test_the_mount_table_is_read_once_and_answers_as_before(mountinfo, tmp_path):
    scratch = tmp_path / "scratch" / "work"
    shared = tmp_path / "shared"
    for _ in range(5):
        assert dispatch_admission._path_is_tmpfs(scratch) is True
        assert dispatch_admission._path_is_tmpfs(shared) is False
    assert len(mountinfo) == 1


def test_the_mount_table_is_read_again_after_its_minute(
    mountinfo, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    clock = [1000.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    dispatch_admission._path_is_tmpfs(tmp_path)
    clock[0] += getattr(dispatch_admission, "_MOUNT_TABLE_SECONDS", 60.0) + 1
    dispatch_admission._path_is_tmpfs(tmp_path)
    assert len(mountinfo) == 2
