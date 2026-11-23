"""Queue a coordinator's chosen sections as one batch.

A coordinator decides which work needs doing, often a few sections from each of
several plans, and writes one entry per chosen section. This module turns that
list into queued requests: it reads each named plan once, fills every field an
entry omits from the section itself, and stores each entry on its own so a
refused entry never stops the rest.

The plan dispatcher enumerates a plan's declared work for its coordinator and
hands the eligible entries to the same batch queue. The per-plan read — the
section's done-when, its write paths, its declared waits — is shared by the
entries, and an entry may wait on another entry of the batch by its node id.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from reckon.crew.node import (
    NEGATIVE_CONTROL_NONE,
    CrewError,
    TaskNode,
    negative_control_writes_a_check,
)

#: The fields a batch entry may carry. Anything else is refused by name rather
#: than silently dropped, because a misspelt field would otherwise store a
#: request that does not say what its author asked for.
BATCH_ENTRY_FIELDS = frozenset(
    {
        "project",
        "plan",
        "section",
        "node",
        "goal",
        "after",
        "waits_only_after",
        "role",
        "spec_level",
        "done_when",
        "write_paths",
        "regenerate_paths",
        "anchor_paths",
        "negative_control",
        "mcp_set",
        "time_budget",
        "over_fence_reason",
        "comment",
        "local",
        "priority",
        "estimated_hours",
        "manifest",
        "brief",
        "peers",
        "requires_decisions",
        "images",
        "inputs",
        "compares_with",
        "session",
        "backend",
        "allow_unreviewed_plan",
    }
)

#: One path token as a section's write-paths sentence names it. A token is a
#: path when it names a directory (`src/payload.py`) or carries a source-file
#: extension (`.py`, `.md`). Prose abbreviations such as ``e.g`` and version
#: tags such as ``v1.2`` carry neither, so a sentence that names a file only by
#: description ("the new test") yields no path for it. A leading ``./`` or
#: ``../`` is taken as the prefix it is — never as a bare slash — and a leading
#: ``/`` not preceded by a dot keeps an absolute path absolute: a sentence
#: naming a file outside the repository must derive that file's whole path,
#: since a slash dropped turns it into a relative path that resolves nowhere.
#: The prefix is matched whole so a parent segment stays visible in the token,
#: where ``write_paths`` can refuse it rather than derive a shortened form.
_PATH_TOKEN = re.compile(r"(?:\.{1,2}/)*/?[A-Za-z0-9_][A-Za-z0-9_./-]*")
_SOURCE_EXTENSIONS = frozenset(
    {
        ".py",
        ".pyi",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".json",
        ".html",
        ".css",
        ".md",
        ".rst",
        ".toml",
        ".ini",
        ".cfg",
        ".yaml",
        ".yml",
        ".sh",
        ".sql",
        ".txt",
        ".lock",
        ".go",
        ".rs",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".hpp",
        ".f90",
        ".cmake",
        ".j2",
        ".svg",
    }
)

#: The write-paths sentence ends at its first full stop. A page's tags are
#: stripped to text with a space in their place, so a sentence closing on a
#: ``<code>`` span reads ``one.py .`` — the stop is found whether or not a
#: space precedes it, while the stops inside a path (``one.py``) never end it.
_SENTENCE_END = re.compile(r"\.(?=\s|$)")

#: A file the write-paths sentence names only by description, such as "the new
#: test" or "its regression test". A description carries no path token, so the
#: sentence alone cannot say where the file goes; the phrase is matched whole,
#: with its determiner, because a refusal quotes it back to the entry's author.
_DESCRIBED_FILE = re.compile(
    r"\b(?:the|its|their|a|an)\s+(?:[A-Za-z0-9_-]+\s+){0,2}tests?\b",
    re.IGNORECASE,
)

#: Phases a request reports as waiting for its upstream rather than queued.
WAITING_UPSTREAM_PHASES = frozenset({"awaiting-upstream", "blocked-upstream"})

#: Phases a live pointer carries when its run delivered nothing.
_UNDELIVERED_PHASES = frozenset(
    {"failed", "stopped", "interrupted", "discarded", "dropped"}
)

#: A promoted row carries no phase, because a promotion is a landing, so a run
#: whose own check failed is read from the gate the row does carry. The
#: classification says what the failure measured: a failure recorded as
#: pre-existing or as a negative result is work that landed with the check
#: reading red for a reason outside the node, so the section is delivered; any
#: other classification, and an unclassified failure, names work the run did
#: not do, so the section is still to queue.
_LANDED_DESPITE_FAILED_GATE = frozenset({"pre-existing-failure", "negative-result"})


class BatchEntryRefusedError(CrewError):
    """One entry cannot be queued, and the rest of the batch still can."""


def read_batch_entries(path: str | Path) -> list[dict[str, Any]]:
    """Read a batch file: a JSON list of entries, or an object carrying one."""
    text = Path(path).read_text(encoding="utf-8")
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise CrewError(f"batch file {path} is not JSON: {exc}") from exc
    if isinstance(payload, Mapping):
        payload = payload.get("entries")
    if not isinstance(payload, list) or not payload:
        raise CrewError(f"batch file {path} carries no entries list")
    entries: list[dict[str, Any]] = []
    for index, entry in enumerate(payload):
        if not isinstance(entry, Mapping):
            raise CrewError(f"batch entry {index + 1} is not an object")
        entries.append(dict(entry))
    return entries


@contextlib.contextmanager
def _parsed_once() -> Iterator[None]:
    """Parse each distinct plan text once for the whole batch.

    Queuing a request records the plan's review contract, which reads every
    section's prose; a derivation reads it too. Left alone, a six-entry batch
    over two plans parses each plan once per entry. The batch holds one cache
    for its duration, so every reader inside it — the batch's derivation and
    the queue-time contract alike — buys each parse once, however many sections
    of that plan are chosen.

    The authored section headings are memoised on the same boundary and on the
    document text itself. Queue ordering's heading walk, every wait edge that
    names a section of the same plan, and the section-todo derivation all
    resolve the reader through its module, so they share one parse for each
    distinct plan text rather than paying for one each. A caller that binds the
    reader by name at import is outside this boundary and parses its own.
    """
    from reckon import _plan_html

    original = _plan_html.section_prose
    cache: dict[tuple[str, tuple[tuple[str, Any], ...]], list[Any]] = {}

    def cached(document, **kwargs):
        key = (document, tuple(sorted(kwargs.items())))
        if key not in cache:
            cache[key] = list(original(document, **kwargs))
        return iter(cache[key])

    original_headings = _plan_html.authored_section_headings
    headings: dict[str, list[Any]] = {}

    def counted_headings(html_text: str):
        if html_text not in headings:
            headings[html_text] = original_headings(html_text)
        return headings[html_text]

    _plan_html.section_prose = cached
    _plan_html.authored_section_headings = counted_headings
    try:
        yield
    finally:
        _plan_html.section_prose = original
        _plan_html.authored_section_headings = original_headings


@contextlib.contextmanager
def _built_once(
    cache: dict[str, dict[str, Any]] | None = None,
) -> Iterator[None]:
    """Derive each project's roadmap at most once for the whole batch.

    Two readers inside a batch ask for a project's roadmap: queue ordering
    ranks each stored request against it, and the wait check reads a section's
    own verdict from it. Left alone, a six-entry batch over one project derives
    that roadmap six times over — once per stored request and again per section
    whose waits are checked — which is the cost the batch exists to remove.

    The batch derives each project's roadmap over the plans its readers name,
    and grows that set as a later reader names a plan the earlier build did not
    carry. Building over the project's whole inventory instead parsed every
    plan document the project holds, so the batch's peak followed the plan set
    rather than the batch. A section verdict is computed the same way from
    either inventory, so the growing build answers the bounded per-section
    question too.
    """
    from reckon import roadmap as roadmap_module
    from reckon.roadmap import roadmap_build_scope

    original = roadmap_module.build_roadmap
    built: dict[str, dict[str, Any]] = cache if cache is not None else {}

    def once(project, inventory, sprints, **kwargs):
        key = str(project)
        named = {
            str(row.get("slug")): row
            for row in (inventory or ())
            if getattr(row, "get", None) is not None and row.get("slug")
        }
        held = built.get(key)
        if held is not None and (
            held["plans"] is None or not (named.keys() - held["plans"])
        ):
            return held["roadmap"]
        docs_dir = kwargs.get("docs_dir") or (held or {}).get("docs_dir")
        if not named:
            # A caller that names no plan asks about the whole project, which
            # discovery is the one read that answers. Neither reader in a batch
            # reads who coordinates a critical-path link, so the build inside
            # discovery runs with coordinator resolution off: it reads every
            # run's ledger document, which was most of each entry's queue time.
            from reckon.mcp_discovery import _discover_project, _inventory_row

            with roadmap_build_scope(
                resolve_coordinators=False, audit_landings=False
            ) as scope:
                discovered = _discover_project(
                    key, Path(docs_dir).parent if docs_dir else None
                )
            rows = [
                _inventory_row(item) for item in discovered.get("inventory", ()) or ()
            ]
            report = (
                scope.built
                if scope.built is not None
                else original(
                    project,
                    rows or inventory,
                    list(discovered.get("sprints") or ()) or list(sprints or ()),
                    active_sprint_id=discovered.get("active_sprint_id"),
                    docs_dir=docs_dir,
                )
            )
            built[key] = {
                "roadmap": report,
                "rows": {},
                "plans": None,
                "docs_dir": docs_dir,
            }
            return report
        # A named inventory is answered from its own rows plus the plans an
        # earlier reader in the same batch named. Discovery would answer the
        # same question having parsed every plan document the project holds,
        # which is the plan-set term a batch's peak memory turned on.
        rows = dict(held["rows"]) if held is not None else {}
        rows.update(named)
        report = original(
            project,
            list(rows.values()),
            list(sprints or ()),
            **{**kwargs, **({"docs_dir": docs_dir} if docs_dir else {})},
        )
        built[key] = {
            "roadmap": report,
            "rows": rows,
            "plans": set(rows),
            "docs_dir": docs_dir,
        }
        return report

    roadmap_module.build_roadmap = once
    try:
        yield
    finally:
        roadmap_module.build_roadmap = original


#: The wording that opens a section's negative-control paragraph, together with
#: the separator that ends the heading — a colon or a full stop, or the dash a
#: plan may use in its place. The separator is part of the pattern because a
#: section's explanatory prose also names its negative control while saying why
#: the section exists; only the paragraph that states the declaration closes
#: the phrase with a separator, so matching one is what tells the two apart.
_NEGATIVE_CONTROL_MARKER = re.compile(
    "\\bnegative[\\s-]+control\\b\\s*[:.\\u2014\\u2013-]", re.IGNORECASE
)


def _negative_control_paragraph(prose: str) -> str | None:
    """The negative control a section states in its prose, or ``None``.

    The declaration is the section's closing paragraph, so the last phrase that
    opens one states it. A paragraph that opens ``none`` declares that no
    mutation applies and gives its reason; it is written with the colon the
    node's declaration reads that reason after, so a plan and a batch entry
    that spell the same exemption reach one declaration, and both are judged by
    the same reader.
    """
    normalised = " ".join(prose.split())
    matches = list(_NEGATIVE_CONTROL_MARKER.finditer(normalised))
    if not matches:
        return None
    rest = normalised[matches[-1].end() :].strip()
    rest = rest.lstrip(" .:-")
    if not rest:
        return None
    head = rest.partition(" ")[0]
    if head.rstrip(",;:.").lower() == NEGATIVE_CONTROL_NONE:
        reason = rest[len(head) :].lstrip(" ,;:.-")
        return f"{NEGATIVE_CONTROL_NONE}: {reason}" if reason else NEGATIVE_CONTROL_NONE
    return rest


class _PlanRead:
    """One plan document, parsed once for every section the batch takes from it.

    ``section_prose`` is called once per plan, so a batch naming five sections
    of one plan parses that plan once. The prose is kept by section identity so
    each entry's derived fields are bought from that single read.
    """

    def __init__(self, project: str, plan: str, repo_root: Path) -> None:
        from reckon import _plan_html

        self.project = project
        self.plan = plan
        self.repo_root = repo_root
        self.path = repo_root / "docs" / "plans" / f"{plan}.html"
        self.package = repo_root.name
        try:
            self.document = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise BatchEntryRefusedError(
                f"{project}:{plan}: plan document {self.path} cannot be read: {exc}"
            ) from exc
        self.prose: dict[str, str] = dict(_plan_html.section_prose(self.document))
        self.state = _plan_html.read_state(self.document)

    def prose_for(self, section: str) -> str:
        """The named section's prose, or a refusal naming the section."""
        from reckon.crew.node import normalize_section

        wanted = normalize_section(section)
        for identity, prose in self.prose.items():
            if normalize_section(identity) == wanted:
                return prose
        raise BatchEntryRefusedError(
            f"{self.project}:{self.plan}#{section}: the plan declares no such section"
        )

    def done_when(self, section: str) -> str:
        from reckon.crew.plan_review import _done_when_paragraph

        paragraph = _done_when_paragraph(self.prose_for(section))
        if not paragraph:
            raise BatchEntryRefusedError(
                f"{self.project}:{self.plan}#{section}: the section states no "
                "done-when, so the entry must give one"
            )
        return paragraph

    def negative_control(self, section: str) -> str:
        """The negative control the section states in its prose, or ``""``.

        A section that states none yields the empty string: the absence is
        answered where the node's check is known, because whether a missing
        control refuses the request depends on whether the node writes a check.
        """
        return _negative_control_paragraph(self.prose_for(section)) or ""

    def write_paths(self, section: str) -> list[str]:
        """Every path the section's write-paths sentence puts in scope.

        A sentence that names a file only by description leaves that file's
        path unsaid, and a fence built from the sentence alone would let the
        worker write a file it cannot then commit. Where the description is
        the only mention of a test — no literal path in the sentence names one
        — the path comes from the done-when, which names the test the entry
        will write. Exactly one test path under ``tests/`` resolves it;
        anything else is refused rather than guessed, because a fence built on
        a guess either omits the file the worker needs or grants a file nobody
        asked for.
        """
        from reckon.crew.plan_review import (
            _WRITE_PATHS_SENTENCE,
            _done_when_paragraph,
        )

        prose = self.prose_for(section)
        match = _WRITE_PATHS_SENTENCE.search(prose)
        if match is None:
            return []
        sentence = prose[match.end() :]
        stop = _SENTENCE_END.search(sentence)
        if stop is not None:
            sentence = sentence[: stop.start()]
        found: list[str] = []
        for token in _PATH_TOKEN.findall(sentence):
            if ".." in token.split("/") and (
                "/" in token or Path(token).suffix.lower() in _SOURCE_EXTENSIONS
            ):
                raise BatchEntryRefusedError(
                    f"{self.project}:{self.plan}#{section}: write path {token} "
                    f"carries a parent-directory segment, so it is refused rather "
                    f"than derived in a shortened form, from the sentence: "
                    f"{sentence.strip()}"
                )
            # A leading ``./`` is a relative prefix and goes; a leading ``/`` is
            # the path's own root and stays.
            trimmed = token
            while trimmed.startswith("./"):
                trimmed = trimmed[2:]
            candidate = trimmed.lstrip(".-").rstrip("./-")
            if not candidate:
                continue
            if "/" not in candidate and (
                Path(candidate).suffix.lower() not in _SOURCE_EXTENSIONS
            ):
                continue
            if candidate not in found:
                found.append(candidate)
        for candidate in found:
            if Path(candidate).is_absolute():
                continue
            resolved = self.repo_root / candidate
            if resolved.exists() or resolved.parent.is_dir():
                continue
            raise BatchEntryRefusedError(
                f"{self.project}:{self.plan}#{section}: derived write path "
                f"{candidate} is neither present under {self.repo_root} nor in a "
                f"present directory, from the sentence: {sentence.strip()}"
            )
        # The sentence may itself sit inside the done-when paragraph, so the
        # evidence for a described file is what the done-when says before its
        # write-paths sentence: the sentence names no test path by definition
        # here, and reading it again would only re-read the description.
        paragraph = _done_when_paragraph(prose) or ""
        marker = _WRITE_PATHS_SENTENCE.search(paragraph)
        if marker is not None:
            paragraph = paragraph[: marker.start()]
        return found + self._described_test_path(
            section, sentence=sentence, named=found, done_when=paragraph
        )

    def _described_test_path(
        self,
        section: str,
        *,
        sentence: str,
        named: Sequence[str],
        done_when: str,
    ) -> list[str]:
        """The test path a description stands for, when the done-when names one.

        The path is read with the repository's one owner of "a test path a text
        names", the pattern the repair round grants a test from, so both
        surfaces accept the same set: a ``tests/`` path ending ``.py``. A rule
        written here instead would drift the moment that one moves.
        """
        from reckon.crew.recovery_repair_dispatch import _TEST_PATH_PATTERN

        if any("test" in Path(path).name.lower() for path in named):
            return []
        described = _DESCRIBED_FILE.search(sentence)
        if described is None:
            return []
        phrase = " ".join(described.group(0).split())
        candidates: list[str] = []
        for match in _TEST_PATH_PATTERN.finditer(done_when):
            candidate = match.group(0)
            if candidate not in candidates:
                candidates.append(candidate)
        if len(candidates) == 1:
            return candidates
        raise BatchEntryRefusedError(
            f"{self.project}:{self.plan}#{section}: the write-paths sentence "
            f"names a file as {phrase!r} without naming its path, and the "
            f"done-when names {len(candidates)} file(s) under tests/ "
            f"({', '.join(candidates) if candidates else 'none'}), so exactly "
            f"one cannot be taken from it; the entry must give that path, from "
            f"the sentence: {sentence.strip()}"
        )

    def section_record(self, section: str) -> Mapping[str, Any]:
        from reckon.crew.node import normalize_section

        wanted = normalize_section(section)
        for record in self.state.get("sections") or ():
            if not isinstance(record, Mapping):
                continue
            if normalize_section(str(record.get("id") or "")) == wanted:
                return record
        return {}


