"""The drain's plan remainder answers from the metadata index.

``runs._project_executable_remainder`` reads the project's plan inventory from
the persisted metadata index rather than walking the resource tree and parsing
every plan. The index holds one row per plan file together with the stat
identity the row was built from, so a call revalidates each row with one stat
and parses only a plan whose identity moved. Each plan row carries the two
figures a drain reads — the open-followup count and the implementable-section
count — derived once, when the row is built.

Parity: the remainder computed from the index equals the remainder computed by
reading and parsing every plan. Cost: a second call with nothing changed parses
no plan, and after one plan changes exactly that plan is parsed once.

The negative control, armed by ``RECKON_TEST_DRAIN_REREADS_EVERY_PLAN=1``,
replaces the index reader with the read-every-plan rule, so the no-plan-changed
case fails on the calls it counts.
"""

from __future__ import annotations

import html
import json
import os
import re
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import _plan_html, file_memo, metadata_index, resources
from reckon._schema import plan_executable_remainder
from reckon.crew import runs

_PROJECT = "sample"
_PLANS = 200
_ARCHIVED_PLAN = "plan-archived"
_REREAD_ENV = "RECKON_TEST_DRAIN_REREADS_EVERY_PLAN"
_SLUG_RE = re.compile(r'<meta name="plan-slug" content="([^"]+)"')


def _declarations(implementable: int) -> dict[str, str]:
    declared = {f"s{index}": "implementable" for index in range(implementable)}
    declared["sx"] = "done"
    return declared


