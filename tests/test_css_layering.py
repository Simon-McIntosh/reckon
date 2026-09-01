from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
STYLESHEETS = {
    "foundation.css": ROOT / "docs/_shared/foundation.css",
    "dashboard.css": ROOT / "docs/_shared/dashboard.css",
    "styles.css": ROOT / "docs/ui/styles.css",
    "styles-base.css": ROOT / "docs/ui/styles-base.css",
}
OWNERS = {
    "foundation.css": "token",
    "dashboard.css": "primitive",
    "styles.css": "layout",
    "styles-base.css": "view-specific",
}


def _sources() -> dict[str, str]:
    return {
        name: path.read_text(encoding="utf-8") for name, path in STYLESHEETS.items()
    }


def _selectors(source: str) -> set[str]:
    without_comments = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    selectors: set[str] = set()
    for rule in re.finditer(
        r"(?P<header>[^{}]+)\{(?P<body>[^{}]*)\}", without_comments
    ):
        header = " ".join(rule.group("header").split())
        if not header or header.startswith("@"):
            continue
        selectors.update(" ".join(item.split()) for item in header.split(","))
    return selectors


def _assert_no_custom_properties_outside_token_layer(sources: dict[str, str]) -> None:
    definitions = {
        name: sorted(
            set(
                re.findall(
                    r"(?m)(?:^|[;{])[ \t]*(--[\w-]+)[ \t]*:",
                    re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL),
                )
            )
        )
        for name, source in sources.items()
        if name != "foundation.css"
    }
    violations = {name: tokens for name, tokens in definitions.items() if tokens}
    assert violations == {}, f"custom properties outside token layer: {violations}"


def _assert_layout_and_view_selectors_are_disjoint(sources: dict[str, str]) -> None:
    duplicated = sorted(
        _selectors(sources["styles.css"]) & _selectors(sources["styles-base.css"])
    )
    assert duplicated == [], (
        f"selectors owned by both layout and view layers: {duplicated}"
    )


def test_stylesheets_declare_their_ownership() -> None:
    sources = _sources()

    for name, owner in OWNERS.items():
        assert f"Ownership: {owner} layer" in sources[name]


def test_custom_properties_have_one_token_authority() -> None:
    _assert_no_custom_properties_outside_token_layer(_sources())


def test_layout_and_view_layers_share_no_selectors() -> None:
    _assert_layout_and_view_selectors_are_disjoint(_sources())


def test_duplicate_selector_mutation_is_rejected() -> None:
    mutated = _sources()
    mutated["styles-base.css"] += "\n.r-app { color: inherit; }\n"

    with pytest.raises(AssertionError, match=r"selectors owned by both.*\.r-app"):
        _assert_layout_and_view_selectors_are_disjoint(mutated)


def test_non_token_custom_property_mutation_is_rejected() -> None:
    mutated = _sources()
    mutated["dashboard.css"] += "\n.contract-mutation { --unexpected-token: 1; }\n"

    with pytest.raises(
        AssertionError,
        match=r"custom properties outside token layer.*--unexpected-token",
    ):
        _assert_no_custom_properties_outside_token_layer(mutated)