class _Plans:
    """Every plan a batch names, each read at most once."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self._reads: dict[tuple[str, str], _PlanRead] = {}

    def read(self, project: str, plan: str) -> _PlanRead:
        key = (project, plan)
        if key not in self._reads:
            root = self.repo_root
            if not (root / "docs" / "plans" / f"{plan}.html").is_file():
                # A batch spanning projects reads each plan from the checkout
                # its project is mounted in, never from the caller's directory.
                from reckon.crew.dispatch_sections import project_mount_repository

                mount = project_mount_repository(project)
                if mount is not None:
                    root = mount
            self._reads[key] = _PlanRead(project, plan, root)
        return self._reads[key]


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    raise BatchEntryRefusedError(f"{value!r} is not a list of strings")


#: How a section's declared autonomy maps onto the specification level a
#: dispatch records. Autonomy names who owns the design — the section fixes it
#: (``guided``) or the worker derives it (``autonomous`` → an open spec) — which
#: is exactly the question the specification level answers.
_SPEC_LEVEL_FROM_AUTONOMY = {"guided": "guided", "autonomous": "open"}

#: A capability request that states no autonomy still declares a design the
#: worker implements, so its section dispatches guided rather than undeclared.
DEFAULT_SECTION_SPEC_LEVEL = "guided"

def section_time_budget(effort_hours: Any) -> str:
    """The wall-clock budget a section's effort estimate buys.

    The estimate is the plan's own sizing of the work, so an entry that names
    no budget inherits it. It is stated in whole minutes: an estimate of 2.5
    hours is ``150m``, which ``parse_duration`` reads back as the same figure.
    """
    try:
        minutes = int(round(float(effort_hours) * 60))
    except (TypeError, ValueError):
        return ""
    return f"{minutes}m" if minutes > 0 else ""


def section_spec_level(capability: Mapping[str, Any] | None) -> str:
    """The specification level a section's capability request declares.

    A section that declares no capability request states nothing about the
    design's ownership, so it supplies no level and the node's own declaration
    (or its absence) stands.
    """
    requirements = (capability or {}).get("requirements") or {}
    if not (capability or {}).get("class") and not requirements:
        return ""
    autonomy = str(requirements.get("tool_autonomy") or "").strip().lower()
    return _SPEC_LEVEL_FROM_AUTONOMY.get(autonomy, DEFAULT_SECTION_SPEC_LEVEL)




def section_derivation(
    read: _PlanRead, section: str, *, derive_write_paths: bool = True
) -> dict[str, tuple[Any, str]]:
    """Each node field this section supplies, with the digest of its value.

    A batch entry that omits a field takes the section's, and a released
    request re-derives those same fields, so both sides of that contract are
    answered here rather than twice. A field the section does not supply is
    absent, which is what keeps a supplied or edited field the request's own.
    The digest is the plan reader's own canonical-JSON digest, so a value
    recorded at queue time and the same value re-derived at release compare
    equal.
    """
    from reckon.crew.plan_review import _digest

    values: dict[str, Any] = {}
    with contextlib.suppress(BatchEntryRefusedError):
        values["done_when"] = read.done_when(section)
    # A section that states no control supplies none, and the request keeps
    # whatever its entry gave, or is refused where the node's check is known.
    with contextlib.suppress(BatchEntryRefusedError):
        declaration = read.negative_control(section)
        if declaration:
            values["negative_control"] = declaration
    # An entry that gives its own write paths keeps them, so the sentence is
    # not read for it at all: a description the sentence cannot resolve must
    # not refuse an entry that never needed the derivation.
    if derive_write_paths:
        paths = read.write_paths(section)
        if paths:
            values["write_paths"] = paths
    record = read.section_record(section)
    estimate = record.get("effort_hours")
    if isinstance(estimate, (int, float)):
        values["estimated_hours"] = float(estimate)
        budget = section_time_budget(estimate)
        if budget:
            values["time_budget"] = budget
    values["spec_level"] = section_spec_level(record.get("capability"))
    return {name: (value, _digest(value)) for name, value in values.items()}


#: The node fields a section supplies to a dispatch that omits them.
SECTION_SUPPLIED_NODE_FIELDS = (
    "done_when",
    "time_budget",
    "spec_level",
    "negative_control",
)


def apply_section_defaults(
    node: Any, *, project: str, repo_root: str | Path
) -> list[str]:
    """Fill every node field this section supplies and the node left unsaid.

    A dispatch that names a plan section names its authority, and the section
    already states the measure, the sizing, the write paths and the mutation its
    check must fail against. Requiring the caller to repeat them is how a
    reminder of the section's own fields refuses the request, and leaving the
    write paths to the caller is how a single dispatch ran a worker whose
    committed files fell outside the scope the section had already stated.

    Only an empty field is filled: a value the caller gave always wins, and a
    dispatch naming its own write paths keeps exactly those. A section stating
    no write paths leaves the node's list empty, so the default landing scope
    stands as it does for any node that declares none.

    A plan or section that cannot be read is left alone rather than refused
    here, because the dispatch paths already name that failure (plan visibility,
    routing evidence) with the flag the caller can correct.
    """
    plan = str(getattr(node, "plan", "") or "").strip()
    section = str(getattr(node, "section", "") or "").strip()
    if not plan or not section:
        return []
    omitted = [
        name
        for name in SECTION_SUPPLIED_NODE_FIELDS
        if not str(getattr(node, name, "") or "").strip()
    ]
    # A write-path list is a sequence rather than a scalar, so an empty one
    # compares truthy as text; it takes its default from the section on the same
    # rule as an empty string field.
    needs_write_paths = not [
        str(path).strip() for path in (getattr(node, "write_paths", None) or ())
    ]
    if not omitted and not needs_write_paths:
        return []
    try:
        read = _PlanRead(project, plan, Path(repo_root))
        derivation = section_derivation(read, section, derive_write_paths=False)
    except (BatchEntryRefusedError, OSError):
        return []
    filled: list[str] = []
    for name in omitted:
        if name not in derivation:
            continue
        setattr(node, name, derivation[name][0])
        filled.append(name)
    if needs_write_paths:
        # Derived on its own so a sentence that names a file only by
        # description, and cannot resolve it, leaves the other fields already
        # filled rather than discarding them with the write paths.
        with contextlib.suppress(BatchEntryRefusedError, OSError):
            paths = read.write_paths(section)
            if paths:
                node.write_paths = list(paths)
                filled.append("write_paths")
    return filled


def rederived_section_fields(
    *,
    project: str,
    plan: str,
    section: str,
    repo_root: str | Path,
    marked: Mapping[str, str],
    stored: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    """Re-derive a stored request's section-supplied fields from the plan now.

    ``marked`` is the field-to-digest record the queue writer left on the
    request, naming which of its node fields the section supplied, and
    ``stored`` is the node those fields sit on. Both digests are taken here, the
    old one by digesting the stored field rather than by trusting its mark: a
    mark written under an earlier spelling of the same value is not a change,
    and comparing text the two sides both mean is what says so. A field the
    section no longer states is absent from the values, which is what keeps the
    request's stored value.

    A plan or section that cannot be read raises ``BatchEntryRefusedError``.
    The caller must not read that as "nothing was derived": releasing on the
    stored fields would put the request back on the text the plan has left
    behind, which is exactly what the release re-derivation exists to stop.
    """
    from reckon.crew.plan_review import _digest

    read = _PlanRead(project, plan, Path(repo_root))
    # Only a field the request took from the section is re-derived, so a
    # request that supplied its own write paths is never asked to read the
    # write-paths sentence: a described test it cannot resolve must not refuse
    # a release that was never going to use that field.
    derivation = section_derivation(
        read, section, derive_write_paths="write_paths" in marked
    )
    values: dict[str, Any] = {}
    digests: dict[str, dict[str, str]] = {}
    for name, stored_digest in marked.items():
        fresh = derivation.get(name)
        if fresh is None:
            continue
        value, digest = fresh
        values[name] = value
        previous = _digest(stored[name]) if name in stored else str(stored_digest)
        digests[name] = {"old": previous, "new": digest}
    return values, digests


def _entry_node(
    index: int,
    entry: Mapping[str, Any],
    *,
    plans: _Plans,
    defaults: Mapping[str, Any] | None = None,
) -> tuple[str, TaskNode, dict[str, str]]:
    """Build one entry's node, filling every omitted field from its section.

    The third value names every field whose value the section supplied, with
    the digest of the value taken. The queue records it on the stored request:
    a field the dispatcher supplied or an editor set carries no mark, so a
    later release can re-derive the section's fields and keep the rest.
    """
    unknown = sorted(set(entry) - BATCH_ENTRY_FIELDS)
    if unknown:
        raise BatchEntryRefusedError(
            f"entry {index + 1} carries unknown field(s): {', '.join(unknown)}"
        )
    # The waiver is read as a boolean, so a batch file that spells it "false"
    # cannot waive the review by being a non-empty string.
    if not isinstance(entry.get("allow_unreviewed_plan", False), bool):
        raise BatchEntryRefusedError(
            f"entry {index + 1} allow_unreviewed_plan must be true or false"
        )
    # A priority is a whole number of points, so a batch file that spells it
    # "3" is refused rather than read as no priority at all: the string would
    # otherwise store a request nobody prioritised while the entry says it did.
    priority = entry.get("priority", 0)
    if isinstance(priority, bool) or not isinstance(priority, int) or priority < 0:
        raise BatchEntryRefusedError(
            f"entry {index + 1} priority must be a whole number of points, "
            f"zero or more, not {entry.get('priority')!r}"
        )
    project = str(entry.get("project") or "").strip()
    plan = str(entry.get("plan") or "").strip()
    section = str(entry.get("section") or "").strip()
    node_id = str(entry.get("node") or "").strip()
    goal = str(entry.get("goal") or "").strip()
    if not project or not plan or not section:
        raise BatchEntryRefusedError(
            f"entry {index + 1} must name its project, plan and section"
        )
    if not node_id:
        raise BatchEntryRefusedError(f"entry {index + 1} names no node id")
    if not goal:
        raise BatchEntryRefusedError(
            f"{project}:{plan}#{section}: the entry gives no goal"
        )

    read = plans.read(project, plan)
    write_paths = _as_list(entry.get("write_paths"))
    derivation = section_derivation(read, section, derive_write_paths=not write_paths)
    derived: dict[str, str] = {}

    done_when = str(entry.get("done_when") or "").strip()
    if not done_when:
        if "done_when" not in derivation:
            raise BatchEntryRefusedError(
                f"{project}:{plan}#{section}: the section states no "
                "done-when, so the entry must give one"
            )
        done_when, derived["done_when"] = derivation["done_when"]
    if not write_paths and "write_paths" in derivation:
        # A node with no write paths is queued as one: the worktree is its
        # scope, so a section stating no sentence is dispatchable rather than
        # refused. An entry that declares paths keeps them, exactly as before.
        write_paths, derived["write_paths"] = derivation["write_paths"]

    # A control the entry states is the entry's own and is never overridden by
    # the section's paragraph, exactly as a supplied field beats a derived one.
    negative_control = str(entry.get("negative_control") or "").strip()
    if not negative_control and "negative_control" in derivation:
        negative_control, derived["negative_control"] = derivation["negative_control"]

    estimated_hours = entry.get("estimated_hours")
    if estimated_hours is None and "estimated_hours" in derivation:
        estimated_hours, derived["estimated_hours"] = derivation["estimated_hours"]
    # Every field the entry leaves out is the section's. The budget, the
    # specification level and the negative control are read from the section
    # exactly as the done-when and the write paths already were, so an entry
    # naming only its project, plan, section, node and goal is dispatchable.
    time_budget = str(entry.get("time_budget") or "")
    if not time_budget and "time_budget" in derivation:
        time_budget, derived["time_budget"] = derivation["time_budget"]
    spec_level = str(entry.get("spec_level") or "")
    if not spec_level and "spec_level" in derivation:
        spec_level, derived["spec_level"] = derivation["spec_level"]
    negative_control = str(entry.get("negative_control") or "")
    if not negative_control and "negative_control" in derivation:
        negative_control, derived["negative_control"] = derivation["negative_control"]
    # A field the entry leaves out falls back to the command-level value, so a
    # coordinator queuing one batch for many sections states a reason, a
    # comment or a lane once rather than on every entry.
    over_fence_reason = str(entry.get("over_fence_reason") or "") or str(
        (defaults or {}).get("over_fence_reason") or ""
    )

    node = TaskNode(
        id=node_id,
        goal=goal,
        plan=plan,
        section=section,
        after=_as_list(entry.get("after")),
        waits_only_after=bool(entry.get("waits_only_after")),
        brief=str(entry.get("brief") or ""),
        role=str(entry.get("role") or "implement"),
        spec_level=spec_level,
        negative_control=negative_control,
        mcp_set=str(entry.get("mcp_set") or ""),
        done_when=done_when,
        write_paths=list(write_paths),
        regenerate_paths=_as_list(entry.get("regenerate_paths")),
        anchor_paths=_as_list(entry.get("anchor_paths")),
        time_budget=time_budget,
        over_fence_reason=over_fence_reason,
        estimated_hours=estimated_hours,
        manifest_path=str(entry.get("manifest") or ""),
        requires_decisions=_as_list(entry.get("requires_decisions")),
        peer_scopes=_peer_scopes(entry.get("peers")),
        images=_as_list(entry.get("images")),
        inputs=_as_list(entry.get("inputs")),
        compares_with=str(entry.get("compares_with") or ""),
    )
    if negative_control_writes_a_check(node):
        # The section's prose is the authority on its own control, so its
        # absence is refused here, by name, rather than left to the generic
        # node contract where neither the section nor the paragraph it lacks is
        # named. The refusal a plan-level dispatch raises is the section's to
        # repair, so it says what the section is missing.
        if node.write_paths:
            trigger = (
                f"its write paths include a test file ({', '.join(node.write_paths)})"
            )
        else:
            trigger = "its done-when names a test file"
        raise BatchEntryRefusedError(
            f"{project}:{plan}#{section}: the section states no negative-control "
            f"paragraph, and {trigger}, so it writes a check, but its "
            f"negative_control field declares no mutation that check must fail "
            f"against; add a `Negative control.` paragraph to the section, or "
            f"declare the control as `{NEGATIVE_CONTROL_NONE}: <reason>`"
        )
    return project, node, derived


def _entry_options(
    index: int, entry: Mapping[str, Any], defaults: Mapping[str, Any]
) -> dict[str, Any]:
    """The steering comment, lane and queue priority one entry carries.

    Each value the entry leaves out is taken from the command-level default, so
    an option given beside the batch applies to every entry that does not set
    it. The two switches are read as booleans rather than as non-empty strings,
    because a batch file spelling one ``"false"`` must not read as set.
    """
    comment = str(entry.get("comment") or "") or str(defaults.get("comment") or "")

    raw_local = entry.get("local")
    if raw_local is None:
        local = bool(defaults.get("local"))
    elif isinstance(raw_local, bool):
        local = raw_local
    else:
        raise BatchEntryRefusedError(f"entry {index + 1} local must be true or false")

    raw_priority = entry.get("priority")
    priority = defaults.get("priority") if raw_priority is None else raw_priority
    if priority is not None and (
        isinstance(priority, bool) or not isinstance(priority, int)
    ):
        raise BatchEntryRefusedError(
            f"entry {index + 1} priority must be a whole number"
        )
    return {"comment": comment, "local": local, "priority": priority}


def _peer_scopes(values: Any) -> dict[str, list[str]]:
    scopes: dict[str, list[str]] = {}
    for item in _as_list(values):
        name, _, paths = str(item).partition("=")
        scopes[str(name).strip()] = [
            path.strip() for path in paths.split(",") if path.strip()
        ]
    return scopes


def _stamp_section_derived(run_id: str, derived: Mapping[str, str]) -> None:
    """Record on a stored request which fields its section supplied.

    The mark lives on the stored node beside the values it describes, so an
    editor that changes one of those fields clears it in the same write, and a
    release reads it without re-reading the plan the request was queued from.
    """
    from reckon.crew import runs as runs_module

    def stamp(current: dict[str, Any]) -> dict[str, Any]:
        node_block = dict(current.get("node") or {})
        node_block["section_derived"] = dict(derived)
        current["node"] = node_block
        return current

    runs_module._mutate_pointer(run_id, stamp)


def _wait_verdict(
    waits: Sequence[str],
    *,
    project: str,
    plan: str,
    section: str,
    docs_dir: Path,
    live: Sequence[Mapping[str, Any]],
) -> tuple[str, list[str]]:
    """The phase a request's waits justify and the findings the read produced."""
    from reckon.crew.admission import readiness as _readiness

    return _readiness.preview_wait_phase(
        waits,
        project=project,
        plan=plan,
        section=section,
        docs_dir=docs_dir,
        live=live,
    )


