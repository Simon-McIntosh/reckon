"""Canonical identities for resource tags."""

from __future__ import annotations

import re


_SEPARATORS = re.compile(r"[\s_-]+")


def normalise_tag(authored: str) -> str:
    """Derive the canonical stored identity for an authored tag.

    Tag identities are case-insensitive and use a single hyphen between words.
    Other punctuation carries no identity.  A colon is rejected separately so
    it remains available as a future facet separator.
    """

    if not isinstance(authored, str):
        raise TypeError(f"tag must be a string, got {authored!r}")
    if ":" in authored:
        raise ValueError(f"tag {authored!r} contains ':', which is reserved")

    words = _SEPARATORS.split(authored.casefold().strip())
    identity = "-".join(
        cleaned
        for word in words
        if (cleaned := "".join(character for character in word if character.isalnum()))
    )
    if not identity:
        raise ValueError(f"tag {authored!r} normalises to an empty identity")
    return identity