def _plan_doc(
    slug: str, *, implementable: int, open_followups: int, declared: bool
) -> str:
    """Return one plan document carrying declared work and followups."""

    declarations = (
        '<meta name="plan-section-declarations" content="'
        + html.escape(json.dumps(_declarations(implementable)), quote=True)
        + '">'
        if declared
        else ""
    )
    followups = "".join(
        f'<article class="r-fu" data-id="{slug}-f{index}" data-status="open">'
        f'<h4 class="r-fu-title">open {index}</h4></article>'
        for index in range(open_followups)
    ) + (
        f'<article class="r-fu" data-id="{slug}-done" data-status="resolved"'
        f' data-resolved-at="2026-01-01T00:00:00+00:00">'
        f'<h4 class="r-fu-title">answered</h4></article>'
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="docs-project" content="{_PROJECT}">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="{slug}">
<meta name="plan-title" content="{slug}">
<meta name="plan-status" content="active">
{declarations}
<title>{slug}</title></head><body><main class="plan-doc">
<section data-reckon="followups" id="followups" class="r-followups">
{followups}
</section>
</main></body></html>
"""


def _implementable(index: int) -> int:
    return index % 5


def _open_followups(index: int) -> int:
    return index % 3


def _clear_caches() -> None:
    file_memo.clear()
    metadata_index.clear()


@pytest.fixture(autouse=True)
def _isolated_caches():
    """No parse memo or index survives in or out of a test."""

    _clear_caches()
    yield
    _clear_caches()


@pytest.fixture(autouse=True)
def _negative_control_when_armed(monkeypatch):
    """Arm the declared mutation: derive the figures by parsing every plan."""

    if os.environ.get(_REREAD_ENV) != "1":
        return

    def by_parsing_every_plan(docs_dir, project):
        records = []
        for resource in resources.resource_map(
            docs_dir, project, include_archived=False, ignore_invalid=True
        ).values():
            if resource.type != "plan":
                continue
            state = _plan_html.read_state(resource.path.read_text(encoding="utf-8"))
            followups = state.get("followups") or []
            records.append(
                {
                    "path": resource.path.relative_to(docs_dir).as_posix(),
                    "open_followups": sum(
                        1
                        for followup in followups
                        if str(followup.get("status") or "") != "resolved"
                    ),
                    "implementable_sections": plan_executable_remainder(state),
                }
            )
        return records

    monkeypatch.setattr(metadata_index, "plan_derivations", by_parsing_every_plan)


@pytest.fixture()
def parsed_plans(monkeypatch):
    """Count ``read_state`` calls by the plan slug in the text it was handed."""

    original = _plan_html.read_state
    counts: Counter = Counter()

    def counted(html_text):
        match = _SLUG_RE.search(html_text or "")
        counts[match.group(1) if match else "<no-slug>"] += 1
        return original(html_text)

    monkeypatch.setattr(_plan_html, "read_state", counted)
    return counts


@pytest.fixture()
def project(tmp_path, monkeypatch):
    """A 200-plan project over a temporary configuration home."""

    config_home = tmp_path / "config"
    config_home.mkdir()
    docs_dir = tmp_path / "repository" / "docs"
    plans_dir = docs_dir / "plans"
    archive_dir = plans_dir / "archive"
    archive_dir.mkdir(parents=True)
    for index in range(_PLANS):
        slug = f"plan-{index:03d}"
        (plans_dir / f"{slug}.html").write_text(
            _plan_doc(
                slug,
                implementable=_implementable(index),
                open_followups=_open_followups(index),
                # The first plan carries no declaration, so the uncovered count
                # the closure decision reads is exercised as well as the sum.
                declared=index != 0,
            )
        )
    # An archived plan carrying work: the walk keeps it out of the live
    # inventory, so a remainder that counted it would read higher than the sum.
    (archive_dir / f"{_ARCHIVED_PLAN}.html").write_text(
        _plan_doc(_ARCHIVED_PLAN, implementable=9, open_followups=1, declared=True)
    )
    mounts = config_home / "mounts.json"
    mounts.write_text(json.dumps({_PROJECT: str(docs_dir)}))
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    return SimpleNamespace(
        docs_dir=docs_dir,
        plans_dir=plans_dir,
        config_home=config_home,
    )


def _rewrite(path: Path, text: str) -> None:
    previous = path.stat().st_mtime_ns
    path.write_text(text)
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, max(stat.st_mtime_ns, previous + 1)))


def _remainder_by_parsing_every_plan(docs_dir: Path) -> tuple[int | None, int | None]:
    """Return the remainder through the resource walk and a parse per plan."""

    remainders: list[int] = []
    uncovered_plans = 0
    plan_count = 0
    for resource in resources.resource_map(
        docs_dir, _PROJECT, include_archived=False, ignore_invalid=True
    ).values():
        if resource.type != "plan":
            continue
        plan_count += 1
        state = _plan_html.read_state(resource.path.read_text(encoding="utf-8"))
        remainder = plan_executable_remainder(state)
        if remainder is None:
            uncovered_plans += 1
            continue
        remainders.append(remainder)
    if plan_count == 0:
        return None, None
    return (sum(remainders) if remainders else None), uncovered_plans


def _expected_remainder(implementable=0) -> tuple[int, int]:
    total = sum(_implementable(index) for index in range(1, _PLANS))
    return total + implementable, 1


def test_the_remainder_from_the_index_matches_a_full_parse(project):
    from_index = runs._project_executable_remainder(_PROJECT)
    from_parsing = _remainder_by_parsing_every_plan(project.docs_dir)

    assert from_index == from_parsing
    # Positive control: the sum and the uncovered count are both non-trivial, so
    # two empty results agreeing cannot pass for parity.
    assert from_index == _expected_remainder()
    assert from_index[0] > 0
    # The answer came from the temporary index, not from the real one.
    index_files = list(
        (project.config_home / "cache" / "metadata-index").glob("*.json")
    )
    assert len(index_files) == 1


def test_the_index_rows_carry_the_followup_and_section_figures(project):
    rows = metadata_index.plan_derivations(project.docs_dir, _PROJECT)

    assert len(rows) == _PLANS
    assert {row["path"] for row in rows} == {
        f"plans/plan-{index:03d}.html" for index in range(_PLANS)
    }
    # Every plan carries one resolved followup beside its open ones, so a count
    # that swept the resolved articles would read higher than the rule allows.
    assert sum(row["open_followups"] for row in rows) == sum(
        _open_followups(index) for index in range(_PLANS)
    )
    by_path = {row["path"]: row for row in rows}
    assert by_path["plans/plan-004.html"]["implementable_sections"] == 4
    assert by_path["plans/plan-000.html"]["implementable_sections"] is None


def test_a_second_call_with_nothing_changed_parses_no_plan(project, parsed_plans):
    first = runs._project_executable_remainder(_PROJECT)

    # Positive control: the instrument saw the first call parse the plans (the
    # archived one too, whose row is built and then kept out of the inventory),
    # so the empty count below distinguishes reuse from a counter that never
    # fired.
    assert set(parsed_plans) == {f"plan-{index:03d}" for index in range(_PLANS)} | {
        _ARCHIVED_PLAN
    }
    parsed_plans.clear()

    second = runs._project_executable_remainder(_PROJECT)

    assert second == first
    assert parsed_plans == {}


def test_an_unreadable_plan_reports_the_inventory_unknown(project, monkeypatch):
    target = project.plans_dir / "plan-011.html"
    original_read_text = Path.read_text
    unavailable = True

    def read(path, *args, **kwargs):
        if path == target and unavailable:
            raise OSError("inventory unavailable")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)

    # One unreadable plan makes the whole inventory unknown: a remainder that
    # simply skipped it would read as a smaller amount of work than there is.
    assert runs._project_executable_remainder(_PROJECT) == (None, None)

    # The row kept no figures, so the next call reads the file again rather than
    # answering unknown for as long as the index lives.
    unavailable = False
    assert runs._project_executable_remainder(_PROJECT) == _expected_remainder()


def test_one_changed_plan_is_the_only_one_parsed_again(project, parsed_plans):
    first = runs._project_executable_remainder(_PROJECT)
    parsed_plans.clear()
    target = project.plans_dir / "plan-007.html"

    _rewrite(
        target, _plan_doc("plan-007", implementable=4, open_followups=2, declared=True)
    )
    changed = runs._project_executable_remainder(_PROJECT)

    assert set(parsed_plans) == {"plan-007"}
    assert sum(parsed_plans.values()) == 1
    # The changed plan's own figures moved: plan-007 declared two sections and
    # now declares four.
    assert changed == _expected_remainder(implementable=4 - _implementable(7))
    rows = {
        row["path"]: row
        for row in metadata_index.plan_derivations(project.docs_dir, _PROJECT)
    }
    assert rows["plans/plan-007.html"]["open_followups"] == 2
    assert first != changed