def _record_wait_findings(run_id: str, findings: Sequence[str]) -> None:
    """Write what a wait's read found onto the stored request.

    The admission pass records the same values when it next resolves the
    request; writing them here means the coordinator reading the dispatch
    answer finds it on the request it stored, without waiting for a pass.
    """
    from reckon.crew import runs as runs_module

    def record(current: dict[str, Any]) -> dict[str, Any]:
        merged = list(
            dict.fromkeys([*current.get("upstream_obligations", ()), *findings])
        )
        current["upstream_obligations"] = merged
        return current

    runs_module._mutate_pointer(run_id, record)


def _require_stored(stored: Mapping[str, Any], project: str, node: TaskNode) -> None:
    """Refuse an entry whose request the queue did not store."""
    if not stored:
        raise BatchEntryRefusedError(
            f"{project}:{node.plan}#{node.section}: the request was not stored"
        )


def _batch_order(entries: Sequence[Mapping[str, Any]]) -> Iterator[int]:
    """Yield entry indexes so an entry's batch-siblings are stored before it."""
    by_node = {
        str(entry.get("node") or "").strip(): index
        for index, entry in enumerate(entries)
        if str(entry.get("node") or "").strip()
    }
    pending = list(range(len(entries)))
    emitted: set[int] = set()
    while pending:
        progressed = False
        for index in list(pending):
            entry = entries[index]
            siblings = {
                by_node[ref]
                for ref in _as_list(entry.get("after"))
                if ref in by_node and by_node[ref] != index
            }
            if siblings <= emitted:
                emitted.add(index)
                pending.remove(index)
                progressed = True
                yield index
        if not progressed:
            raise CrewError(
                "batch waits form a cycle among entries: "
                + ", ".join(str(entries[index].get("node")) for index in pending)
            )


