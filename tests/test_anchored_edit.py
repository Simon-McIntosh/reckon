"""The line-anchored edit tool: anchors, atomicity, confinement, and stdio."""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tomllib
from datetime import timedelta
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from reckon.anchored_edit import DEFAULT_LIMIT, AnchoredEditor, _line_hash

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def root(tmp_path):
    directory = tmp_path / "root"
    directory.mkdir()
    return directory


@pytest.fixture
def editor(root):
    return AnchoredEditor([root])


def _anchors(editor, path):
    """Map 1-based line number to its anchor, as the tool reports it."""
    return {
        int(line.split(":")[0]): line.split(":")[1].split("|")[0]
        for line in editor.read(str(path)).split("\n")
        if ":" in line and "|" in line
    }


def _anchor(editor, path, number):
    return f"{number}:{_anchors(editor, path)[number]}"


def test_read_renders_each_line_with_its_anchor(editor, root):
    target = root / "sample.py"
    target.write_bytes(b"alpha\nbeta\ngamma\n")

    rendered = editor.read(str(target))

    assert rendered.split("\n")[0] == f"1:{_anchors(editor, target)[1]}|alpha"
    assert [line.split("|")[1] for line in rendered.split("\n")] == [
        "alpha",
        "beta",
        "gamma",
    ]


def test_read_reports_how_many_lines_remain(editor, root):
    target = root / "long.txt"
    target.write_bytes(b"".join(f"line {n}\n".encode() for n in range(1, 21)))

    first = editor.read(str(target), limit=5)

    assert "15 more lines" in first
    assert "offset=6" in first
    assert "line 5" in editor.read(str(target), offset=5, limit=1)


def test_read_offset_out_of_range_is_a_refusal(editor, root):
    target = root / "small.txt"
    target.write_bytes(b"only\n")

    assert editor.read(str(target), offset=2).startswith("refused:")


def test_replace_by_anchor(editor, root):
    target = root / "replace.py"
    target.write_bytes(b"one\ntwo\nthree\n")
    anchor = _anchor(editor, target, 2)

    result = editor.edit(
        str(target), [{"anchor": anchor, "op": "replace", "text": "TWO"}]
    )

    assert not result.startswith("refused:"), result
    assert target.read_bytes() == b"one\nTWO\nthree\n"


@pytest.mark.parametrize(
    ("op", "expected"),
    [
        ("insert_before", b"zero\none\ntwo\n"),
        ("insert_after", b"one\ntwo\nzero\n"),
    ],
)
def test_inserts_by_anchor(editor, root, op, expected):
    target = root / "insert.py"
    target.write_bytes(b"one\ntwo\n")
    anchor = _anchor(editor, target, 1 if op == "insert_before" else 2)

    result = editor.edit(str(target), [{"anchor": anchor, "op": op, "text": "zero"}])

    assert not result.startswith("refused:"), result
    assert target.read_bytes() == expected


@pytest.mark.parametrize(
    ("op", "expected"),
    [
        ("replace", b"one\nTWO\nzero\nthree\n"),
        ("delete", b"one\nzero\nthree\n"),
    ],
)
def test_an_insert_after_a_changed_line_survives_the_change(editor, root, op, expected):
    target = root / f"after_{op}.py"
    target.write_bytes(b"one\ntwo\nthree\n")
    anchor = _anchor(editor, target, 2)

    result = editor.edit(
        str(target),
        [
            {"anchor": anchor, "op": op, "text": "TWO"},
            {"anchor": anchor, "op": "insert_after", "text": "zero"},
        ],
    )

    assert not result.startswith("refused:"), result
    assert target.read_bytes() == expected


def test_delete_by_anchor(editor, root):
    target = root / "delete.py"
    target.write_bytes(b"one\ntwo\nthree\n")
    anchor = _anchor(editor, target, 2)

    result = editor.edit(str(target), [{"anchor": anchor, "op": "delete"}])

    assert not result.startswith("refused:"), result
    assert target.read_bytes() == b"one\nthree\n"


