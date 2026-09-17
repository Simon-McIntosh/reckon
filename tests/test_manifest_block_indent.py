"""A block scalar ends where the next field begins, at any manifest indent.

A manifest is commonly presented as a fenced or indented Markdown block, so
every one of its lines carries leading whitespace. The block-continuation test
used to ask only whether a line was indented or at column zero, which meant a
field following a block scalar was folded into that scalar's value whenever the
manifest itself was indented: the field's own leading whitespace read as the
scalar's body. The field that loses its value this way is ``commits``, which is
exactly the field a promotion resolves a run by, so the manifest silently
reports that the node committed nothing.

The rule pinned here: a single-line ``|`` (or ``>``) value opens a block, the
block continues over blank lines and over any line indented past the column the
manifest's own fields sit at, and it ends at a line at or left of that column.
"""

from __future__ import annotations

import pytest

from reckon.crew import reports


def _manifest(body: str) -> dict[str, object]:
    return reports.parse_manifest(body)


def test_a_wholly_indented_manifest_ends_its_block_at_the_next_field() -> None:
    fields = _manifest(
        "  node: node-a\n"
        "  status: complete\n"
        "  tests: |\n"
        "    seven passed\n"
        "  commits:\n"
        "    - abc1234\n"
    )

    assert fields["tests"] == "seven passed"
    assert fields["commits"] == ["abc1234"]
    assert fields["node"] == "node-a"
    assert fields["status"] == "complete"


def test_the_unindented_form_is_unchanged() -> None:
    fields = _manifest(
        "node: node-a\n"
        "status: complete\n"
        "tests: |\n"
        "  seven passed\n"
        "commits:\n"
        "  - abc1234\n"
    )

    assert fields["tests"] == "seven passed"
    assert fields["commits"] == ["abc1234"]


def test_a_four_space_indent_ends_its_block_at_the_next_field() -> None:
    fields = _manifest(
        "    node: node-a\n"
        "    status: complete\n"
        "    tests: |\n"
        "        seven passed\n"
        "    commits:\n"
        "        - abc1234\n"
    )

    assert fields["tests"] == "seven passed"
    assert fields["commits"] == ["abc1234"]


def test_a_tab_indented_manifest_ends_its_block_at_the_next_field() -> None:
    fields = _manifest(
        "\tnode: node-a\n"
        "\tstatus: complete\n"
        "\ttests: |\n"
        "\t\tseven passed\n"
        "\tcommits:\n"
        "\t\t- abc1234\n"
    )

    assert fields["tests"] == "seven passed"
    assert fields["commits"] == ["abc1234"]


def test_a_block_scalar_as_the_final_field_keeps_all_of_its_lines() -> None:
    fields = _manifest(
        "node: node-a\nstatus: complete\ntests: |\n  seven passed\n  eight passed\n"
    )

    assert fields["tests"] == "seven passed\neight passed"


def test_a_block_body_left_of_the_field_column_ends_the_block() -> None:
    # The body line sits left of the column the fields occupy, so it ends the
    # block rather than continuing it, and the fields after the block are still
    # read. The orphaned line belongs to no field and is dropped, which is how
    # a body line left of the column was already treated.
    fields = _manifest(
        "    node: node-a\n"
        "    status: complete\n"
        "    tests: |\n"
        "  seven passed\n"
        "    commits:\n"
        "        - abc1234\n"
        "    changed_paths:\n"
        "        - a.py\n"
    )

    assert fields["commits"] == ["abc1234"]
    assert fields["changed_paths"] == ["a.py"]


@pytest.mark.parametrize("indent", ["", "  ", "    "])
def test_a_multi_line_block_scalar_keeps_every_line_of_its_body(indent: str) -> None:
    fields = _manifest(
        f"{indent}node: node-a\n"
        f"{indent}status: complete\n"
        f"{indent}tests: |\n"
        f"{indent}  first line\n"
        f"{indent}  second line\n"
        f"{indent}commits:\n"
        f"{indent}  - abc1234\n"
    )

    assert fields["tests"] == "first line\nsecond line"
    assert fields["commits"] == ["abc1234"]