def _plan_history(project: str, plan: str, root: str | Path) -> list[dict[str, Any]]:
    """One plan's recorded runs, read through the store's own plan index.

    Queuing a plan asks, section by section, whether a run has already
    delivered that section, so the rows that answer the question are that
    plan's. Reading the project's whole projected history instead made a
    batch's memory follow the run store's row count: every plan the batch named
    pulled the project's full projection again, so a store carrying more
    delivered rows cost more per plan and the batch's peak grew with it.

    The store answers a plan filter from its own index, which resolves the plan
    through the same three fields :func:`reckon.ledger.row_plan` reads, so a
    brief-carried row is found as well as one carrying a flat plan. The
    committed ledger is read only when the store holds nothing for the project,
    which is the store's own rule for answering history at all; a plan the
    store simply holds no delivery for answers empty, and falling through to
    the ledger there would decode the project's whole projection to filter it
    back down to that empty answer.
    """
    from reckon import ledger, run_store

    try:
        rows = run_store.stored_run_rows(
            project, plan=plan, fields=ledger._PICKER_FIELDS
        )
    except Exception:  # noqa: BLE001 - an unreadable store answers no history
        rows = None
    if rows is None:
        return ledger.picker_runs(project, root)
    # The projection carries the row's own plan field and drops the two places
    # a brief-carried dispatch records the same fact, so a row filtered in by
    # its impl move reaches here with no flat plan. The filter is what
    # establishes it, and the caller compares candidates on it.
    return [
        row if str(row.get("plan") or "").strip() else {**row, "plan": plan}
        for row in rows
    ]