def test_unique_bare_hash_is_accepted(editor, root):
    target = root / "bare.py"
    target.write_bytes(b"alpha\nbeta\ngamma\n")
    hashes = [_line_hash(content) for content in (b"alpha", b"beta", b"gamma")]
    assert len(set(hashes)) == 3, "the fixture must give each line a distinct hash"
    assert hashes.count(hashes[1]) == 1, "beta's hash must be unique for this test"

    result = editor.edit(
        str(target), [{"anchor": hashes[1], "op": "replace", "text": "BETA"}]
    )

    assert not result.startswith("refused:"), result
    assert target.read_bytes() == b"alpha\nBETA\ngamma\n"


def test_ambiguous_bare_hash_is_refused(editor, root):
    target = root / "dupes.py"
    target.write_bytes(b"same\nother\nsame\n")
    before = target.read_bytes()

    result = editor.edit(
        str(target),
        [{"anchor": _line_hash(b"same"), "op": "replace", "text": "changed"}],
    )

    assert result.startswith("refused:"), result
    assert "ambiguous" in result
    assert "1, 3" in result
    assert target.read_bytes() == before


def test_stale_anchor_refuses_the_whole_call(editor, root):
    target = root / "stale.py"
    target.write_bytes(b"one\ntwo\nthree\n")
    fresh = _anchor(editor, target, 1)
    stale = _anchor(editor, target, 2)
    target.write_bytes(b"one\nTWO\nthree\n")
    before = target.read_bytes()

    result = editor.edit(
        str(target),
        [
            {"anchor": fresh, "op": "replace", "text": "ONE"},
            {"anchor": stale, "op": "replace", "text": "TWO"},
        ],
    )

    assert result.startswith("refused:"), result
    assert "stale anchor" in result
    assert "nothing was written" in result
    # The refusal carries the current lines, so the retry needs no re-read.
    assert "Current anchors around line 2:" in result
    assert f"2:{_line_hash(b'TWO')}|TWO" in result
    assert target.read_bytes() == before


def test_overlapping_edits_are_refused(editor, root):
    target = root / "overlap.py"
    target.write_bytes(b"one\ntwo\nthree\n")
    anchor = _anchor(editor, target, 2)
    before = target.read_bytes()

    result = editor.edit(
        str(target), [{"anchor": anchor, "op": "replace", "text": "a"}] * 2
    )

    assert result.startswith("refused:"), result
    assert "overlapping" in result
    assert target.read_bytes() == before


def test_crlf_file_keeps_its_endings_outside_the_edit(editor, root):
    target = root / "crlf.py"
    target.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    anchor = _anchor(editor, target, 2)

    result = editor.edit(
        str(target), [{"anchor": anchor, "op": "replace", "text": "TWO"}]
    )

    assert not result.startswith("refused:"), result
    assert target.read_bytes() == b"one\r\nTWO\r\nthree\r\n"


def test_file_without_a_final_newline_keeps_that_property(editor, root):
    target = root / "nonewline.py"
    target.write_bytes(b"one\ntwo\nthree")
    anchor = _anchor(editor, target, 2)

    result = editor.edit(
        str(target), [{"anchor": anchor, "op": "replace", "text": "TWO"}]
    )

    assert not result.startswith("refused:"), result
    assert target.read_bytes() == b"one\nTWO\nthree"


def test_successful_edit_returns_anchors_for_the_change(editor, root):
    target = root / "retry.py"
    target.write_bytes(b"one\ntwo\nthree\n")
    first = _anchor(editor, target, 2)

    result = editor.edit(
        str(target), [{"anchor": first, "op": "replace", "text": "two!"}]
    )

    reported = [
        line
        for line in result.split("\n")
        if line.startswith("2:") and line.endswith("|two!")
    ]
    assert len(reported) == 1, result
    # The anchor the edit returned addresses the line without a further read.
    second = reported[0].split("|")[0]
    again = editor.edit(str(target), [{"anchor": second, "op": "replace", "text": "2"}])
    assert not again.startswith("refused:"), again
    assert target.read_bytes() == b"one\n2\nthree\n"


def test_mode_is_preserved(editor, root):
    target = root / "mode.py"
    target.write_bytes(b"one\ntwo\n")
    os.chmod(
        target,
        stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH,
    )
    expected = stat.S_IMODE(target.stat().st_mode)

    editor.edit(
        str(target),
        [{"anchor": _anchor(editor, target, 1), "op": "replace", "text": "ONE"}],
    )

    assert stat.S_IMODE(target.stat().st_mode) == expected


