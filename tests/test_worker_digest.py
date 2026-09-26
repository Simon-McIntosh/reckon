"""The role digests must carry a floor of rules and regenerate byte-identically.

The floor is asserted by stable markers taken verbatim from the canonical
policy, so a section-map edit that drops a required rule fails here. Each role's
own blocks are asserted by heading, so a role that loses a study-rated rule it
was required to retain fails too, and a case forbids the coordinator-only
delivery text a worker digest must never carry. The committed digests are
compared against a fresh generation from the canonical file, so a digest that
drifts from the policy is caught rather than shipped.

Generation is pure over its canonical text, so the map, the excerpts and the
parser are also exercised against a synthesised policy in ``tmp_path``; that
case runs on any host, and the canonical-file cases skip where the file is
absent.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from reckon.crew import worker_digest as wd

# The markers the declared negative controls pin: dropping the git-safety block
# from the section map must fail the rule assertion, and restoring the whole
# commit-discipline block must fail the coordinator-content assertion.
GIT_SAFETY_MARKER = dict(wd.REQUIRED_RULES)[wd.GIT_SAFETY_RULE]
COORDINATOR_MARKER = dict(wd.FORBIDDEN_CONTENT)[wd.COORDINATOR_RULE]


def assert_markers_present(role: str, digest_text: str) -> None:
    """Every required rule's marker is present, the git-safety one checked first.

    The git-safety assertion comes first so that dropping the git-safety block
    is the failure a reader sees, rather than a sibling rule that vanished with
    the same edit.
    """
    assert GIT_SAFETY_MARKER in digest_text, f"{role}: git-safety marker absent"
    missing = [
        marker
        for name, marker in wd.REQUIRED_RULES
        if name != wd.GIT_SAFETY_RULE and marker not in digest_text
    ]
    assert not missing, f"{role}: markers absent: {missing}"


def assert_no_coordinator_content(role: str, digest_text: str) -> None:
    """No coordinator-only delivery instruction is present, named if one is."""
    found = wd.forbidden_content(digest_text)
    assert not found, f"{role}: coordinator-only content present: {found}"


def _synthetic_policy() -> str:
    """A policy carrying the headers, the excerpt spans and the withheld text.

    Every required marker sits in the git-safety block, so the rule floor is
    testable without the workstation's canonical file. Each excerpted header
    carries its declared span lines and, after them, the coordinator-only
    delivery text the excerpt withholds, so the coordinator case can prove the
    default map drops it and a restored whole block restores it.
    """
    keys = {key for keys in wd.SECTION_MAP.values() for key in keys}
    whole = sorted(key for key in keys if key not in wd.EXCERPTS)
    markers = "\n".join(marker for _, marker in wd.REQUIRED_RULES)
    coordinator = "\n".join(marker for _, marker in wd.FORBIDDEN_CONTENT)
    parts: list[str] = []
    for header in whole:
        parts.append(f"{header}\n\n")
        if header == "## Git Safety":
            parts.append(f"{markers}\n\n")
    for excerpt in wd.EXCERPTS.values():
        parts.append(f"{excerpt.header}\n\n")
        for first, last in excerpt.spans:
            parts.append(f"{first}\n{last}\n\n")
        parts.append(f"{coordinator}\n\n")
    return "".join(parts)


CANONICAL = wd.canonical_path()
requires_canonical = pytest.mark.skipif(
    not CANONICAL.exists(),
    reason=f"{wd.CANONICAL_SOURCE} is not present on this host",
)


def test_every_role_in_the_map_generates_a_digest_from_a_synthetic_policy():
    policy = _synthetic_policy()

    for role in wd.ROLES:
        digest = wd.generate(role, policy)
        assert_markers_present(role, digest)
        assert wd.missing_rules(digest) == []
        assert wd.missing_headings(role, digest) == []
        assert wd.check_rules(role, digest) is None
        assert wd.check_headings(role, digest) is None


def test_every_role_keeps_the_git_safety_block():
    policy = _synthetic_policy()

    for role in wd.ROLES:
        digest = wd.generate(role, policy)
        present = {header for header, _ in wd.parse_blocks(digest)}
        assert "### Banned Commands" in present
        assert GIT_SAFETY_MARKER in digest


def test_every_role_digest_carries_its_own_required_headings():
    policy = _synthetic_policy()

    for role in wd.ROLES:
        digest = wd.generate(role, policy)
        present = {header for header, _ in wd.parse_blocks(digest)}
        for header in (*wd.FLOOR_HEADINGS, *wd.ROLE_HEADINGS[role]):
            assert header in present, f"{role}: heading absent: {header}"


def test_the_implement_role_carries_the_test_visibility_block():
    present = {
        header
        for header, _ in wd.parse_blocks(wd.generate("implement", _synthetic_policy()))
    }

    assert "## Test Visibility" in present
    assert "## Test Visibility" in wd.SECTION_MAP["implement"]


def test_the_generated_digest_is_deterministic():
    policy = _synthetic_policy()

    first = wd.generate("implement", policy)
    second = wd.generate("implement", policy)

    assert first == second


def test_unknown_blocks_are_refused_and_absent_roles_are_named():
    with pytest.raises(wd.UnknownBlockError):
        wd.generate(
            "report", "## Git Safety\n\nbody\n", section_map={"report": ("## Nope",)}
        )

    with pytest.raises(KeyError):
        wd.generate("nope", "## Git Safety\n\nbody\n")


def test_blocks_are_emitted_in_canonical_order():
    policy = _synthetic_policy()
    order = [header for header, _ in wd.parse_blocks(policy)]
    retained = wd.retained_headers(wd.SECTION_MAP["implement"], policy)
    digest = wd.generate("implement", policy)
    positions = [digest.index(header) for header in retained]

    assert positions == sorted(positions)
    assert retained == [h for h in order if h in set(retained)]


def test_the_floor_and_the_role_blocks_are_both_retained():
    policy = _synthetic_policy()
    report = wd.generate("report", policy)
    review = wd.generate("review", policy)

    assert "## User-Facing Communication" in report
    assert "## Reading a Fleet Monitor Without Being Misled" not in report
    assert "## Reading a Fleet Monitor Without Being Misled" in review
    for role in wd.ROLES:
        retained = set(wd.retained_headers(wd.SECTION_MAP[role], policy))
        assert set(wd.FLOOR_HEADINGS) <= retained
        assert set(wd.ROLE_HEADINGS[role]) <= retained


def test_the_map_withholds_the_coordinator_only_blocks():
    policy = _synthetic_policy()

    for role in wd.ROLES:
        digest = wd.generate(role, policy)
        assert_no_coordinator_content(role, digest)


def test_dropping_the_git_safety_block_from_the_map_fails_the_check():
    policy = _synthetic_policy()
    wd.check_rules("report", wd.generate("report", policy))

    mutated = {
        role: tuple(key for key in keys if key not in wd._GIT_SAFETY)
        for role, keys in wd.SECTION_MAP.items()
    }
    digest = wd.generate("report", policy, section_map=mutated)

    assert "### Banned Commands" not in digest
    assert GIT_SAFETY_MARKER not in digest
    with pytest.raises(wd.MissingRuleError, match="git-safety"):
        wd.check_rules("report", digest)
    with pytest.raises(AssertionError, match="git-safety marker absent"):
        assert_markers_present("report", digest)


def test_restoring_the_whole_commit_discipline_block_fails_the_coordinator_case():
    policy = _synthetic_policy()
    plain = wd.generate("report", policy)

    assert COORDINATOR_MARKER not in plain
    assert_no_coordinator_content("report", plain)

    mutated = {
        role: (*keys, "### Commit Discipline") for role, keys in wd.SECTION_MAP.items()
    }
    digest = wd.generate("report", policy, section_map=mutated)

    assert COORDINATOR_MARKER in digest
    with pytest.raises(wd.CoordinatorContentError, match=COORDINATOR_MARKER):
        wd.check_rules("report", digest)
    with pytest.raises(AssertionError, match=COORDINATOR_MARKER):
        assert_no_coordinator_content("report", digest)


def test_restoring_the_whole_parallel_safety_block_fails_the_coordinator_case():
    policy = _synthetic_policy()
    plain = wd.generate("report", policy)

    assert_no_coordinator_content("report", plain)

    mutated = {
        role: tuple(
            "## Parallel Agent Safety" if key == "worker-parallel-safety" else key
            for key in keys
        )
        for role, keys in wd.SECTION_MAP.items()
    }
    digest = wd.generate("report", policy, section_map=mutated)

    assert "The orchestrator owns merges" in digest
    assert "Never message a CLI-launched worker as a peer session." in digest
    with pytest.raises(wd.CoordinatorContentError, match="orchestrator owns merges"):
        wd.check_rules("report", digest)
    with pytest.raises(AssertionError, match="orchestrator owns merges"):
        assert_no_coordinator_content("report", digest)


def test_the_parallel_safety_excerpt_keeps_the_worker_duties():
    policy = _synthetic_policy()

    for role in wd.ROLES:
        digest = wd.generate(role, policy)
        assert "one exclusive write scope" in digest
        assert "never push the" in digest
        assert "Durable delivery" in digest


def _committed_digest_hashes() -> dict[str, str]:
    return {
        path.name: hashlib.sha256(
            path.read_text(encoding="utf-8").encode("utf-8")
        ).hexdigest()
        for path in sorted(wd.DIGEST_DIR.glob("*.md"))
    }


def test_regeneration_writes_only_under_the_named_directory(tmp_path: Path):
    before = _committed_digest_hashes()

    written = wd.regenerate_all(_synthetic_policy(), out_dir=tmp_path)

    assert set(written) == set(wd.ROLES)
    for role, path in written.items():
        assert path == tmp_path / f"{role}.md"
        assert path.read_text(encoding="utf-8") == wd.generate(
            role, _synthetic_policy()
        )
    assert _committed_digest_hashes() == before


def test_verify_reports_a_perturbed_digest(tmp_path: Path):
    policy = _synthetic_policy()
    wd.regenerate_all(policy, out_dir=tmp_path)
    assert wd.verify(policy, out_dir=tmp_path) == []

    (tmp_path / "report.md").write_text("tampered\n", encoding="utf-8")

    assert wd.verify(policy, out_dir=tmp_path) == ["report"]


def test_check_mode_reports_staleness(tmp_path: Path):
    policy_file = tmp_path / "policy.md"
    policy_file.write_text(_synthetic_policy(), encoding="utf-8")
    out_dir = tmp_path / "out"
    argv = ["--canonical", str(policy_file), "--out-dir", str(out_dir)]

    assert wd.main(argv) == 0
    assert wd.main([*argv, "--check"]) == 0

    (out_dir / "test.md").unlink()

    assert wd.main([*argv, "--check"]) == 1


@requires_canonical
def test_committed_digests_regenerate_byte_identically():
    canonical = CANONICAL.read_text(encoding="utf-8")

    for role in wd.ROLES:
        path = wd.digest_path(role)
        assert path.exists(), f"{path} is not committed"
        assert path.read_text(encoding="utf-8") == wd.generate(role, canonical), (
            f"{path} is stale; regenerate with python -m reckon.crew.worker_digest"
        )


@requires_canonical
def test_every_role_digest_carries_the_floor_and_its_blocks_from_the_canonical_file():
    canonical = CANONICAL.read_text(encoding="utf-8")

    for role in wd.ROLES:
        digest = wd.generate(role, canonical)
        assert_markers_present(role, digest)
        assert wd.missing_rules(digest) == []
        assert wd.missing_headings(role, digest) == []
        assert wd.check_rules(role, digest) is None
        assert wd.check_headings(role, digest) is None
        assert_no_coordinator_content(role, digest)


@requires_canonical
def test_no_role_digest_from_the_canonical_file_carries_the_dropped_blocks():
    canonical = CANONICAL.read_text(encoding="utf-8")

    for role in wd.ROLES:
        present = {
            header for header, _ in wd.parse_blocks(wd.generate(role, canonical))
        }
        dropped = [header for header in wd.DROPPED_BLOCKS if header in present]
        assert not dropped, f"{role}: withheld block retained: {dropped}"


@requires_canonical
def test_every_role_digest_stays_under_the_bound_and_below_the_source():
    canonical = CANONICAL.read_text(encoding="utf-8")
    source_tokens = wd.estimate_tokens(canonical)

    for role in wd.ROLES:
        digest = wd.generate(role, canonical)
        size = wd.estimate_tokens(digest)
        assert size < source_tokens, (
            f"{role} digest is no smaller than the canonical policy"
        )
        assert size <= wd.TOKEN_BOUND, (
            f"{role} digest is ~{size} tokens, over {wd.TOKEN_BOUND}"
        )
        assert wd.check_bound(role, digest) is None


@requires_canonical
def test_regeneration_does_not_write_the_canonical_file(tmp_path: Path):
    canonical = CANONICAL.read_text(encoding="utf-8")
    before = hashlib.sha256(CANONICAL.read_bytes()).hexdigest()

    wd.regenerate_all(canonical, out_dir=tmp_path)

    assert hashlib.sha256(CANONICAL.read_bytes()).hexdigest() == before


@requires_canonical
def test_the_committed_digests_are_current_on_this_host():
    canonical = CANONICAL.read_text(encoding="utf-8")

    assert wd.verify(canonical) == []
