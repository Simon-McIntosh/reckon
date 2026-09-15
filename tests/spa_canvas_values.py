"""Read the handoff canvas's own values and compare them against the surface.

The design handoff at ``design/reckon-spa-handoff.dc.html`` is an executable
document: it carries the reader measures, the dependency toggle's font and
padding and standalone opacity, the cone card's ground and border tokens, the
field lists each metadata row prints, and the depth column labels as literal
values. Retyping those values into a test reproduces the drift this check
exists to catch, so nothing here is a hand-written literal — every named value
is extracted from the canvas import, and every comparison target is read from
the live surface files (``docs/ui/reader.css``, ``plans.css``,
``shell-titlebar.jsx``, ``graph.jsx``).

Two failure modes are loud by construction. A canvas key that is renamed or
removed leaves a named value unfindable, so extraction raises
``CanvasValueError`` naming that value rather than silently dropping it. The
same holds for a surface value its own file no longer declares
(``ImplementationValueError``). A value the canvas declares and the surface
simply does not implement yet is not a missing pattern — it compares as a
*verdict* (mismatch) so the run reports it instead of skipping it.

Deliberate deviations are declared in one table with their reason. A surface
that deviates from the canvas must be covered by a declaration whose expected
value matches what the surface actually shows, or the run fails as an
undeclared deviation; a declaration whose expected value disagrees with the
surface's actual deviation (``declared`` equals neither) fails as stale, so a
declaration cannot outlive its cause. While the implementation still matches
the canvas, the declaration is recorded as dormant rather than failed, because
the deviation it describes is a directed change in progress, not a reverted
one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANVAS_PATH = ROOT / "design" / "reckon-spa-handoff.dc.html"

SURFACES = (
    "reader_measure_normal",
    "reader_measure_focus",
    "toggle_font_size",
    "toggle_padding",
    "toggle_standalone_opacity",
    "card_ground",
    "card_border",
    "card_border_blocked",
    "metadata_plan",
    "metadata_research",
    "metadata_evidence",
    "metadata_figure",
    "column_label_none",
    "column_label_depth",
)


class CanvasValueError(ValueError):
    """A named value the check expects could not be found in the canvas import."""


class ImplementationValueError(ValueError):
    """A named value the check expects could not be found in the surface files."""


@dataclass(frozen=True)
class DeclaredDeviation:
    """A documented, intentional deviation from the canvas, keyed to one surface."""

    declared: object
    reason: str


# The measure stays constant at the larger of the canvas's two values. Entering
# full screen must not reflow the text being read, and the canvas's own reason
# for growing the measure — that full screen buys width — is served by using the
# wider value throughout. This is the single deliberate deviation the plan
# names; every other surface compares against the canvas as drawn.
DECLARED_DEVIATIONS: dict[str, DeclaredDeviation] = {
    "reader_measure_focus": DeclaredDeviation(
        declared="820px",
        reason=(
            "Full screen must not reflow the document, so the reading measure "
            "stays at 820px instead of growing from the canvas's 760px."
        ),
    ),
    "reader_measure_normal": DeclaredDeviation(
        declared="820px",
        reason=(
            "The measure stays constant at the larger of the canvas's two "
            "values (820px) so entering full screen reflows nothing."
        ),
    ),
}


class FidelityReport:
    """A verdict per named surface, plus the counts the run states as digits."""

    def __init__(
        self,
        verdicts: dict[str, str],
        mismatch_kinds: dict[str, str],
        deviation_states: dict[str, str],
        details: dict[str, tuple[object, object]],
    ) -> None:
        self.verdicts = verdicts
        self.mismatch_kinds = mismatch_kinds
        self.deviation_states = deviation_states
        self.details = details

    @property
    def compared(self) -> int:
        return len(self.verdicts)

    @property
    def matched(self) -> int:
        return sum(v in ("match", "declared-deviation") for v in self.verdicts.values())

    @property
    def mismatched(self) -> int:
        return sum(v == "mismatch" for v in self.verdicts.values())

    @property
    def failing(self) -> bool:
        return self.mismatched > 0

    def mismatches(self) -> list[str]:
        return [s for s, v in self.verdicts.items() if v == "mismatch"]

    def summary(self) -> str:
        bits = [
            f"compared={self.compared}",
            f"matched={self.matched}",
            f"mismatched={self.mismatched}",
        ]
        dormant = [s for s, st in self.deviation_states.items() if st == "dormant"]
        if dormant:
            bits.append(f"dormant_declarations={','.join(dormant)}")
        return "fidelity " + " ".join(bits)


# ── canvas extractors ────────────────────────────────────────────────────────

_MEASURE = re.compile(
    r'readMeasure:\s*S\.reading\s*\?\s*"(?P<focus>[^"]+px)"\s*:\s*"(?P<normal>[^"]+px)"'
)
_TOGGLE = re.compile(
    r"readGraphBtnStyle:.*?: `(?P<style>display:flex[^`]+)`", re.DOTALL
)
_CONE_CARD = re.compile(r"const coneCard = p => \(\{(?P<body>.*?)\n\s*\}\);", re.DOTALL)
_CONNEC_BAD = re.compile(
    r'border:1px solid \$\{p\.status === "blocked" \? "(?P<bad>var\(--[^)]+\))" : "(?P<line>var\(--[^)]+\))"'
)
_READMETA = re.compile(
    r'readMeta = selected \? \(kind === "plan"\s*\?\s*\[(?P<plan>.*?)\]\s*:\s*kind === "research"\s*\?\s*\[(?P<research>.*?)\]\s*:\s*kind === "evidence"\s*\?\s*\[(?P<evidence>.*?)\]\s*:\s*\[(?P<figure>.*?)\]\)\s*:\s*\[\];',
    re.DOTALL,
)
_COLUMN_LABELS = re.compile(
    r'label:\s*Number\(k\) === 0 \? "(?P<none>[^"]+)" : `(?P<template>\$\{[^}]*\}[^`]*|[^`]*\$\{[^}]*\})`'
)

_META_KEY = re.compile(r'k:\s*"(\w+)"')


def _depth_template(template: str) -> str:
    # The interpolated variable name differs between the canvas and the surface;
    # only the constant wording and the presence of the interpolation are the
    # contract, so normalise the variable out before comparing.
    return re.sub(r"\$\{[^}]*\}", "${}", template)


def extract_canvas_values(source: str | None = None) -> dict[str, object]:
    """Extract every named value from the canvas import, loudly on a rename."""
    text = CANVAS_PATH.read_text(encoding="utf-8") if source is None else source

    measure = _MEASURE.search(text)
    if not measure:
        raise CanvasValueError(
            "reader_measure_normal", 'readMeasure: S.reading ? "820px" : "760px"'
        )
    toggle = _TOGGLE.search(text)
    if not toggle:
        raise CanvasValueError(
            "toggle_font_size", "readGraphBtnStyle: template literal"
        )
    cone = _CONE_CARD.search(text)
    if not cone:
        raise CanvasValueError("card_ground", "const coneCard = p => ({ ... })")
    meta = _READMETA.search(text)
    if not meta:
        raise CanvasValueError(
            "metadata_plan", 'readMeta: selected ? (kind === "plan" ? [...] : ...) : []'
        )

    return {
        "reader_measure_normal": measure.group("normal"),
        "reader_measure_focus": measure.group("focus"),
        "toggle_font_size": _extract_first(
            _TOGGLE_FONT,
            toggle.group("style"),
            "toggle_font_size",
            "font-size in readGraphBtnStyle",
        ),
        "toggle_padding": _extract_padding(toggle.group("style")),
        "toggle_standalone_opacity": _extract_first(
            _OPACITY,
            toggle.group("style"),
            "toggle_standalone_opacity",
            "opacity in readGraphBtnStyle",
        ),
        "card_ground": _extract_fallback(
            _BACKGROUND,
            cone.group("body"),
            "token",
            "card_ground",
            "background in coneCard",
        ),
        "card_border": _extract_border_tokens(cone.group("body"))["line"],
        "card_border_blocked": _extract_border_tokens(cone.group("body"))["bad"],
        "metadata_plan": _meta_keys(meta, "plan"),
        "metadata_research": _meta_keys(meta, "research"),
        "metadata_evidence": _meta_keys(meta, "evidence"),
        "metadata_figure": _meta_keys(meta, "figure"),
        "column_label_none": _extract_fallback(
            _COLUMN_LABELS, text, "none", "column_label_none", "column label"
        ),
        "column_label_depth": _depth_template(
            _extract_fallback(
                _COLUMN_LABELS, text, "template", "column_label_depth", "column label"
            )
        ),
    }


_TOGGLE_FONT = re.compile(r"font-size:([\d.]+)px")
_OPACITY = re.compile(r"opacity:([\d.]+)")
_PADDING = re.compile(r"padding:([\d.]+)px ([\d.]+)px")
_BACKGROUND = re.compile(r"background:(?P<token>var\(--[^)]+\))")
_BORDER_TOKENS = re.compile(r'"(var\(--[^)]+\))"')


def _extract_first(
    pattern: re.Pattern[str], text: str, surface: str, site: str
) -> object:
    match = pattern.search(text)
    if not match:
        raise CanvasValueError(surface, site)
    return float(match.group(1))


def _extract_padding(style: str) -> list[float]:
    match = _PADDING.search(style)
    if not match:
        raise CanvasValueError("toggle_padding", "padding in readGraphBtnStyle")
    return [float(match.group(1)), float(match.group(2))]


def _extract_border_tokens(body: str) -> dict[str, str]:
    match = _CONNEC_BAD.search(body)
    if not match:
        raise CanvasValueError("card_border", "border in coneCard")
    return {"bad": match.group("bad"), "line": match.group("line")}


def _extract_fallback(
    pattern: re.Pattern[str], text: str, group: str, surface: str, site: str
) -> str:
    match = pattern.search(text)
    if not match:
        raise CanvasValueError(surface, site)
    return match.group(group)


def _meta_keys(match: re.Match[str], branch: str) -> list[str]:
    keys = _META_KEY.findall(match.group(branch))
    if not keys:
        raise CanvasValueError(f"metadata_{branch}", "readMeta field lists")
    return keys


# ── implementation readers ───────────────────────────────────────────────────

_IMPL_MEASURE_NORMAL = re.compile(
    r"(?<!\.is-focus-mode )\.r-reading-content\s*\{[^}]*?max-width:\s*([\d.]+px)"
)
_IMPL_MEASURE_FOCUS = re.compile(
    r"\.r-reading\.is-focus-mode \.r-reading-content\s*\{[^}]*?max-width:\s*([\d.]+px)"
)
_IMPL_TOGGLE_FONT = re.compile(
    r"\.r-reading-controls\s*\{[^}]*?font-size:\s*([\d.]+)px"
)
_IMPL_TOGGLE_PADDING = re.compile(
    r"\.r-reading-controls \.r-reading-dependencies\s*\{[^}]*?padding:\s*([\d.]+)px ([\d.]+)px"
)
_IMPL_STANDALONE_OPACITY = re.compile(
    r"\.r-reading-controls \.r-reading-dependencies\.is-standalone\s*\{[^}]*?opacity:\s*([\d.]+)"
)
_IMPL_CONE_CARD = re.compile(
    r"\.r-dependency-cone-card\s*\{[^}]*?border:\s*1px solid\s*(?P<border>var\(--[^)]+\));[^}]*?background:\s*(?P<ground>var\(--[^)]+\))"
)
_IMPL_BLOCKED_BORDER = re.compile(r"^[^}]*border[^;{}]*var\(--bad\)", re.MULTILINE)
_IMPL_META = re.compile(
    r'kind === "plan"\s*\?\s*\[(?P<plan>.*?)\]\s*:\s*\[(?P<other>.*?)\];',
    re.DOTALL,
)
_IMPL_LITERAL_KEYS = re.compile(r'\["(\w+)"')
_IMPL_KEY_TERNARY = re.compile(
    r'kind === "research" \? "(\w+)" : kind === "evidence" \? "(\w+)" : "(\w+)"'
)
_IMPL_LABEL_NONE = re.compile(r'label:\s*value === 0 \? "(?P<none>[^"]+)"')
_IMPL_LABEL_TEMPLATE = re.compile(
    r'label:\s*\w+ === 0 \? "[^"]+" : `(?P<template>[^`]*\$\{[^}]*\}[^`]*)`'
)


def _impl_unique(
    pattern: re.Pattern[str], text: str, surface: str, file_name: str
) -> re.Match[str]:
    match = pattern.search(text)
    if not match:
        raise ImplementationValueError(surface, file_name)
    return match


def read_implementation_values(root: Path | None = None) -> dict[str, object]:
    """Read the surface's version of each named value from its source files."""
    base = ROOT if root is None else root
    ui = base / "docs" / "ui"
    reader_css = (ui / "reader.css").read_text(encoding="utf-8")
    plans_css = (ui / "plans.css").read_text(encoding="utf-8")
    titlebar = (ui / "shell-titlebar.jsx").read_text(encoding="utf-8")
    graph = (ui / "graph.jsx").read_text(encoding="utf-8")

    normal = _impl_unique(
        _IMPL_MEASURE_NORMAL, reader_css, "reader_measure_normal", "reader.css"
    )
    focus = _impl_unique(
        _IMPL_MEASURE_FOCUS, reader_css, "reader_measure_focus", "reader.css"
    )
    font = _impl_unique(_IMPL_TOGGLE_FONT, reader_css, "toggle_font_size", "reader.css")
    padding = _impl_unique(
        _IMPL_TOGGLE_PADDING, reader_css, "toggle_padding", "reader.css"
    )
    opacity = _impl_unique(
        _IMPL_STANDALONE_OPACITY, reader_css, "toggle_standalone_opacity", "reader.css"
    )
    cone = _impl_unique(_IMPL_CONE_CARD, plans_css, "card_border", "plans.css")
    meta = _impl_unique(_IMPL_META, titlebar, "metadata_plan", "shell-titlebar.jsx")
    label_none = _impl_unique(_IMPL_LABEL_NONE, graph, "column_label_none", "graph.jsx")
    label_template = _impl_unique(
        _IMPL_LABEL_TEMPLATE, graph, "column_label_depth", "graph.jsx"
    )
    non_plan_meta = _impl_other_meta(meta.group("other"))

    return {
        "reader_measure_normal": normal.group(1),
        "reader_measure_focus": focus.group(1),
        "toggle_font_size": float(font.group(1)),
        "toggle_padding": [float(padding.group(1)), float(padding.group(2))],
        "toggle_standalone_opacity": float(opacity.group(1)),
        "card_border": cone.group("border"),
        "card_ground": cone.group("ground"),
        "card_border_blocked": _impl_blocked_border(plans_css),
        "metadata_plan": _IMPL_LITERAL_KEYS.findall(meta.group("plan")),
        "metadata_research": non_plan_meta[0],
        "metadata_evidence": non_plan_meta[1],
        "metadata_figure": non_plan_meta[2],
        "column_label_none": label_none.group("none"),
        "column_label_depth": _depth_template(label_template.group("template")),
    }


