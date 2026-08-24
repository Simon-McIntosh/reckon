import re

import pytest

from reckon.tags import normalise_tag


@pytest.mark.parametrize(
    "authored",
    [
        "standard-names",
        "Standard_Names",
        " standard names ",
        "STANDARD___NAMES",
        "standard - names",
    ],
)
def test_spelling_variants_have_one_byte_identical_identity(authored: str) -> None:
    expected = "standard-names"

    assert normalise_tag(authored) == expected
    assert normalise_tag(authored).encode() == expected.encode()


@pytest.mark.parametrize(
    "authored",
    ["standard-names", "Standard_Names", " mixed  separators___here "],
)
def test_normalisation_is_idempotent(authored: str) -> None:
    identity = normalise_tag(authored)

    assert normalise_tag(identity) == identity


@pytest.mark.parametrize("authored", ["", "   ", "___", "-_-", "!?.,"])
def test_empty_identity_names_the_offending_input(authored: str) -> None:
    with pytest.raises(ValueError, match=rf"{re.escape(repr(authored))}.*empty"):
        normalise_tag(authored)


def test_colon_is_refused_as_reserved() -> None:
    authored = "machine:iter"

    with pytest.raises(ValueError, match=rf"{re.escape(repr(authored))}.*reserved"):
        normalise_tag(authored)