#: The fixed answers a plan's queue read gives, one per implementable section.
#: Every caller that answers "may this be queued" — the whole-plan dispatch,
#: the single-section dispatch and the admission service — reports one of
#: these, so a reader comparing two surfaces compares one vocabulary.
QUEUE_ANSWER_MAY = "may be queued"
QUEUE_ANSWER_REVIEW = "review owed"
QUEUE_ANSWER_DECISION = "an open decision holds the plan"
QUEUE_ANSWER_HOLD = "a coordinator's hold covers it"
QUEUE_ANSWER_SPRINT = "the plan's sprint is not one the session works in"

#: The session identity the admission service files a request under when the
#: plan has no coordinator of its own. A service pass answers for a project
#: rather than for one coordinator, so the sprint rule that constrains which
#: unowned plans a session may take on does not apply to it: the service's
#: purpose is to queue work nobody's session has taken, across every sprint the
#: project holds. Other dispatchers identify themselves by their own session id.
ADMISSION_SERVICE_SESSION = "admission-service"

#: The event a whole-plan dispatch appends to the project's watch stream when a
#: session takes on an unowned plan. It is its own kind, so a reader never
#: mistakes it for a run's state change: the plan's coordinator is derived from
#: the request this dispatch queued, and this row is how the project's other
#: sessions learn who took it.
PLAN_TAKEN_ON_EVENT = "plan-taken-on"


def _coordinated_answer(session: str) -> str:
    """The answer a section of another session's plan carries."""
    return f"plan coordinated by session {session}"


def plan_taken_on_row(
    project: str, plan: str, session: str, *, at: str | None = None
) -> dict[str, Any]:
    """One watch row naming the plan a session took on and the session.

    The row carries no ``session``: it is a broadcast about a plan rather than
    one coordinator's run, so a follower on every live session in the project
    reads it, the taking session's own pane included. The taking session rides
    ``took_by`` as a structured field beside the free-text ``detail`` clause a
    formatter may render, so a reader that must know who took the plan reads the
    field rather than parsing the clause.
    """
    from datetime import UTC, datetime

    return {
        "project": project,
        "event": PLAN_TAKEN_ON_EVENT,
        "kind": PLAN_TAKEN_ON_EVENT,
        "observed_at": at or datetime.now(UTC).isoformat(),
        "plan": plan,
        "took_by": session,
        "detail": f"session {session} took on plan {plan}",
    }


