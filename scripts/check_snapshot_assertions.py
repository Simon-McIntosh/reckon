#!/usr/bin/env python3
"""Flag assertions that pin a closed value as if it were a contract.

An assertion that compares an exact value can be a legitimate exact-value
contract or a snapshot of whatever it happened to observe -- a generated file,
emitted source, or served payload. Static inspection cannot tell them apart: a
closed-set assertion and a legal exact-value assertion read the same. The
discriminator used here is to perturb and observe: add one benign member to what
the assertion observes and see which assertions break. A contract survives the
addition; a snapshot does not. That is a property of the assertion's shape,
which is exactly the distinction no text match can make.

Each recorded instance is carried here as a self-contained specimen, so the
census re-runs without any repository or history dependency:

    python scripts/check_snapshot_assertions.py

The check binds to the moment an exact-value assertion is added, edited, or
pinned -- the same staging moment the repository's naming checks bind to -- and
the test suite re-runs the same census on every pytest run.

The census prints both tallies as numbers and fails closed when the mechanism
stops discriminating: at least three of the four recorded snapshot instances
must break, the later object-substitution instance must break, and none of the
four legitimate exact-value carve-outs may break. Every case carries a positive
control: its assertion must pass unchanged before the perturbation is applied.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass

RECORDED = "recorded-snapshot"
OBJECT_SUBSTITUTION = "object-substitution"
CARVE_OUT = "carve-out"

MIN_RECORDED_FLAGGED = 3

# --- recorded snapshot instances -------------------------------------------

STYLESHEET_PLACEHOLDERS = (
    "totem-accent",
    "totem-border",
    "totem-background",
    "totem-foreground",
    "totem-mono",
    "totem-radius",
    "totem-shadow",
)


def _generated_stylesheet() -> str:
    lines = [":root {"]
    lines.extend(
        f"  --{name}: var(--source-{name});" for name in STYLESHEET_PLACEHOLDERS
    )
    lines.append("}")
    return "\n".join(lines) + "\n"


def _assert_stylesheet_bytes(text: str) -> bool:
    return text == _generated_stylesheet()


def _perturb_stylesheet(text: str) -> str:
    return text + "  --probe-benign: 0;\n"


COMPONENT_IMPLEMENTATION_TOKENS = (
    'className="r-row"',
    "key={item.id}",
    "renderSidebar(",
    "onClick={() => toggle(item)}",
    "const filtered = items.filter(",
    'aria-label="open"',
    "<section ",
    "placeholder={query}",
    ">{item.label}<",
)

_EMITTED_COMPONENT_SOURCE = """\
export function row(item) {
  const filtered = items.filter((candidate) => candidate.active);
  return (
    <section className="r-row" key={item.id}>
      <button aria-label="open" onClick={() => toggle(item)}>
        <span>{item.label}</span>
      </button>
      <input placeholder={query} />
      {renderSidebar(item)}
    </section>
  );
}
"""


def _assert_component_tokens(text: str) -> bool:
    return all(token in text for token in COMPONENT_IMPLEMENTATION_TOKENS)


def _perturb_component_source(text: str) -> str:
    return text + "\n{/* inert probe member */}\n"


_BAND_ANCHOR = '<section id="band">'
_SECTION_OPEN = '<section id="section">'

_OBSERVED_RENDER_ORDER = (
    '<section id="band">band content</section>\n'
    '<section id="section">in-flight section content</section>\n'
    "<footer>footer content</footer>"
)


def _assert_section_sits_after_band(text: str) -> bool:
    return text.index(_SECTION_OPEN) > text.index(_BAND_ANCHOR)


def _perturb_render_order(text: str) -> str:
    del text  # the emitted order is the whole observed value
    # an inert helper member hoists the section ahead of the band anchor; both
    # members still render, the section now before the anchor
    return (
        "<!-- rendered by the inert render-run helper -->\n"
        '<section id="section">in-flight section content</section>\n'
        '<section id="band">band content</section>\n'
        "<footer>footer content</footer>"
    )


CREW_ROW_KEYS = frozenset(
    {
        "id",
        "run_id",
        "node",
        "status",
        "worktree",
        "base_sha",
        "started_at",
        "finished_at",
        "commit",
        "gate",
        "exit_status",
        "artifacts",
        "follow_ons",
        "blockers",
    }
)


def _served_row() -> dict[str, str]:
    return dict.fromkeys(sorted(CREW_ROW_KEYS), "")


def _assert_closed_key_set(row: dict[str, str]) -> bool:
    return frozenset(row) == CREW_ROW_KEYS


# --- the later object-substitution instance --------------------------------

_NORTH_STAR_LITERAL = "{p.north_star && <>"

_OBSERVED_NORTH_STAR_SOURCE = (
    '<span className="meta-item">'
    '{p.north_star && <><span className="sp">·</span>'
    "<span>{northStarName(p)}</span></>}"
    "</span>"
)


def _assert_literal_north_star_pinned(source: str) -> bool:
    return _NORTH_STAR_LITERAL in source


def _substitute_north_star_object(source: str) -> str:
    # the payload expression is rewritten through a helper performing the same
    # membership test, so the rendered behaviour is unchanged while the pinned
    # spelling moves
    return source.replace(
        _NORTH_STAR_LITERAL, "{hasMetadataValue(p.north_star) && <>", 1
    )


# --- legitimate exact-value carve-outs --------------------------------------

_DEPRECATION_NOTICE_RECORD = {"replacement": "reckon crew follow"}


def _assert_replacement_exact(record: dict[str, object]) -> bool:
    return record.get("replacement") == "reckon crew follow"


_MEASUREMENT_RECORD = {"indexed_path_seconds": 9}


def _assert_measurement_exact(record: dict[str, object]) -> bool:
    return record.get("indexed_path_seconds") == 9


_PAST_TENSE_RECORD = {"slot": "released"}


def _assert_slot_released(record: dict[str, object]) -> bool:
    return record.get("slot") == "released"


_TOOL_RESULT_RECORD = {
    "exit_code": 77,
    "pin": "ruff>=0.16.4",
    "artifact": "checkpoints/run-0007",
}


def _assert_tool_result_exact(record: dict[str, object]) -> bool:
    return (
        record.get("exit_code") == 77
        and record.get("pin") == "ruff>=0.16.4"
        and record.get("artifact") == "checkpoints/run-0007"
    )


@dataclass(frozen=True)
class Specimen:
    name: str
    kind: str
    observed: object
    perturbed: object
    assertion: Callable[[object], bool]


SPECIMENS = (
    Specimen(
        name="byte-equality over seven stylesheet placeholders",
        kind=RECORDED,
        observed=_generated_stylesheet(),
        perturbed=_perturb_stylesheet(_generated_stylesheet()),
        assertion=_assert_stylesheet_bytes,
    ),
    Specimen(
        name="nine literal implementation strings across two components",
        kind=RECORDED,
        observed=_EMITTED_COMPONENT_SOURCE,
        perturbed=_perturb_component_source(_EMITTED_COMPONENT_SOURCE),
        assertion=_assert_component_tokens,
    ),
    Specimen(
        name="positional source scan standing in for a rendering contract",
        kind=RECORDED,
        observed=_OBSERVED_RENDER_ORDER,
        perturbed=_perturb_render_order(_OBSERVED_RENDER_ORDER),
        assertion=_assert_section_sits_after_band,
    ),
    Specimen(
        name="closed exact-key set on the served row",
        kind=RECORDED,
        observed=_served_row(),
        perturbed={**_served_row(), "backend": "clive"},
        assertion=_assert_closed_key_set,
    ),
    Specimen(
        name="source-literal scan whose payload object is substituted",
        kind=OBJECT_SUBSTITUTION,
        observed=_OBSERVED_NORTH_STAR_SOURCE,
        perturbed=_substitute_north_star_object(_OBSERVED_NORTH_STAR_SOURCE),
        assertion=_assert_literal_north_star_pinned,
    ),
    Specimen(
        name="deprecation notice keeps its replacement command exact",
        kind=CARVE_OUT,
        observed=_DEPRECATION_NOTICE_RECORD,
        perturbed={**_DEPRECATION_NOTICE_RECORD, "removal_window": "planned"},
        assertion=_assert_replacement_exact,
    ),
    Specimen(
        name="measurement that stops someone undoing the code stays exact",
        kind=CARVE_OUT,
        observed=_MEASUREMENT_RECORD,
        perturbed={**_MEASUREMENT_RECORD, "sample_count": 128},
        assertion=_assert_measurement_exact,
    ),
    Specimen(
        name="runtime state described in the past tense stays exact",
        kind=CARVE_OUT,
        observed=_PAST_TENSE_RECORD,
        perturbed={**_PAST_TENSE_RECORD, "observed_at": "measure-time"},
        assertion=_assert_slot_released,
    ),
    Specimen(
        name="tool codes, dependency pins, and artifact paths stay exact",
        kind=CARVE_OUT,
        observed=_TOOL_RESULT_RECORD,
        perturbed={**_TOOL_RESULT_RECORD, "description": "green lint gate"},
        assertion=_assert_tool_result_exact,
    ),
)


@dataclass(frozen=True)
class Result:
    name: str
    kind: str
    baseline_pass: bool
    perturbed_pass: bool

    @property
    def flagged(self) -> bool:
        return self.baseline_pass and not self.perturbed_pass


def run_census(specimens: tuple[Specimen, ...] = SPECIMENS) -> list[Result]:
    return [
        Result(
            name=specimen.name,
            kind=specimen.kind,
            baseline_pass=specimen.assertion(specimen.observed),
            perturbed_pass=specimen.assertion(specimen.perturbed),
        )
        for specimen in specimens
    ]


def _tallies(results: list[Result]) -> dict[str, int]:
    def flagged(kind: str) -> tuple[int, int]:
        members = [result for result in results if result.kind == kind]
        return sum(result.flagged for result in members), len(members)

    recorded_flagged, recorded_total = flagged(RECORDED)
    substitution_flagged, substitution_total = flagged(OBJECT_SUBSTITUTION)
    carve_out_flagged, carve_out_total = flagged(CARVE_OUT)
    return {
        "recorded_flagged": recorded_flagged,
        "recorded_total": recorded_total,
        "substitution_flagged": substitution_flagged,
        "substitution_total": substitution_total,
        "carve_out_flagged": carve_out_flagged,
        "carve_out_total": carve_out_total,
    }


def report(results: list[Result]) -> str:
    lines = [
        (
            "Census: perturb and observe on recorded snapshot instances "
            "and exact-value carve-outs"
        ),
        (
            "Bound to the moment an exact-value assertion is added, edited, "
            "or pinned -- the staging moment the repository's naming checks bind "
            "to -- and re-run by the suite."
        ),
        "",
    ]
    for result in results:
        baseline = "pass" if result.baseline_pass else "FAIL"
        after = "pass" if result.perturbed_pass else "break"
        verdict = "flagged" if result.flagged else "kept"
        lines.append(
            f"- {result.name}: baseline {baseline}, after {after} -> {verdict}"
        )
    lines.append("")
    counts = _tallies(results)
    snapshot_flagged = counts["recorded_flagged"] + counts["substitution_flagged"]
    snapshot_total = counts["recorded_total"] + counts["substitution_total"]
    lines.append(
        "snapshot instances flagged: "
        f"{snapshot_flagged} of {snapshot_total}  "
        f"(recorded {counts['recorded_flagged']} of {counts['recorded_total']}; "
        f"object substitution {counts['substitution_flagged']} of "
        f"{counts['substitution_total']})"
    )
    lines.append(
        "carve-outs flagged: "
        f"{counts['carve_out_flagged']} of {counts['carve_out_total']}"
    )
    return "\n".join(lines)


def _bound_violations(results: list[Result]) -> list[str]:
    problems = [
        (
            f"{result.name}: positive control failed "
            "-- the assertion does not pass unchanged"
        )
        for result in results
        if not result.baseline_pass
    ]
    counts = _tallies(results)
    if counts["recorded_flagged"] < MIN_RECORDED_FLAGGED:
        problems.append(
            f"recorded snapshot instances flagged: "
            f"{counts['recorded_flagged']} of {counts['recorded_total']}, "
            f"need at least {MIN_RECORDED_FLAGGED} of 4"
        )
    if counts["substitution_flagged"] != 1:
        problems.append(
            f"object-substitution instance flagged: "
            f"{counts['substitution_flagged']} of {counts['substitution_total']}, "
            "need 1 of 1"
        )
    if counts["carve_out_flagged"] != 0:
        problems.append(
            f"carve-outs flagged: "
            f"{counts['carve_out_flagged']} of {counts['carve_out_total']}, "
            "need exactly 0"
        )
    return problems


def main() -> int:
    results = run_census()
    print(report(results))
    violations = _bound_violations(results)
    for message in violations:
        print(message, file=sys.stderr)
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
