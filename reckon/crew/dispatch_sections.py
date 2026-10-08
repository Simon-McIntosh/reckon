# ruff: noqa: I001, UP035
from __future__ import annotations
import re
import subprocess
from pathlib import (
    Path,
)
from typing import (
    Any,
    Mapping,
)
from reckon import (
    ledger,
)
from reckon._plan_html import (
    _strip_tags,
    plan_headings,
    section_id_candidates,
    section_prose,
)
from reckon.crew.node import (
    CrewError,
    PlanVisibilityError,
    TaskNode,
    repository_identity,
)
from reckon.crew.routing import (
    mounted_repository_projects,
    resolve_dispatch_authority,
    resolve_section_routing,
)


# Twenty-two words can still be one compact evidence pointer naming a test
# path, symbol, unit, and numeric threshold. The twenty-third word is where the
# text stops being that pointer and becomes a reproduced passage. Report rather
# than refuse: short shared facts are the desired way to point back to a plan,
# and making an advisory overlap check block dispatch would invite disabling it.
DONE_WHEN_PLAN_TEXT_SPAN_WORDS = 23


def project_mount_repository(project: str) -> Path | None:
    """Return the repository root registered for one project's docs mount.

    The answer comes from the same mounted-project map every other scope
    resolution consults, so a project's mount has one definition rather than a
    second spelling here.
    """
    for repository, projects in mounted_repository_projects().items():
        if project in projects:
            return repository
    return None


def resolve_project_repository(
    project: str, repo: str | Path | None, *, flag: str = "--repo"
) -> Path:
    """Return the repository root one project's work is written in.

    The project's registered mount decides it, never the repository enclosing
    the caller's working directory. A caller that names no repository is given
    the mount, because a dispatch run from another checkout otherwise cuts its
    worktree from the wrong repository and the worker then finds none of its
    declared write paths. A named repository is admitted when it resolves to
    the same repository as the mount — a linked worktree shares the mount's git
    common directory, so it is one repository under two paths, which is what
    lets a coordinator dispatch from inside a worktree. Anything else is
    refused before a worktree, pointer or ledger row exists, naming both
    resolved roots and the flag so the caller can correct one of them.
    """
    mount = project_mount_repository(project)
    if repo is None:
        if mount is None:
            raise CrewError(
                f"project {project!r} has no registered mount, so {flag} must "
                "name the repository its work is written in"
            )
        return mount
    named = Path(repo).expanduser().resolve()
    if mount is None:
        return named
    if repository_identity(named) == repository_identity(mount):
        # One repository under two paths: the mount is the canonical root, and
        # the caller's worktree names the same repository rather than a second
        # one, so the work is still cut from the mount.
        return mount
    raise CrewError(
        f"{flag} {named} is not the repository registered for project "
        f"{project!r} ({mount}); the project's mount decides where its work is "
        f"written, so name {mount} or omit {flag}"
    )


def _normalised_words(text: str) -> tuple[list[str], list[str]]:
    """Return comparison words and their readable spellings from HTML or prose."""
    from bs4 import BeautifulSoup

    plain = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    displayed = re.sub(r"\s+", " ", plain).strip().split()
    return [word.casefold() for word in displayed], displayed


def _section_heading_matches(heading, requested: str, ids: set[str]) -> bool:
    """Return whether one heading identifies the requested authored section."""
    if heading.identity in ids or str(heading.raw_id or "").casefold() in ids:
        return True
    text = re.sub(r"\s+", " ", heading.text).casefold()
    return text == requested or bool(
        re.match(rf"^{re.escape(requested)}(?:\s|[-—:])", text)
    )


def _plan_section_text(html_text: str, section: str) -> str | None:
    """Extract one section's visible text without including its successors.

    The requested spelling resolves to a heading record. An authored plan
    section — a level-two heading the section-prose reader serves — takes its
    text from ``section_prose``, the one reader the review digests and the
    section view share. A heading that reader does not serve as a unit, such as
    a nested subsection or the document title, keeps its own extent's prose, so
    the level bound is unchanged. The branch for an identified element that is
    not a heading is unchanged.
    """
    from bs4 import BeautifulSoup

    requested = re.sub(r"\s+", " ", section.strip()).casefold()
    if not requested:
        return None
    ids = section_id_candidates(requested)
    headings = plan_headings(html_text)
    soup = BeautifulSoup(html_text, "html.parser")
    identified = next(
        (
            tag
            for tag in soup.find_all(id=True)
            if str(tag.get("id") or "").casefold() in ids
        ),
        None,
    )
    if identified is not None:
        heading = next(
            (item for item in headings if item.raw_id == identified.get("id")), None
        )
        if heading is None:
            return identified.get_text(" ", strip=True)
    else:
        heading = next(
            (
                item
                for item in headings
                if _section_heading_matches(item, requested, ids)
            ),
            None,
        )
    if heading is None:
        return None
    parts = [
        prose
        for identity, prose in section_prose(html_text)
        if identity == heading.identity
    ]
    if parts:
        return " ".join(parts)
    return _strip_tags(html_text[slice(*heading.span)])