class _QueueReads:
    """One read answering, per section, whether a plan's work may be queued.

    Each reason is taken from the place that already computes it: the
    coordinator resolution names another session's plan, a stored request's
    ``queue_hold`` covers it, the roadmap's decision rows hold it, and the
    dispatch's own plan-review gate says whether a review is owed. The plan is
    parsed once and its review evidence is judged once per section, so a
    whole-plan dispatch, a batch beside it and the service's pass all read one
    answer rather than three derivations that can drift.

    ``config_for`` answers a project's flight configuration, which is where the
    review gate's enforce-or-report mode lives: in report-only mode a missing
    review does not hold work, so the read answers ``may be queued`` exactly as
    the dispatch would proceed.
    """

    def __init__(
        self,
        *,
        session: str,
        repo_root: str | Path,
        config_for,
        allow_unreviewed: bool = False,
        live: Sequence[Mapping[str, Any]] | None = None,
        plans: _Plans | None = None,
    ) -> None:
        self.session = session
        self.config_for = config_for
        self.allow_unreviewed = allow_unreviewed
        self._plans = plans or _Plans(Path(repo_root).expanduser().resolve())
        self._live = list(live) if live is not None else None
        self._ownership: dict[str, Mapping[str, Any]] = {}
        self._authority: dict[tuple[str, str], Mapping[str, Any]] = {}
        self._answers: dict[
            tuple[str, str, bool], dict[str, dict[str, Any]]
        ] = {}
        self._sprints_worked: dict[tuple[str, str, str], bool] = {}

    def live(self, project: str) -> list[Mapping[str, Any]]:
        if self._live is not None:
            return self._live
        from reckon.crew import runs

        try:
            self._live = list(runs.list_live(project=project))
        except Exception:  # noqa: BLE001 - an unreadable live set holds nothing
            self._live = []
        return self._live

    def ownership(self, project: str, repo_root: str | Path) -> Mapping[str, Any]:
        if project not in self._ownership:
            from reckon.crew.plan_coordinator import resolve_plan_coordinators

            self._ownership[project] = resolve_plan_coordinators(
                project, root=repo_root, pointers=self.live(project)
            )
        return self._ownership[project]

    def _authority_for(self, project: str, repo_root: str | Path) -> Mapping[str, Any]:
        key = (project, str(repo_root))
        if key not in self._authority:
            from reckon.crew import routing

            self._authority[key] = routing.resolve_dispatch_authority(
                project, repo_root
            )
        return self._authority[key]

    def answers(
        self,
        project: str,
        plan: str,
        *,
        allow_unreviewed: bool | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Every implementable section of one plan, with its answer."""
        waived = (
            self.allow_unreviewed if allow_unreviewed is None else allow_unreviewed
        )
        key = (project, plan, bool(waived))
        if key not in self._answers:
            self._answers[key] = self._read(
                project, plan, allow_unreviewed=bool(waived)
            )
        return self._answers[key]

    def answer(
        self,
        project: str,
        plan: str,
        section: str,
        *,
        allow_unreviewed: bool | None = None,
    ) -> dict[str, Any] | None:
        """One section's answer, or ``None`` when the plan does not declare it."""
        from reckon.crew.node import normalize_section

        wanted = normalize_section(section)
        for identity, row in self.answers(
            project, plan, allow_unreviewed=allow_unreviewed
        ).items():
            if normalize_section(identity) == wanted:
                return row
        return None

    def _read(
        self, project: str, plan: str, *, allow_unreviewed: bool
    ) -> dict[str, dict[str, Any]]:
        from reckon import _plan_html
        from reckon.crew.node import normalize_section

        read = self._plans.read(project, plan)
        sections = [
            todo
            for todo in _plan_html.derive_section_todos(read.document, read.state)
            if todo.get("declaration") == "implementable"
        ]
        answers = {
            todo["id"]: {"section": todo["id"], "may": True, "reason": QUEUE_ANSWER_MAY}
            for todo in sections
        }
        if not answers:
            return answers
        ownership = self.ownership(project, read.repo_root)
        owned = (ownership.get("plans") or {}).get(plan) or {}
        # Only a session still dispatching holds a plan out of the queue. A
        # coordinator whose runs have all ended names no one who could be
        # building the plan now, and the service is what takes a plan on once
        # its coordinator has stopped.
        owner = str(owned.get("coordinator") or "") if owned.get("live") else ""
        if owner and owner != self.session:
            for row in answers.values():
                row.update(
                    may=False, reason=_coordinated_answer(owner), session=owner
                )
            return answers
        if not owner and self.session != ADMISSION_SERVICE_SESSION:
            sprint = str((read.state or {}).get("sprint") or "").strip()
            if sprint and not self._works_in_sprint(read.repo_root, project, sprint):
                for row in answers.values():
                    row.update(may=False, reason=QUEUE_ANSWER_SPRINT)
                return answers
        held_whole, held = self._held_sections(project, plan)
        if held_whole or held:
            for identity, row in answers.items():
                if held_whole or normalize_section(identity) in held:
                    row.update(may=False, reason=QUEUE_ANSWER_HOLD)
        decisions = self._decisions(read, plan)
        for identity, row in answers.items():
            if not row["may"]:
                continue
            if decisions["holds_whole_plan"] or (
                normalize_section(identity) in decisions["sections"]
            ):
                row.update(may=False, reason=QUEUE_ANSWER_DECISION)
        config = self.config_for(project) if self.config_for is not None else None
        if not self._review_gate_enforces(config):
            return answers
        for identity, row in answers.items():
            if row["may"] and self._review_owed(
                project, plan, identity, read, allow_unreviewed=allow_unreviewed
            ):
                row.update(may=False, reason=QUEUE_ANSWER_REVIEW)
        return answers

    def _works_in_sprint(
        self, repo_root: str | Path, project: str, sprint: str
    ) -> bool:
        """Whether this session already coordinates a plan in ``sprint``.

        The session's standing is read from the same coordinator resolution the
        queue read uses: a plan the resolution names this session for is one the
        session works in, and the sprint it declares is a sprint the session
        works in. Only the plan's head is read for its sprint, so a session
        coordinating many plans pays one small read each rather than parsing
        their bodies on every queue read.
        """
        from reckon._plan_html import _parse_meta_head

        key = (project, str(repo_root), sprint)
        cached = self._sprints_worked.get(key)
        if cached is not None:
            return cached
        worked = False
        ownership = self.ownership(project, repo_root)
        for slug, row in (ownership.get("plans") or {}).items():
            if str(row.get("coordinator") or "") != self.session:
                continue
            path = Path(repo_root) / "docs" / "plans" / f"{slug}.html"
            try:
                declared = str(_parse_meta_head(path).get("sprint") or "").strip()
            except OSError:
                declared = ""
            if declared and declared == sprint:
                worked = True
                break
        self._sprints_worked[key] = worked
        return worked

    def _held_sections(self, project: str, plan: str) -> tuple[bool, set[str]]:
        """The sections a coordinator's queue hold covers, and whether it is whole.

        A hold lives on the stored request it was written to and names the
        section that request builds; one that names no section, or is written
        against the plan itself, covers every section of the plan. A plan-level
        hold covers the whole plan whether or not it has a request yet, which is
        the hold a coordinator places before the service has queued anything.
        """
        from reckon.crew import queue_edit
        from reckon.crew.node import normalize_section

        if queue_edit.plan_hold(project, plan) is not None:
            return True, set()
        whole = False
        sections: set[str] = set()
        for pointer in self.live(project):
            if str(pointer.get("project") or "") != project:
                continue
            if not (pointer.get("queue_hold") or {}):
                continue
            node = pointer.get("node")
            node = node if isinstance(node, Mapping) else {}
            pointer_plan = str(pointer.get("plan") or node.get("plan") or "")
            if pointer_plan != plan:
                continue
            section = pointer.get("section") or node.get("section")
            if str(section or "").strip():
                sections.add(normalize_section(section))
            else:
                whole = True
        return whole, sections

    def _decisions(self, read: _PlanRead, plan: str) -> dict[str, Any]:
        """The sections an unsettled decision of this plan holds."""
        from reckon import roadmap

        return roadmap.queue_holding_decisions(
            {
                "slug": plan,
                "decisions": read.state.get("decisions") or {},
                "gates": read.state.get("gates") or [],
            }
        )

    @staticmethod
    def _review_gate_enforces(config: Mapping[str, Any] | None) -> bool:
        """Whether the resolved config holds work for an owed plan review."""
        from reckon import flight

        return flight.plan_review_gate_enforces(config)

    def _review_owed(
        self,
        project: str,
        plan: str,
        section: str,
        read: _PlanRead,
        *,
        allow_unreviewed: bool,
    ) -> bool:
        """Whether the plan's review holds this section.

        A plan with no stored review at all covers nothing, so every section of
        it is owed one and the store is never walked for coverage. Otherwise
        the dispatch's own gate is asked, narrowed to the units a dispatch of
        this section reads, so a finding anchored on another section does not
        hold this one.
        """
        from reckon.crew import plan_review, routing
        from reckon.crew.node import PlanReviewMissingError, TaskNode

        if allow_unreviewed:
            return False
        try:
            records = plan_review.list_plan_reviews(project, plan)
            if not records and (
                plan_review.newest_design_review(project, plan, records=records)
                is None
            ):
                return True
            routing.require_plan_reviewed(
                node=TaskNode(
                    id=f"{plan}-{section}",
                    goal="",
                    plan=plan,
                    section=section,
                    role="implement",
                ),
                project=project,
                repo=read.repo_root,
                authority=self._authority_for(project, read.repo_root),
                enforce=True,
                units=routing.plan_review_units(section),
            )
        except PlanReviewMissingError:
            return True
        except Exception:  # noqa: BLE001 - unreadable evidence is held, not waved
            return True
        return False


def plan_queue_answers(
    project: str,
    plan: str,
    *,
    session: str,
    repo_root: str | Path,
    config: Mapping[str, Any] | None = None,
    allow_unreviewed: bool = False,
    live: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """What the read answers for each implementable section of one plan."""
    reads = _QueueReads(
        session=session,
        repo_root=repo_root,
        config_for=lambda _project: config or {},
        allow_unreviewed=allow_unreviewed,
        live=live,
    )
    return reads.answers(project, plan)


def project_queue_answers(
    project: str,
    *,
    session: str,
    repo_root: str | Path,
    config: Mapping[str, Any] | None = None,
    allow_unreviewed: bool = False,
    plans_dir: str | Path | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    """The read over every plan of one project, keyed by plan then section.

    The coordinator resolution is derived once for the project rather than once
    per plan, which is what makes a read over a project of hundreds of plans
    cost the plans' own parsing rather than a resolution each.
    """
    root = Path(repo_root).expanduser().resolve()
    directory = Path(plans_dir) if plans_dir is not None else root / "docs" / "plans"
    slugs = sorted(path.stem for path in directory.glob("*.html"))
    reads = _QueueReads(
        session=session,
        repo_root=root,
        config_for=lambda _project: config or {},
        allow_unreviewed=allow_unreviewed,
    )
    return {slug: reads.answers(project, slug) for slug in slugs}


def queue_plan(
    project: str,
    plan: str,
    *,
    session: str,
    config_for,
    repo_root: str | Path,
    launch_kind: str = "cli",
    over_fence_reason: str = "",
    comment: str = "",
    local: bool = False,
    dry_run: bool = False,
    queued_by: str = "",
    live: Sequence[Mapping[str, Any]] | None = None,
    ownership: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Queue a plan's implementable sections, reporting every authored section.

    Ownership uses the coordinator view's resolver. Existing work is reported
    before deriving a new contract, so a repeat neither edits queued requests
    nor forgets delivered work whose pointer has already been promoted. The
    command-level options are carried to every section this queues, exactly as
    they are to the entries of a batch file.

    Delivered work is read from the run store, which is where a promotion puts
    its row, rather than from the ledger documents beside a checkout: the
    documents are no longer written, so a reader of them alone sees no landed
    run at all. A row is matched on the plan and section it names rather than
    on the node id, because a section dispatched on its own carries the node id
    that dispatch chose, not the one this call derives from the section. A
    stored row carries no phase, so a run whose own check failed is read from
    the gate and classification the row does carry: such a run is delivered
    only when its failure is recorded as one the node's work did not cause.

    ``live`` and ``ownership`` let a caller queueing many plans of one project
    pass the live pointers and the project's coordinator resolution it has
    already read. Both answer for the whole project, so reading them again for
    every plan repeats the same work once per plan.
    """
    from reckon import _plan_html, ledger
    from reckon.crew import runs
    from reckon.crew.node import normalize_section
    from reckon.crew.routing import resolved_time_ceiling

    plans = _Plans(Path(repo_root).expanduser().resolve())
    read = plans.read(project, plan)
    sections = _plan_html.derive_section_todos(read.document, read.state)
    live = list(live) if live is not None else list(runs.list_live(project=project))
    reads = _QueueReads(
        session=session,
        repo_root=repo_root,
        config_for=config_for,
        live=live,
        plans=plans,
    )
    if ownership is not None:
        reads._ownership[project] = ownership
    answers = reads.answers(project, plan)
    ownership = reads.ownership(project, read.repo_root)
    owned = (ownership.get("plans") or {}).get(plan) or {}
    owner = str(owned.get("coordinator") or "") if owned.get("live") else ""
    history = _plan_history(project, plan, read.repo_root)
    entries: list[dict[str, Any]] = []
    rows: dict[str, dict[str, Any]] = {}
    config = None
    for section in sections:
        identity = section["id"]
        node_id = f"{plan}-{identity}"
        row = {
            "index": len(rows),
            "project": project,
            "plan": plan,
            "section": identity,
            "node": node_id,
            "status": "skipped",
        }
        rows[identity] = row
        if owner and owner != session:
            # Every authored row names the coordinating session, including the
            # ones the read does not answer for: a caller enumerating a plan it
            # does not own needs the owner on each row it is told to leave.
            coordinated = _coordinated_answer(owner)
            row.update(reason=coordinated, session=owner, queue_answer=coordinated)
            continue
        answer = answers.get(identity)
        if answer is not None:
            row["queue_answer"] = answer["reason"]
        if answer is not None and not answer["may"]:
            row["reason"] = answer["reason"]
            if answer.get("session"):
                row["session"] = answer["session"]
            continue
        declaration = section.get("declaration")
        if declaration != "implementable":
            row["reason"] = declaration or "undeclared"
            continue
        existing = None
        for candidate in [*live, *history]:
            node = candidate.get("node")
            node = node if isinstance(node, Mapping) else {}
            candidate_plan = ledger.row_plan(candidate) or str(node.get("plan") or "")
            candidate_section = node.get("section") or candidate.get("section")
            phase = str(candidate.get("phase") or candidate.get("status") or "")
            gate = str(candidate.get("gate") or "")
            classification = str(candidate.get("failure_classification") or "")
            delivered = phase not in _UNDELIVERED_PHASES and (
                gate != "failed" or classification in _LANDED_DESPITE_FAILED_GATE
            )
            role = node.get("role") or candidate.get("role") or "implement"
            if (
                candidate_plan == plan
                and normalize_section(candidate_section) == normalize_section(identity)
                and role == "implement"
                and delivered
            ):
                existing = candidate
                break
        if existing is not None:
            held_session = str(existing.get("session") or "")
            run_id = str(existing.get("run_id") or "")
            row.update(
                reason=f"existing run {run_id} of session {held_session}",
                run_id=run_id,
                session=held_session,
            )
            continue
        if config is None:
            config = config_for(project)
        entries.append(
            {
                "project": project,
                "plan": plan,
                "section": identity,
                "node": node_id,
                "goal": section["heading"],
                "spec_level": "guided",
                "time_budget": resolved_time_ceiling(config),
            }
        )
    result = (
        queue_batch(
            entries,
            session=session,
            config_for=lambda _: config,
            repo_root=read.repo_root,
            launch_kind=launch_kind,
            over_fence_reason=over_fence_reason,
            comment=comment,
            local=local,
            dispatch_width="plan",
            dry_run=dry_run,
            queue_answers={(project, plan): answers},
            queued_by=queued_by,
        )
        if entries
        else {"ok": True, "queued": 0, "refused": 0, "results": []}
    )
    for queued in result["results"]:
        row = rows[queued["section"]]
        index = row["index"]
        row.update(queued)
        row["index"] = index
        if row["status"] == "already-queued":
            held_session = str(runs.read_pointer(row["run_id"]).get("session") or "")
            row.update(
                status="skipped",
                session=held_session,
                reason=f"existing run {row['run_id']} of session {held_session}",
            )
        else:
            row.setdefault("reason", row["status"])
    results = list(rows.values())
    skipped = sum(row["status"] == "skipped" for row in results)
    queued = len(results) - skipped - result["refused"]
    if not dry_run and not owner and queued > 0:
        # The plan had no coordinator and this dispatch became one, so the
        # project's other live sessions are told which plan was taken on and by
        # whom. A row that cannot be written never fails the dispatch: the
        # queued requests, not the row, are what took the plan on.
        with contextlib.suppress(Exception):
            runs._append_watch_lines(
                runs.watch_stream_path(project),
                [plan_taken_on_row(project, plan, session)],
            )
    return {
        "ok": result["ok"],
        "project": project,
        "plan": plan,
        "queued": len(results) - skipped - result["refused"],
        "skipped": skipped,
        "refused": result["refused"],
        "results": results,
    }


def queue_batch(
    entries: Sequence[Mapping[str, Any]],
    *,
    session: str,
    config_for,
    repo_root: str | Path,
    launch_kind: str = "cli",
    over_fence_reason: str = "",
    comment: str = "",
    local: bool = False,
    priority: int | None = None,
    dispatch_width: str = "",
    dry_run: bool = False,
    queue_answers: Mapping[
        tuple[str, str], Mapping[str, Mapping[str, Any]]
    ] | None = None,
    queued_by: str = "",
) -> dict[str, Any]:
    """Queue each entry on its own — see :func:`_queue_batch`.

    Inside one batch each named plan is parsed once, each named project's
    roadmap is derived once, and each plan's state, text and section headings
    are read once, so the work of queuing does not grow with the number of
    sections chosen.
    """
    from reckon.crew.queue_admission import plan_read_scopes
    from reckon.roadmap import roadmap_history_scope
    from reckon.serve import discovery_without_git_dates

    with (
        _parsed_once(),
        _built_once(),
        # The batch reads no plan's promoted history: it prevents a duplicate
        # with the per-node hold read, and the only reader of its roadmap — the
        # wait check — reads gate and decision blockers and never a section's
        # run holds. Stating that here keeps the roadmap's section-history read
        # from decoding the project's whole plan set on every batch pass.
        roadmap_history_scope(()),
        plan_read_scopes(),
        discovery_without_git_dates(),
    ):
        return _queue_batch(
            entries,
            session=session,
            config_for=config_for,
            repo_root=repo_root,
            launch_kind=launch_kind,
            over_fence_reason=over_fence_reason,
            comment=comment,
            local=local,
            priority=priority,
            dispatch_width=dispatch_width,
            dry_run=dry_run,
            queue_answers=queue_answers,
            queued_by=queued_by,
        )


def _queue_batch(
    entries: Sequence[Mapping[str, Any]],
    *,
    session: str,
    config_for,
    repo_root: str | Path,
    launch_kind: str = "cli",
    over_fence_reason: str = "",
    comment: str = "",
    local: bool = False,
    priority: int | None = None,
    dispatch_width: str = "",
    dry_run: bool = False,
    queue_answers: Mapping[
        tuple[str, str], Mapping[str, Mapping[str, Any]]
    ] | None = None,
    queued_by: str = "",
) -> dict[str, Any]:
    """Queue each entry on its own and report one row per entry.

    ``config_for`` answers the flight configuration for one entry's project, so
    a batch spanning projects is queued under each project's own lanes. The
    ``ok`` field is true only when every entry was stored, while a refused
    entry leaves the rest of the batch stored either way.
    """
    from reckon.crew import queue_admission, section_holds
    from reckon.crew.dispatch import queue_requests as queue_one
    from reckon.crew.dispatch_admission import node_context_fit_finding
    from reckon.crew.runs import list_live, new_run_id, read_pointer, run_dir

    root = Path(repo_root).expanduser().resolve()
    plans = _Plans(root)
    try:
        live_pointers = list_live()
    except Exception:  # noqa: BLE001 - an unreadable live set holds nothing
        live_pointers = []
    results: list[dict[str, Any]] = []
    refused = 0

    def refuse(index: int, entry: Mapping[str, Any], detail: str) -> None:
        nonlocal refused
        refused += 1
        results.append(
            {
                "index": index,
                "node": entry.get("node"),
                "project": entry.get("project"),
                "plan": entry.get("plan"),
                "section": entry.get("section"),
                "status": "refused",
                "reason": detail,
            }
        )

    stored_by_node: dict[str, str] = {}
    ordered: list[int]
    try:
        ordered = list(_batch_order(entries))
    except CrewError as exc:
        # A cycle is refused whole: no entry of it can be ordered, so none is
        # stored and every member is reported by name.
        for index, entry in enumerate(entries):
            refuse(index, entry, str(exc))
        cycle: dict[str, Any] = {
            "ok": False,
            "queued": 0,
            "refused": refused,
            "results": results,
        }
        if dry_run:
            cycle["dry_run"] = True
            cycle["would_queue"] = 0
        return cycle

    defaults = {
        "over_fence_reason": over_fence_reason,
        "comment": comment,
        "local": local,
        "priority": priority,
    }
    reads = _QueueReads(
        session=session,
        repo_root=root,
        config_for=config_for,
        live=live_pointers,
        plans=plans,
    )

    def _answer(project: str, node: TaskNode, entry: Mapping[str, Any]) -> Any:
        """The read's answer for one entry's section, or ``None`` when absent."""
        if not node.plan.strip() or not str(node.section or "").strip():
            return None
        supplied = (queue_answers or {}).get((project, node.plan))
        waived = bool(entry.get("allow_unreviewed_plan"))
        if supplied is not None:
            from reckon.crew.node import normalize_section

            for identity, row in supplied.items():
                if normalize_section(identity) != normalize_section(node.section):
                    continue
                if waived and row.get("reason") == QUEUE_ANSWER_REVIEW:
                    # The entry carries the review waiver, and the waiver is
                    # the answer to a review question: the read's own answer
                    # for any other reason stands.
                    return {"section": identity, "may": True, "reason": QUEUE_ANSWER_MAY}
                return row
            return None
        return reads.answer(
            project,
            node.plan,
            node.section,
            allow_unreviewed=waived,
        )

    for index in ordered:
        entry = entries[index]
        try:
            project, node, derived = _entry_node(
                index, entry, plans=plans, defaults=defaults
            )
            options = _entry_options(index, entry, defaults)
            # An entry waits on another entry of this batch by that entry's node
            # id; the stored request is named by its run id, which the earlier
            # entry's row carries.
            after: list[str] = []
            for ref in node.after:
                if ref in stored_by_node:
                    after.append(stored_by_node[ref])
                else:
                    after.append(ref)
            node.after = after

            config = config_for(project)
            backend = str(entry.get("backend") or "")
            run_id = new_run_id(node.id)
            # A queued request names the manifest path its worker delivers to,
            # exactly as a single dispatch stores it, so a coordinator can edit
            # the request while it waits: the queue edit judges every request on
            # that contract, and holds its own run directory as the durable
            # place for it.
            node.manifest_path = node.manifest_path or str(
                run_dir(run_id) / "manifest.md"
            )
            # A request larger than its lane fits in the queue and is refused
            # at the launch that first tries it, where the refusal costs the
            # queue position and reaches nobody. It is measured here instead,
            # before `queue_one` writes a pointer, so the row that comes back
            # names the estimate and the window the entry could not meet.
            work_repository = plans.read(project, node.plan).repo_root
            # A node a run has already delivered is not new work: queueing it
            # again is the duplicate a deep queue exists to avoid. The hold is
            # per node, so the section's other nodes are unaffected, and the
            # answer names the run that holds this one.
            held = section_holds.held_node_rows(
                project,
                work_repository,
                live_pointers,
                plan=node.plan,
                section=node.section,
                node=node.id,
            )
            if held:
                results.append(
                    {
                        "index": index,
                        "node": node.id,
                        "project": project,
                        "plan": node.plan,
                        "section": node.section,
                        "status": "held",
                        "held_by": [dict(row) for row in held],
                    }
                )
                continue
            fit = node_context_fit_finding(
                node,
                project=project,
                repository=work_repository,
                config=config,
                backend=backend,
            )
            if fit is not None:
                refuse(index, entry, str(fit["detail"]))
                continue
            # The batch stays inside the per-section readiness read: queuing
            # each entry through the project-wide view would build every plan
            # in the project once per entry, which is what the batch exists to
            # avoid. The review contract is stamped per stored request exactly
            # as the single-dispatch queue path stamps it.
            stored = queue_one(
                [node],
                project=project,
                session=str(entry.get("session") or session),
                repo_root=work_repository,
                config=config,
                backend_name=backend,
                launch_kind=launch_kind,
                run_ids={node.id: run_id},
                allow_unreviewed_plan=bool(entry.get("allow_unreviewed_plan")),
                local=options["local"],
                comment=options["comment"],
                priority=options["priority"],
                # An entry may state its own width; otherwise it is the width
                # of the call that named the batch.
                dispatch_width=str(entry.get("dispatch_width") or dispatch_width),
                dry_run=dry_run,
                queued_by=str(entry.get("queued_by") or queued_by),
            )
            if not dry_run:
                for stored_id in stored:
                    pointer = read_pointer(stored_id)
                    if isinstance(pointer, dict):
                        queue_admission.record_review_contract(pointer)
                    if derived and stored_id == run_id:
                        # A request already queued keeps the node it was stored
                        # with, so its marks stay the ones that node was built
                        # under.
                        _stamp_section_derived(stored_id, derived)
            _require_stored(stored, project, node)
            actual_id, row = next(iter(stored.items()))
            phase = str(row.get("phase") or "")
            stored_by_node[node.id] = actual_id
            already = actual_id != run_id
            waits = list(row.get("waits") or ())
            # What the waits resolve to, read through the same derivation the
            # admission pass uses. A real dispatch has already applied it to the
            # pointer; a preview needs it to answer with the row the dispatch
            # would report, which is what makes a dry run worth reading.
            would_phase = ""
            findings: list[str] = []
            if dry_run or phase in WAITING_UPSTREAM_PHASES:
                would_phase, findings = _wait_verdict(
                    waits,
                    project=project,
                    plan=node.plan,
                    section=node.section,
                    docs_dir=Path(work_repository) / "docs",
                    live=live_pointers,
                )
            if findings and not dry_run:
                _record_wait_findings(actual_id, findings)
            if dry_run:
                # A preview names what the entry resolved to and stores nothing.
                status = "would-queue"
            elif already:
                status = "already-queued"
            else:
                status = (
                    "awaiting-upstream" if phase in WAITING_UPSTREAM_PHASES else phase
                )
            answer = _answer(project, node, entry)
            reported = {
                key: row[key]
                for key in ("reason", "detail", "plan_reviews")
                if key in row
            }
            if answer is not None:
                # The read's answer is reported on every row, in its own
                # words, so a caller comparing a single-section dispatch with
                # the whole-plan dispatch and with the read itself compares one
                # vocabulary. A section the read says may not be queued says so
                # in ``reason``, where a reader looks for the refusal.
                reported["queue_answer"] = answer["reason"]
                if not answer["may"]:
                    reported["reason"] = answer["reason"]
            results.append(
                {
                    "index": index,
                    "node": node.id,
                    "project": project,
                    "plan": node.plan,
                    "section": node.section,
                    "status": status or "queued",
                    "run_id": actual_id,
                    "position": row.get("position"),
                    "waits": waits,
                    # The phase the waits justify, reported beside a preview's
                    # ``would-queue`` status so the preview names the same
                    # decision the dispatch records rather than a bare promise
                    # to queue.
                    "would_phase": would_phase,
                    "wait_holds": findings,
                    **reported,
                }
            )
        except (BatchEntryRefusedError, CrewError) as exc:
            refuse(index, entry, str(exc))

    results.sort(key=lambda row: int(row["index"]))
    accepted = len(results) - refused
    # A held entry stored no request, so it is neither queued nor refused: it is
    # answered, and the answer is the run that already holds its node.
    held_count = sum(1 for row in results if row.get("status") == "held")
    payload: dict[str, Any] = {
        "ok": refused == 0,
        "queued": accepted - held_count,
        "held": held_count,
        "refused": refused,
        "results": results,
    }
    if dry_run:
        # A preview stores nothing, so it reports what it would have queued
        # rather than claiming a queue depth it never wrote.
        payload["dry_run"] = True
        payload["queued"] = 0
        payload["would_queue"] = accepted
    return payload