def _impl_blocked_border(plans_css: str) -> str | None:
    # The cone-card rules run until the status rule; a blocked card that turns
    # its border var(--bad) will declare it there. None means the surface has no
    # blocked border yet, which the run reports as a mismatch rather than a pass.
    start = plans_css.find(".r-dependency-cone-card {")
    if start < 0:
        raise ImplementationValueError("card_border_blocked", "plans.css")
    end = plans_css.find(".r-dependency-cone-status", start)
    region = plans_css[start:end] if end > start else plans_css[start:]
    match = _IMPL_BLOCKED_BORDER.search(region)
    return "var(--bad)" if match else None


def _impl_other_meta(body: str) -> list[list[str]]:
    keys = _IMPL_KEY_TERNARY.search(body)
    tail = _IMPL_LITERAL_KEYS.findall(body)
    if not keys or len(tail) < 3:
        raise ImplementationValueError("metadata_research", "shell-titlebar.jsx")
    return [
        [keys.group(1), tail[0], tail[1], tail[2]],
        [keys.group(2), tail[0], tail[1], tail[2]],
        [keys.group(3), tail[0], tail[1], tail[2]],
    ]


# ── comparison ───────────────────────────────────────────────────────────────


def compare(
    canvas_values: dict[str, object], impl_values: dict[str, object]
) -> FidelityReport:
    """Assign each named surface a verdict against the canvas, deviations declared."""
    verdicts: dict[str, str] = {}
    mismatch_kinds: dict[str, str] = {}
    deviation_states: dict[str, str] = {}
    details: dict[str, tuple[object, object]] = {}

    for surface in SURFACES:
        if surface not in canvas_values:
            raise CanvasValueError(surface, "extract_canvas_values output")
        if surface not in impl_values:
            raise ImplementationValueError(surface, "read_implementation_values output")
        canvas = canvas_values[surface]
        impl = impl_values[surface]
        details[surface] = (canvas, impl)
        declaration = DECLARED_DEVIATIONS.get(surface)

        if canvas == impl:
            verdicts[surface] = "match"
        elif declaration is not None and declaration.declared == impl:
            verdicts[surface] = "declared-deviation"
        else:
            verdicts[surface] = "mismatch"
            mismatch_kinds[surface] = (
                "undeclared" if declaration is None else "declaration-stale"
            )

        if declaration is not None:
            if impl == canvas:
                deviation_states[surface] = "dormant"
            elif declaration.declared == impl:
                deviation_states[surface] = "active"
            else:
                deviation_states[surface] = "contradicted"

    return FidelityReport(verdicts, mismatch_kinds, deviation_states, details)