def _resolved_plan_section_text(
    *,
    node: TaskNode,
    project: str,
    authority: Mapping[str, Any],
    plan_commit: str,
) -> str | None:
    """Read the named section from the resolved plan's committed blob."""
    from reckon.resources import resolve_resource

    plan_data = authority["plan"]
    plan_repo = Path(str(plan_data["repository"])).resolve()
    docs_dir = Path(str(plan_data["docs"])).resolve()
    resource = resolve_resource(
        docs_dir, project, node.plan, "plan", include_archived=False
    )
    if resource is None:
        return None
    relative_path = resource.path.resolve().relative_to(plan_repo)
    blob = subprocess.run(
        ["git", "show", f"{plan_commit}:{relative_path.as_posix()}"],
        cwd=str(plan_repo),
        capture_output=True,
        text=True,
        check=False,
    )
    if blob.returncode:
        return None
    return _plan_section_text(blob.stdout, node.section)


def _dispatch_section_routing(
    config: Mapping[str, Any],
    *,
    node: TaskNode,
    project: str,
    repo: str | Path | None,
    authority: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Resolve a node's routing from its plan section's own typed record.

    The record declares capability and executable run history supplies the
    attempt count. A section at the threshold resolves on the raised class's
    lane, and the payload's summary names the count that caused it.

    A node whose plan or section cannot be read here resolves through role
    routing alone: the visibility gates downstream remain the authority for
    refusals, so a lane lookup that cannot see the plan — no repository, no
    mount, a record the parser rejects — must not become a new refusal point,
    nor a reason a node that dispatches today stops dispatching. A rule that
    was read and could not be resolved is the one failure that fallback would
    otherwise hide, so it comes back as a routing failure record rather than as
    nothing: the node still dispatches on role routing, and the record says the
    raise was attempted and lost.
    """
    from reckon.resources import ResourceCollision, resolve_resource

    if not node.section.strip() or not node.plan.strip():
        return None
    if repo is None and authority is None:
        return None
    try:
        resolved_authority = dict(
            authority
            or resolve_dispatch_authority(project, Path(str(repo)).resolve())
        )
        docs_dir = Path(str(resolved_authority["plan"]["docs"])).resolve()
        resource = resolve_resource(
            docs_dir, project, node.plan, "plan", include_archived=False
        )
        if resource is None:
            return None
        return resolve_section_routing(config, node=node, plan_path=resource.path)
    except (
        CrewError,
        ledger.LedgerError,
        PlanVisibilityError,
        ResourceCollision,
        OSError,
        ValueError,
    ) as exc:
        return _section_routing_failure(node.section, exc)


def _section_routing_failure(section: str, exc: BaseException) -> dict[str, Any]:
    """Return the record a dispatch carries when a section's raise cannot resolve.

    The failure names the exception class and the section, because the two cases
    it separates — a rule that raised and a section that carries no rule — both
    end on role routing and would otherwise leave identical evidence. The detail
    is the exception's own message, so a reader sees what the parser or the
    lookup actually said rather than a paraphrase of it.
    """
    label = str(section or "section")
    exception = type(exc).__name__
    detail = str(exc).strip()
    summary = f"{label}: the raise could not be resolved ({exception})"
    if detail:
        summary += f": {detail}"
    summary += "; dispatch fell back to role routing"
    return {
        "failure": {
            "section": str(section),
            "exception": exception,
            "detail": detail,
        },
        "summary": summary,
    }


def _section_routing_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Trim a routing payload to the section facts a dispatch record carries.

    The ``failure`` key is written on every outcome rather than left off when
    the rule resolved cleanly: absent would read as a section whose rule was
    never read, which is the state ``section_routing: None`` already names.
    """
    failure = payload.get("failure")
    if failure is not None:
        return {"failure": dict(failure), "summary": payload["summary"]}
    return {
        "attempts": payload["attempts"],
        "capability": payload["capability"],
        "raise": payload["raise"],
        "summary": payload["summary"],
        "failure": None,
    }


def _longest_contiguous_word_span(left: str, right: str) -> tuple[int, str]:
    """Return the length and readable text of the longest shared word run."""
    left_words, left_display = _normalised_words(left)
    right_words, _right_display = _normalised_words(right)
    previous = [0] * (len(right_words) + 1)
    best_length = 0
    best_end = 0
    for left_index, left_word in enumerate(left_words, start=1):
        current = [0] * (len(right_words) + 1)
        for right_index, right_word in enumerate(right_words, start=1):
            if left_word != right_word:
                continue
            current[right_index] = previous[right_index - 1] + 1
            if current[right_index] > best_length:
                best_length = current[right_index]
                best_end = left_index
        previous = current
    start = best_end - best_length
    return best_length, " ".join(left_display[start:best_end])


def _done_when_plan_overlap_warning(
    *,
    node: TaskNode,
    project: str,
    authority: Mapping[str, Any],
    plan_commit: str,
) -> str | None:
    """Quote copied plan prose while remaining unable to break a dispatch."""
    try:
        section_text = _resolved_plan_section_text(
            node=node,
            project=project,
            authority=authority,
            plan_commit=plan_commit,
        )
        if section_text is None:
            return None
        length, span = _longest_contiguous_word_span(section_text, node.done_when)
    except Exception:  # noqa: BLE001 - an advisory report cannot stop dispatch
        # This report is advisory. The existing visibility guard remains the
        # authority for dispatchability; failure to produce an extra warning
        # must not create a new refusal or alter an otherwise valid launch.
        return None
    if length < DONE_WHEN_PLAN_TEXT_SPAN_WORDS:
        return None
    return (
        f"done-when reproduces a {length}-word contiguous span from plan "
        f"{node.plan!r} section {node.section!r}: \u201c{span}\u201d"
    )
