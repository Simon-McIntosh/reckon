"""Every spelling of work sent onward is read, and refused at every body write.

Work found beyond a node is a plan section or an instruction in the dispatching
prompt, never prose that parks it. Reckon already refused the hyphenated
``follow-on`` spelling; the census of live plans shows the same intent written
as ``carry to §N``, ``go to §N``, ``followon``, ``follow on``, and as a verb of
onward motion aiming at a follow-up, later, next or separate node, section,
plan or figure. This module holds both readers to the whole set: the audit
warns on a document that already carries any of them, and every site that
writes a comment or followup body refuses one.

A sentence that only cites a landed section (``landed in §4``) carries no
onward verb and stays writable, and a spelling inside ``<code>`` names a symbol
rather than a bucket and stays exempt in both readers.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from click.testing import CliRunner

import reckon._store as _store_module
import reckon.mcp as mcp_module
from reckon import _plan_html
from reckon.cli import main
from tests.mcp_family_reload import reload_mcp_family

PROJECT = "spelling-proj"
PLAN = "spelling-plan"
SUCCESSOR = "spelling-successor"
RULE = "additional work is a plan section or an instruction in the dispatching prompt"
FINDING = "deferred-work-term-in-prose"

DECLARATIONS = {"s1": "done", "s2": "implementable"}

# One phrase per spelling of work sent onward, each a sentence a plan's prose
# has carried in practice. Three are quoted from live power_convertor plans.
SPELLINGS = [
    "Three follow-ons carry to §4",
    "Two follow-ons go to §6",
    "The remainder carries to §6",
    "The remainder carried to §4",
    "The three items carry to §4",
    "The remainder goes to §6",
    "The remainder go to §6",
    "Parked as followon work",
    "Parked as follow on work",
    "the remaining non-blocking items go to a follow-up node",
    "go to a follow-up figure node",
    "go to a separate plan",
    "left for a later node",
    "deferred to a next section",
    "sent to a separate plan",
    "going to a later figure",
]

#: The three phrases quoted from power_convertor's own plans and census brief.
QUOTED = {
    "the remaining non-blocking items go to a follow-up node",
    "go to a follow-up figure node",
    "go to a separate plan",
}

_RECORD = (
    '<section data-reckon="section" data-id="{section}" data-effort-hours="1.0"'
    ' data-capability-version="1.0" data-capability-class="general"'
    ' data-capability-reasoning="standard"'
    ' data-capability-verification="strict"'
    ' data-capability-risk="low"'
    ' data-status="{status}" data-links=""></section>'
)


def _record(section: str, status: str) -> str:
    return _RECORD.format(section=section, status=status)


def _records(declarations: dict[str, str]) -> list[dict]:
    return [
        {
            "id": section_id,
            "effort_hours": 1.0,
            "capability": {
                "version": "1.0",
                "class": "general",
                "requirements": {
                    "reasoning": "standard",
                    "verification": "strict",
                    "risk": "low",
                },
            },
            "attempts": 0,
            "status": status,
            "links": [],
        }
        for section_id, status in declarations.items()
    ]


def _scaffold(declarations: dict[str, str]) -> str:
    """One authored heading, typed record and prose span per declared section."""
    parts = []
    for section_id, status in declarations.items():
        parts.append(f'<h2 id="{section_id}">§ {section_id}</h2>')
        parts.append(_record(section_id, status))
        parts.append(f"<p>Body for {section_id}.</p>")
    return "".join(parts)


def _authored(slug: str, body: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        f"<title>{slug}</title></head><body>"
        '<main class="plan-doc">'
        f"{body}"
        "</main></body></html>"
    )


def _write_plan(
    checkout: Path,
    extra: str = "",
    *,
    slug: str = PLAN,
    declarations: dict[str, str] | None = None,
    followups: list[dict] | None = None,
) -> Path:
    """Write one plan whose authored prose is its section scaffolding plus ``extra``."""
    declared = DECLARATIONS if declarations is None else declarations
    path = checkout / "docs" / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    authored = _authored(slug, _scaffold(declared) + extra)
    state = {
        "project": PROJECT,
        "type": "plan",
        "slug": slug,
        "title": slug,
        "status": "active",
        "standalone": "a fixture wired to nothing",
        "modified": "2026-10-23",
        "version": 0,
        "section_declarations": deepcopy(declared),
        "sections": _records(declared),
        "followups": deepcopy(followups or []),
    }
    path.write_text(_plan_html.write_state(authored, state), encoding="utf-8")
    return path


def _comment_section(phrase: str, section: str = "s1") -> str:
    return (
        '<section data-reckon="comments" id="comments"><h2>Comments</h2>'
        f'<div class="r-comment" data-section="{section}" data-id="c-legacy"'
        ' data-who="human" data-when="2026-10-01">'
        f'<div class="r-comment-body"><p>{phrase}</p></div></div></section>'
    )


@pytest.fixture()
def checkout(tmp_path: Path) -> Path:
    """A checkout holding this plan, a successor, and one open followup."""
    root = tmp_path / "repo"
    _write_plan(
        root,
        '<p id="intro">Introductory prose.</p>',
        followups=[
            {
                "id": "f1",
                "status": "open",
                "title": "an open followup",
                "body": "why it is open",
                "written_by": "reckon-build",
                "written_at": "2026-10-01",
                "prompt": f"/reckon-build {PLAN} §2",
            }
        ],
    )
    _write_plan(
        root,
        "",
        slug=SUCCESSOR,
        declarations={"s9": "implementable"},
    )
    return root


def _version(checkout: Path, slug: str = PLAN) -> int:
    path = checkout / "docs" / "plans" / f"{slug}.html"
    return _plan_html.read_state(path.read_text(encoding="utf-8"))["version"]


def _edit(checkout: Path, ops: list[dict], *, slug: str = PLAN) -> dict:
    return mcp_module._edit_plan_tool(
        PROJECT,
        slug,
        doc_type="plan",
        checkout_path=str(checkout),
        ops=ops,
        expected_version=_version(checkout, slug),
    )


def _append_comment_op(body: str, section: str = "s1") -> dict:
    return {
        "op": "append",
        "target": "comments",
        "section": section,
        "item": {
            "id": "c-new",
            "who": "human",
            "when": "2026-10-02",
            "body": body,
        },
    }


def _append_followup_op(body: str) -> dict:
    return {
        "op": "append",
        "target": "followups",
        "item": {
            "id": "f-new",
            "written_by": "reckon-build",
            "written_at": "2026-10-02",
            "title": "a new item",
            "body": body,
            "prompt": f"/reckon-build {PLAN} §2",
        },
    }


def _audit(path: Path, tmp_path: Path) -> str:
    result = CliRunner().invoke(
        main,
        ["audit-doc", str(path)],
        env={"RECKON_HOME": str(tmp_path / "config-home")},
    )
    assert result.exit_code == 0, result.output
    return result.output


# ── the audit reads every spelling ─────────────────────────────────────────


@pytest.mark.parametrize("phrase", SPELLINGS)
def test_audit_doc_reports_each_spelling_in_a_comment_body(
    phrase: str, tmp_path: Path
) -> None:
    """Each spelling sits in its own plan: the audit reports the first term only."""
    root = tmp_path / "repo"
    path = _write_plan(root, _comment_section(phrase))

    output = _audit(path, tmp_path)

    assert FINDING in output, output
    assert "plan prose carries" in output, output


def test_audit_doc_reports_the_quoted_power_convertor_phrases(
    tmp_path: Path,
) -> None:
    """The three phrases the plan quotes are the ones the rule now covers."""
    for index, phrase in enumerate(sorted(QUOTED)):
        root = tmp_path / f"repo-{index}"
        path = _write_plan(root, _comment_section(phrase))

        output = _audit(path, tmp_path)

        assert FINDING in output, (phrase, output)


def test_audit_doc_is_silent_on_a_landed_section_citation(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    path = _write_plan(root, _comment_section("The rename landed in §4."))

    output = _audit(path, tmp_path)

    assert FINDING not in output, output


def test_audit_doc_is_silent_when_the_spelling_is_a_code_citation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    path = _write_plan(
        root,
        _comment_section("The helper <code>carried to §4</code> renames the field."),
    )

    output = _audit(path, tmp_path)

    assert FINDING not in output, output


# ── the store refuses each spelling at every body write ────────────────────


def test_a_comment_append_naming_a_new_spelling_is_refused(checkout: Path) -> None:
    path = checkout / "docs" / "plans" / f"{PLAN}.html"
    before = path.read_bytes()

    result = _edit(
        checkout, [_append_comment_op("<p>The remainder carries to §6.</p>")]
    )

    assert result["ok"] is False, result
    assert result["error"] == "op_error", result
    assert RULE in result["detail"], result
    assert "carries to §6" in result["detail"], result
    assert path.read_bytes() == before


def test_the_refusal_quotes_the_matched_text_as_a_string(checkout: Path) -> None:
    """A capture group would put a tuple in the message; the pattern has none."""
    result = _edit(
        checkout, [_append_comment_op("<p>Parked as carried to §4 work.</p>")]
    )

    assert result["ok"] is False, result
    detail = result["detail"]
    assert 'the term "carried to §4" in prose' in detail, detail
    assert "('carried to §4'" not in detail, detail
    assert '("carried to §4"' not in detail, detail


def test_a_section_move_reason_naming_a_new_spelling_is_refused(
    checkout: Path,
) -> None:
    path = checkout / "docs" / "plans" / f"{PLAN}.html"
    before = path.read_bytes()

    result = _edit(
        checkout,
        [
            {
                "op": "set",
                "path": "section_declarations.s2",
                "value": {
                    "status": "descoped",
                    "reason": "carried to §4",
                    "moved_to": f"{SUCCESSOR}#s9",
                },
            }
        ],
    )

    assert result["ok"] is False, result
    assert result["error"] == "op_error", result
    assert "section move's reason" in result["detail"], result
    assert RULE in result["detail"], result
    assert path.read_bytes() == before, "nothing landed behind the refusal"


def test_a_followup_append_naming_a_new_spelling_is_refused(checkout: Path) -> None:
    result = _edit(checkout, [_append_followup_op("<p>Deferred to a later node.</p>")])

    assert result["ok"] is False, result
    assert result["error"] == "op_error", result
    assert RULE in result["detail"], result
    assert "Deferred to a later node" in result["detail"], result


def test_a_followup_body_set_naming_a_new_spelling_is_refused(checkout: Path) -> None:
    path = checkout / "docs" / "plans" / f"{PLAN}.html"
    before = path.read_bytes()

    result = _edit(
        checkout,
        [
            {
                "op": "set",
                "path": "followups.f1.body",
                "value": "<p>Left for a later section.</p>",
            }
        ],
    )

    assert result["ok"] is False, result
    assert result["error"] == "op_error", result
    assert "followups.f1.body" in result["detail"], result
    assert RULE in result["detail"], result
    assert path.read_bytes() == before


def test_a_section_body_set_naming_a_new_spelling_is_refused(checkout: Path) -> None:
    path = checkout / "docs" / "plans" / f"{PLAN}.html"
    before = path.read_bytes()

    result = _edit(
        checkout,
        [
            {
                "op": "set",
                "path": "sections.s2.body",
                "value": "<p>Two items go to §6.</p>",
            }
        ],
    )

    assert result["ok"] is False, result
    assert result["error"] == "op_error", result
    assert "sections.s2.body" in result["detail"], result
    assert RULE in result["detail"], result
    assert path.read_bytes() == before


def test_a_landed_section_citation_is_accepted_in_a_comment(checkout: Path) -> None:
    result = _edit(checkout, [_append_comment_op("<p>The rename landed in §4.</p>")])

    assert result["ok"] is True, result
    text = (checkout / "docs" / "plans" / f"{PLAN}.html").read_text(encoding="utf-8")
    assert "landed in §4" in text


def test_a_code_citation_of_a_spelling_is_accepted_in_a_comment(
    checkout: Path,
) -> None:
    result = _edit(
        checkout,
        [
            _append_comment_op(
                "<p>The helper <code>carried to §4</code> renames it.</p>"
            )
        ],
    )

    assert result["ok"] is True, result
    text = (checkout / "docs" / "plans" / f"{PLAN}.html").read_text(encoding="utf-8")
    assert "<code>carried to §4</code>" in text


def test_a_comment_append_with_no_spelling_is_accepted(checkout: Path) -> None:
    """Control: the clean arm lands, so the refusals above are not constant."""
    result = _edit(checkout, [_append_comment_op("<p>The rename is complete.</p>")])

    assert result["ok"] is True, result


# ── the followup-add operation through append_to_list ──────────────────────


@pytest.fixture()
def store_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A temp docs dir registered as this process's mount for ``PROJECT``."""
    docs_dir = tmp_path / "store-docs"
    docs_dir.mkdir()
    state_root = tmp_path / "state"
    state_root.mkdir()
    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text(json.dumps({PROJECT: str(docs_dir)}))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))
    serve_mod = pytest.importorskip("reckon.serve")
    serve_mod._MOUNTS_FILE = mounts_file
    serve_mod._STATE_ROOT = state_root
    reload_mcp_family()

    path = docs_dir / "plans" / f"{PLAN}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    declared = {"s1": "implementable"}
    state = {
        "project": PROJECT,
        "type": "plan",
        "slug": PLAN,
        "title": PLAN,
        "status": "active",
        "standalone": "a fixture wired to nothing",
        "modified": "2026-10-23",
        "version": 0,
        "section_declarations": declared,
        "sections": _records(declared),
        "followups": [],
    }
    path.write_text(
        _plan_html.write_state(
            _authored(PLAN, _scaffold(declared)),
            state,
        ),
        encoding="utf-8",
    )
    return docs_dir


def _followup_item(body: str) -> dict:
    return {
        "id": "f-mcp",
        "written_by": "reckon-build",
        "written_at": "2026-10-02",
        "title": "a new item",
        "body": body,
        "prompt": f"/reckon-build {PLAN} §1",
    }


def test_the_followup_add_operation_refuses_a_new_spelling(store_setup: Path) -> None:
    with pytest.raises(_store_module.OpError) as refused:
        mcp_module._append_followup(
            PROJECT, PLAN, _followup_item("<p>Sent to a next figure.</p>"), 0
        )

    assert RULE in str(refused.value), refused.value
    assert "Sent to a next figure" in str(refused.value), refused.value

    data, _ = _store_module.read_plan(PROJECT, PLAN)
    assert data.get("followups") == [], "nothing landed behind the refusal"


def test_the_followup_add_operation_accepts_a_clean_body(store_setup: Path) -> None:
    result = mcp_module._append_followup(
        PROJECT, PLAN, _followup_item("the field is renamed"), 0
    )

    assert result["ok"] is True, result
    data, _ = _store_module.read_plan(PROJECT, PLAN)
    assert len(data["followups"]) == 1