def test_malformed_anchor_is_refused_not_raised(editor, root):
    target = root / "malformed.py"
    target.write_bytes(b"one\ntwo\n")
    before = target.read_bytes()

    for anchor in ("nonsense", "12", "2:zzz", "x:abc", "99:abc"):
        result = editor.edit(str(target), [{"anchor": anchor, "op": "delete"}])
        assert result.startswith("refused:"), (anchor, result)
        assert "N:HHH" in result, (anchor, result)
        assert target.read_bytes() == before


def test_no_root_refuses_every_path(tmp_path):
    unrooted = AnchoredEditor()
    target = tmp_path / "unrooted.py"
    target.write_bytes(b"one\n")
    anchor = f"1:{_line_hash(b'one')}"

    assert unrooted.read(str(target)).startswith("refused:")
    assert unrooted.edit(str(target), [{"anchor": anchor, "op": "delete"}]).startswith(
        "refused:"
    )
    assert target.read_bytes() == b"one\n"


def test_path_outside_every_root_is_refused(tmp_path, editor):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "secret.py"
    target.write_bytes(b"one\ntwo\n")
    anchor = f"2:{_line_hash(b'two')}"

    assert editor.read(str(target)).startswith("refused:")
    result = editor.edit(str(target), [{"anchor": anchor, "op": "delete"}])

    assert result.startswith("refused:"), result
    assert "outside every --root" in result
    assert target.read_bytes() == b"one\ntwo\n"


def test_symlink_escape_is_refused_and_the_target_untouched(tmp_path, editor):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.py"
    secret.write_bytes(b"one\ntwo\n")
    escape = editor._roots[0] / "escape.py"
    escape.symlink_to(secret)
    before = secret.read_bytes()
    anchor = f"2:{_line_hash(b'two')}"

    assert editor.read(str(escape)).startswith("refused:")
    result = editor.edit(str(escape), [{"anchor": anchor, "op": "delete"}])

    assert result.startswith("refused:"), result
    assert "outside every --root" in result
    assert secret.read_bytes() == before


def test_the_console_script_is_declared():
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert declared["project"]["scripts"]["reckon-anchored-edit"] == (
        "reckon.anchored_edit:main"
    )


def _server_command(root):
    script = shutil.which("reckon-anchored-edit")
    if script:
        return [script, "--root", str(root)]
    return [sys.executable, "-m", "reckon.anchored_edit", "--root", str(root)]


async def _round_trip(root, target):
    command = _server_command(root)
    params = StdioServerParameters(
        command=command[0],
        args=command[1:],
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    async with (
        stdio_client(params) as (read, write),
        ClientSession(
            read, write, read_timeout_seconds=timedelta(seconds=120)
        ) as session,
    ):
        await session.initialize()
        tools = {tool.name for tool in (await session.list_tools()).tools}
        assert {"read_anchored", "edit_anchored"} <= tools

        rendered = await session.call_tool("read_anchored", {"path": str(target)})
        text = "".join(
            part.text for part in rendered.content if getattr(part, "text", None)
        )
        anchor = next(
            line.split("|")[0] for line in text.split("\n") if line.endswith("|two")
        )
        edited = await session.call_tool(
            "edit_anchored",
            {
                "path": str(target),
                "edits": [{"anchor": anchor, "op": "replace", "text": "TWO"}],
            },
        )
        answer = "".join(
            part.text for part in edited.content if getattr(part, "text", None)
        )
        return text, answer


def test_console_script_completes_a_read_and_an_edit_over_stdio(root, monkeypatch):
    target = root / "stdio.py"
    target.write_bytes(b"one\ntwo\nthree\n")

    read_text, edit_text = anyio.run(_round_trip, root, target)

    assert "|one" in read_text and "|two" in read_text
    assert not edit_text.startswith("refused:"), edit_text
    assert "edit" in edit_text
    assert target.read_bytes() == b"one\nTWO\nthree\n"
    assert DEFAULT_LIMIT >= 200
