"""Generate a per-role worker digest from the canonical user-space policy.

The canonical file is ``~/.agents/AGENTS.md``: it is the authority for every
worker on this workstation and is served whole to each launch. It carries
every project's rules and every role's retrospective, so a worker whose role
needs only part of it pays for all of it. This module keeps the canonical file
unchanged and derives a smaller file per role.

A digest is built from an explicit section map, never from a heuristic. Each
role names the canonical blocks it keeps, by header text, and the map is
declared here so a reader can see what a role does not receive as plainly as
what it does. Blocks are emitted in canonical order so a digest reads as the
source does and so two runs over the same source produce the same bytes.

Some canonical blocks are written for a coordinator in a shared checkout: they
instruct the reader to push, to pick a branch, or to arbitrate a peer's stash.
A worker dispatches into its own worktree, commits locally, never pushes and
never picks a branch, so those blocks are withheld, and where only part of a
block is coordinator-only the whole block is withheld and the worker-binding
lines are re-admitted from :data:`EXCERPTS` — the commit-message rules, the
banned commands, the hook policy, the worktree rule, and the worker's own
worktree and manifest duties. A digest then states the worker's delivery
authority once, not twice.

A floor of rules binds every role, because a launch without them is not safe
at any size: git safety, the commit and delivery rules (explicit-path
staging, a commit body, no AI attribution), the naming checks, test
application, the environment rules, and the worker manifest contract.
:data:`REQUIRED_RULES` pairs each with a marker taken verbatim from the
canonical file, and :func:`missing_rules` names any a digest does not carry.
:data:`FORBIDDEN_CONTENT` names the coordinator-only delivery instructions a
digest must not carry. A role adds blocks on top of that floor; it never
removes one.

The digests are generated artefacts committed under ``reckon/crew/digests/``.
They are regenerated from the canonical file, not hand-edited, and
:func:`generate` is pure so a test can assert that regeneration reproduces the
committed bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

CANONICAL_SOURCE = "~/.agents/AGENTS.md"

DIGEST_DIR = Path(__file__).resolve().parent / "digests"

TOKEN_BOUND = 15_000

BYTES_PER_TOKEN = 4

ROLES = ("report", "review", "implement", "test")

_HEADER = re.compile(r"^#{2,4} ")
_FENCE = re.compile(r"^\s*```")


class UnknownBlockError(KeyError):
    """A section-map key names a canonical header the source does not have."""


class DigestError(RuntimeError):
    """A generated digest violates a rule every role must satisfy."""


class MissingRuleError(DigestError):
    """A digest does not carry a rule its role is required to retain."""


class MissingHeadingError(DigestError):
    """A digest does not carry a block its role is required to retain."""


class CoordinatorContentError(DigestError):
    """A digest carries coordinator-only delivery text a worker must not get."""


class OverBoundError(DigestError):
    """A digest is larger than the stated token bound."""


# A sub-range of the canonical file, admitted in place of the whole block that
# contains it. ``spans`` are inclusive and matched by line prefix, so a span is
# declared by the words it opens and closes on rather than by a line number.
Span = tuple[str, str]


@dataclass(frozen=True)
class Excerpt:
    """The worker-binding part of one canonical block, by header text."""

    header: str
    note: str
    spans: tuple[Span, ...]


# Blocks whose coordinator-only delivery text is withheld from every worker
# digest. Each entry is a canonical header; ``EXCERPTS`` re-admits part of one
# of them, the rest is not retained by any role.
DROPPED_BLOCKS: tuple[str, ...] = (
    "### Branch Hygiene",
    "### Stash Recovery Protocol",
    "### Pre-Edit Protocol for Shared Files",
)

EXCERPTS: dict[str, Excerpt] = {
    "banned-git-commands": Excerpt(
        header="### Banned Commands",
        note="the ban table, without the stash-recovery advice",
        spans=(
            (
                "These commands have caused data loss in multi-agent sessions.",
                "| `git cherry-pick` |",
            ),
        ),
    ),
    "commit-runtime-rules": Excerpt(
        header="### Commit Discipline",
        note="message grammar and authorship, without push timing or branch policy",
        spans=(
            (
                "- **Conventional commits:**",
                "the first line, not of the whole message.",
            ),
            (
                "- **NEVER add AI attribution to ANY message",
                "user's; the tooling is not a co-author and never signs its own work.",
            ),
            (
                "- **No plan references in commits.**",
                "filenames in commit messages or PR titles.",
            ),
        ),
    ),
    "pre-commit-hooks": Excerpt(
        header="### Pre-Commit Hook Policy",
        note="check-only hooks and scoped fixing, without the pull-and-push example",
        spans=(
            (
                "Pre-commit hooks (if present) MUST be **check-only**",
                "multi-agent environments. Run format/fix **before** staging:",
            ),
            (
                "**Scope the fixer to the paths you are staging",
                "wrong number and reads as equal while the count moved.",
            ),
        ),
    ),
    "worktree-isolation": Excerpt(
        header="### No Stray Clones (binding)",
        note="worktree isolation, without the end-of-session push inventory",
        spans=(
            (
                "**Never `git clone` a repo into a sibling directory",
                "sibling clones.",
            ),
            (
                "- For isolation, use a **git worktree**",
                "clone. Worktrees are tracked, share the object store, and are auto-audited.",
            ),
        ),
    ),
    "worker-parallel-safety": Excerpt(
        header="## Parallel Agent Safety",
        note="the worker's own worktree and delivery duties, without the coordinator's",
        spans=(
            (
                "- One worker, one worktree, one exclusive write scope.",
                "`git show --stat` against the declared scope.",
            ),
            (
                "- Workers commit locally (conventional subject AND a body), never push the",
                "primary branch, never merge/rebase/stash, and stage explicit paths only.",
            ),
            (
                "- Durable delivery: every worker writes its manifest to an",
                "orchestrator-named file; the reply is a convenience, the file is the",
            ),
        ),
    ),
}

# Blocks shared by every role: git safety and its worker commit and delivery
# rules, the worker manifest contract, the test gate, and the environment rules.
_GIT_SAFETY = (
    "## Git Safety",
    "banned-git-commands",
    "### Anomaly Protocol",
    "commit-runtime-rules",
    "pre-commit-hooks",
    "worktree-isolation",
)
_WORKER_MANIFEST = (
    "worker-parallel-safety",
    "### A Worker That Invents Its Own Manifest Status Slips The Review Guard",
    "## A Worker's Process Ends With Its Turn",
)
_NAMING = (
    "### Naming & Comment Hygiene (binding)",
    "#### Mandatory pre-stage naming check (binding)",
)
_TEST_EXECUTION = ("## Test Execution Protocol",)
_ENVIRONMENT = ("## Development Environment (binding, all repos)",)
_CORE = _GIT_SAFETY + _NAMING + _WORKER_MANIFEST + _TEST_EXECUTION + _ENVIRONMENT

# The explicit section map. Every role keeps the shared floor and adds the
# blocks its work needs; no role subtracts from the floor.
SECTION_MAP: dict[str, tuple[str, ...]] = {
    "report": (
        *_CORE,
        "## User-Facing Communication",
    ),
    "review": (
        *_CORE,
        "## Reading a Fleet Monitor Without Being Misled",
        "## Test Visibility",
        "## Closing A Fail-Open Guard: Measure What The Suite Was Resting On",
    ),
    "implement": (
        *_CORE,
        "## Bug and Failure Ownership",
        "## Runtime Model Routing",
        "## IMAS Data Access",
        "## Compute Infrastructure (SDCC) — never heavy work on the login node (binding)",
        "### On the fleet allocation, run heavy work in place",
        "## Killing Processes On A Shared Login Node (binding)",
        "## Test Visibility",
    ),
    "test": (
        "## Test Visibility",
        "## Bug and Failure Ownership",
        *_CORE,
        "## Compute Infrastructure (SDCC) — never heavy work on the login node (binding)",
        "### On the fleet allocation, run heavy work in place",
    ),
}

# Blocks every role keeps, named here so a case can require them per role.
FLOOR_HEADINGS: tuple[str, ...] = (
    "## Git Safety",
    "### Banned Commands",
    "### Anomaly Protocol",
    "### Commit Discipline",
    "### Pre-Commit Hook Policy",
    "### No Stray Clones (binding)",
    "### Naming & Comment Hygiene (binding)",
    "#### Mandatory pre-stage naming check (binding)",
    "## Parallel Agent Safety",
    "## Test Execution Protocol",
    "## Development Environment (binding, all repos)",
)

# Blocks a role must add to the floor for its own work.
ROLE_HEADINGS: dict[str, tuple[str, ...]] = {
    "report": ("## User-Facing Communication",),
    "review": (
        "## Reading a Fleet Monitor Without Being Misled",
        "## Test Visibility",
        "## Closing A Fail-Open Guard: Measure What The Suite Was Resting On",
    ),
    "implement": (
        "## Bug and Failure Ownership",
        "## Test Visibility",
    ),
    "test": (
        "## Test Visibility",
        "## Bug and Failure Ownership",
    ),
}

# rule name -> the marker the digest must carry, verbatim from the canonical
# file. Each marker sits on one line of the canonical text, so its presence is
# string membership rather than a normalised search.
REQUIRED_RULES: tuple[tuple[str, str], ...] = (
    ("git-safety", "banned unconditionally"),
    (
        "git-stash-ban",
        "`git stash` (any form: push, pop, apply, branch, create, store)",
    ),
    ("explicit-path-staging", "stage specific paths only"),
    (
        "commit-body",
        "followed by a blank line and a BODY stating what changed and why",
    ),
    ("no-ai-attribution", "NEVER add AI attribution to ANY message"),
    ("naming-checks", "Mandatory pre-stage naming check"),
    ("naming-check-paths", "banned labels in the PATHS"),
    ("test-execution", "Select the gate from what changed"),
    ("environment", "One environment per repository"),
    ("manifest-delivery", "Durable delivery"),
    ("manifest-contract", "Write the manifest with what you have, then start the wait"),
)

# Coordinator-only delivery instructions. A worker commits locally and never
# pushes, never picks a branch, and never owns a merge, a plan edit or a
# session audit, so a digest carrying one of these contradicts the worker
# contract the manifest section states.
FORBIDDEN_CONTENT: tuple[tuple[str, str], ...] = (
    ("push-on-commit", "Always commit and push"),
    ("branch-policy", "Always commit to the project's primary branch"),
    (
        "orchestrator-owns-cleanup",
        "The orchestrator owns merges, pushes, plan/index state, and cleanup.",
    ),
    (
        "dispatch-contract-pointer",
        "The canonical dispatch contract (embed it verbatim in worker prompts)",
    ),
    ("session-audits", "Session audits: `git stash list` at start"),
    (
        "message-a-worker",
        "Never message a CLI-launched worker as a peer session.",
    ),
)

# The marker the declared negative control pins: dropping the git-safety block
# from the map must fail this assertion first.
GIT_SAFETY_RULE = "git-safety"

# The rule the coordinator-content control pins.
COORDINATOR_RULE = "push-on-commit"


def canonical_path() -> Path:
    """The canonical policy on this host, expanded from the literal source."""
    return Path(CANONICAL_SOURCE).expanduser()


def parse_blocks(text: str) -> list[tuple[str, list[str]]]:
    """Split canonical text into (header, chunk) pairs, in canonical order.

    A chunk runs from its header to the next header. Headers are two to four
    ``#`` marks; fenced code blocks are skipped, so a shell comment that
    begins with ``#`` inside an example is not mistaken for a section.
    """
    blocks: list[list[str]] = []
    in_fence = False
    for line in text.split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
        if not in_fence and _HEADER.match(line):
            blocks.append([line.rstrip("\n")])
        elif blocks:
            blocks[-1].append(line)
    return [(lines[0], lines) for lines in blocks]


def block_map(text: str) -> dict[str, str]:
    """Map each canonical header to its chunk text (last occurrence wins)."""
    return {
        header: "\n".join(lines).rstrip("\n") + "\n"
        for header, lines in parse_blocks(text)
    }


def _line_index(lines: Sequence[str], prefix: str, start: int = 1) -> int:
    """Index of the first line at or after ``start`` beginning with ``prefix``."""
    for index in range(start, len(lines)):
        if lines[index].strip().startswith(prefix):
            return index
    raise DigestError(
        f"canonical source has no line at or after {start} for {prefix!r}"
    )


def _excerpt_chunk(name: str, blocks: Mapping[str, str]) -> str:
    """The worker-binding spans of one excerpted block, with its header line."""
    excerpt = EXCERPTS[name]
    if excerpt.header not in blocks:
        raise UnknownBlockError(f"canonical source has no block {excerpt.header!r}")
    lines = blocks[excerpt.header].split("\n")
    rendered: list[str] = []
    for first, last in excerpt.spans:
        start = _line_index(lines, first)
        end = _line_index(lines, last, start=start + 1)
        rendered.append("\n".join(lines[start : end + 1]))
    return "\n".join([excerpt.header, "", *rendered]) + "\n"


def select_blocks(keys: Iterable[str], text: str) -> list[tuple[str, str]]:
    """(canonical header, rendered chunk) for each key, in canonical order.

    A key is either a canonical header, kept whole, or the name of an excerpt
    in :data:`EXCERPTS`, kept as the spans it declares. Both render under the
    canonical header they came from, so the digest's list of retained sections
    names the source block a reader can find.
    """
    parsed = parse_blocks(text)
    order = {header: index for index, (header, _) in enumerate(parsed)}
    blocks = block_map(text)
    selected: list[tuple[int, str, str, str]] = []
    for key in keys:
        if key in EXCERPTS:
            header = EXCERPTS[key].header
            if header not in blocks:
                raise UnknownBlockError(f"canonical source has no block {header!r}")
            selected.append((order[header], key, header, _excerpt_chunk(key, blocks)))
        elif key in blocks:
            selected.append((order[key], key, key, blocks[key]))
        else:
            raise UnknownBlockError(f"canonical source has no block {key!r}")
    return [(header, chunk) for _, _, header, chunk in sorted(selected)]


def retained_headers(keys: Iterable[str], text: str) -> list[str]:
    """The canonical headers ``keys`` retains, in canonical order."""
    return [header for header, _ in select_blocks(keys, text)]


def estimate_tokens(text: str) -> int:
    """Byte-derived token equivalent: four bytes per token, rounded up."""
    return (len(text.encode("utf-8")) + BYTES_PER_TOKEN - 1) // BYTES_PER_TOKEN


def missing_rules(digest_text: str) -> list[str]:
    """Rule names a digest is required to carry but does not."""
    return [name for name, marker in REQUIRED_RULES if marker not in digest_text]


def forbidden_content(digest_text: str) -> list[str]:
    """Coordinator-only markers a digest must not carry but does."""
    return [marker for _, marker in FORBIDDEN_CONTENT if marker in digest_text]


def missing_headings(role: str, digest_text: str) -> list[str]:
    """Headings the floor and ``role`` require a digest to carry but it does not."""
    present = {header for header, _ in parse_blocks(digest_text)}
    required = (*FLOOR_HEADINGS, *ROLE_HEADINGS.get(role, ()))
    return [header for header in required if header not in present]


def generate(
    role: str,
    canonical_text: str,
    *,
    section_map: Mapping[str, Sequence[str]] | None = None,
) -> str:
    """Compose one role's digest from canonical text. Pure and deterministic."""
    mapping = SECTION_MAP if section_map is None else section_map
    if role not in mapping:
        raise KeyError(f"no section map for role {role!r}")
    selected = select_blocks(mapping[role], canonical_text)
    retained = [header for header, _ in selected]
    digest_hash = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
    source_bytes = len(canonical_text.encode("utf-8"))
    source_tokens = estimate_tokens(canonical_text)
    header = [
        f"# Worker role digest: {role}",
        "#",
        f"# Generated from {CANONICAL_SOURCE} (sha256 {digest_hash},",
        f"# {source_bytes} bytes, ~{source_tokens} — the same policy holds for every",
        "# role; this digest retains the sections below and withholds the",
        "# coordinator-only delivery text.",
        "# Regenerate with: python -m reckon.crew.worker_digest",
        "# Edit the canonical file and regenerate; do not hand-edit this file.",
        "#",
        f"# Sections retained, in canonical order ({len(retained)}):",
        *[f"#   {name}" for name in retained],
        "#",
        "",
    ]
    return "\n".join(header) + "".join(chunk for _, chunk in selected)


