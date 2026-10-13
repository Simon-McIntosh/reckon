"""The report's section reference composes the shared section-number core.

``reckon.crew.reports`` flags a plan-section reference -- a section sign
followed by a section number -- that a production source file adds in a comment
or docstring. Its finder composes ``SECTION_NUMBER_PATTERN`` rather than
spelling the number as a bare digit, so a continuation number (``5.1``,
``5-1``) reads as one reference like every other section spelling in the plan
system.
"""

import re

from reckon import _plan_html
from reckon.crew import reports


# References the bare ``§\s*\d`` finder matched: the sign, optional spacing, a
# single digit.
_SAMPLES_THE_BARE_DIGIT_MATCHED = ["§1", "§ 9", "§0", "§ 5"]

_BARE_DIGIT = re.compile(r"§\s*\d")


def test_composed_reference_reuses_the_shared_core():
    assert _plan_html.SECTION_NUMBER_PATTERN in reports._PLAN_SECTION_REFERENCE.pattern


def test_composed_reference_matches_every_sample_the_bare_digit_matched():
    for sample in _SAMPLES_THE_BARE_DIGIT_MATCHED:
        assert _BARE_DIGIT.search(sample), sample
        assert reports._PLAN_SECTION_REFERENCE.search(sample), sample


def test_composed_reference_carries_a_continuation_number():
    for sample in ["§5.1", "§5-1", "§12.3.4"]:
        assert reports._PLAN_SECTION_REFERENCE.search(sample), sample