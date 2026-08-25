import re
from pathlib import Path

import pytest


FOUNDATION_CSS = Path(__file__).parents[1] / "docs" / "_shared" / "foundation.css"
MINIMUM_TEXT_CONTRAST = 4.5


def _theme_tokens(css: str, selector: str) -> dict[str, str]:
    match = re.search(rf"{re.escape(selector)}\s*\{{(?P<body>.*?)\}}", css, re.DOTALL)
    assert match is not None, f"missing theme selector {selector}"
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})\s*;", match["body"]))


def _relative_luminance(color: str) -> float:
    channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        channel / 12.92
        if channel <= 0.04045
        else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast_ratio(foreground: str, background: str) -> float:
    lighter, darker = sorted(
        (_relative_luminance(foreground), _relative_luminance(background)),
        reverse=True,
    )
    return (lighter + 0.05) / (darker + 0.05)


@pytest.mark.parametrize(
    ("theme", "selector"),
    [("light", ":root"), ("dark", '[data-theme="dark"]')],
)
@pytest.mark.parametrize("token", ["faint", "muted"])
def test_small_metadata_tokens_meet_text_contrast(theme: str, selector: str, token: str):
    tokens = _theme_tokens(FOUNDATION_CSS.read_text(), selector)
    ratio = _contrast_ratio(tokens[token], tokens["bg"])

    assert ratio >= MINIMUM_TEXT_CONTRAST, (
        f"{theme} --{token} {tokens[token]} on --bg {tokens['bg']} has "
        f"{ratio:.2f}:1 contrast; expected at least {MINIMUM_TEXT_CONTRAST}:1"
    )