def digest_path(role: str, out_dir: Path | None = None) -> Path:
    """The on-disk path of a role's digest."""
    return (out_dir or DIGEST_DIR) / f"{role}.md"


def check_rules(role: str, digest_text: str) -> None:
    """Refuse a digest that does not carry every rule its role must retain."""
    missing = missing_rules(digest_text)
    if missing:
        raise MissingRuleError(f"{role} digest is missing required rules: {missing}")
    forbidden = forbidden_content(digest_text)
    if forbidden:
        raise CoordinatorContentError(
            f"{role} digest carries coordinator-only content: {forbidden}"
        )


def check_headings(role: str, digest_text: str) -> None:
    """Refuse a digest that does not carry every heading its role must retain."""
    missing = missing_headings(role, digest_text)
    if missing:
        raise MissingHeadingError(
            f"{role} digest is missing required blocks: {missing}"
        )


def check_bound(role: str, digest_text: str) -> None:
    """Refuse a digest larger than the stated token bound."""
    size = estimate_tokens(digest_text)
    if size > TOKEN_BOUND:
        raise OverBoundError(
            f"{role} digest is ~{size} tokens, over the {TOKEN_BOUND} bound"
        )


def regenerate_all(
    canonical_text: str,
    *,
    out_dir: Path | None = None,
    roles: Iterable[str] = ROLES,
) -> dict[str, Path]:
    """Write every role's digest, refusing one that breaks the floor or bound."""
    written: dict[str, Path] = {}
    for role in roles:
        text = generate(role, canonical_text)
        check_rules(role, text)
        check_headings(role, text)
        check_bound(role, text)
        path = digest_path(role, out_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="")
        written[role] = path
    return written


def verify(canonical_text: str, *, out_dir: Path | None = None) -> list[str]:
    """Roles whose committed digest is not what regeneration produces now."""
    stale: list[str] = []
    for role in ROLES:
        path = digest_path(role, out_dir)
        if not path.exists() or path.read_text(encoding="utf-8") != generate(
            role, canonical_text
        ):
            stale.append(role)
    return stale


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="worker_digest", description=__doc__)
    parser.add_argument(
        "--canonical", default=None, help="override the canonical source path"
    )
    parser.add_argument(
        "--out-dir", default=None, help="override the digest output directory"
    )
    parser.add_argument(
        "--role", action="append", choices=ROLES, help="limit to one role"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report staleness without writing; exit 1 when a digest would change",
    )
    args = parser.parse_args(argv)

    source = Path(args.canonical) if args.canonical else canonical_path()
    if not source.exists():
        parser.error(f"canonical policy not found at {source}")
    canonical_text = source.read_text(encoding="utf-8")
    out_dir = Path(args.out_dir) if args.out_dir else None
    roles = tuple(args.role) if args.role else ROLES

    if args.check:
        stale = verify(canonical_text, out_dir=out_dir)
        if stale:
            print(f"stale digests: {', '.join(stale)}")
            return 1
        print(f"up to date: {', '.join(roles)}")
        return 0

    for role, path in regenerate_all(
        canonical_text, out_dir=out_dir, roles=roles
    ).items():
        size = estimate_tokens(path.read_text(encoding="utf-8"))
        print(f"{role}: {path} (~{size} tokens, bound {TOKEN_BOUND})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
