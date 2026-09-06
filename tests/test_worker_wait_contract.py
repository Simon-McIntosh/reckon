"""The declared external wait is documented where workers are told how to behave.

The machinery predates the instruction: `reckon/crew/recovery.py` reads four
fields and a waiting status from a manifest and classifies such a run as
waiting, and the recovery sweep resumes it once its probe reports a terminal
value. But the reference that tells workers how to behave never mentioned any of
it, so a worker that submitted a compute job polled inside its turn instead —
paying a full context re-send per check. These tests bind the documented
instruction and field names to the code that actually parses them, so a rename
on either side fails here rather than drifting silently.
"""

from pathlib import Path

ROOT = Path(__file__).parents[1]

PROTOCOL = ROOT / "skills" / "reckon-ship" / "references" / "worker-protocol.md"
RECOVERY = ROOT / "reckon" / "crew" / "recovery.py"

# The fields a declared external wait must name, plus the one the parser treats
# as optional. These are the strings `_manifest_wait` in recovery.py actually
# reads; the reference may not drop one and may not invent a carrier the parser
# ignores.
REQUIRED_WAIT_FIELDS = (
    "wait_condition",
    "wait_probe",
    "wait_terminal",
    "resume_brief",
)
OPTIONAL_WAIT_FIELDS = ("wait_started_at",)


def normalized(text: str) -> str:
    return " ".join(text.split())


def test_worker_protocol_documents_the_declared_external_wait() -> None:
    protocol = normalized(PROTOCOL.read_text())
    recovery = RECOVERY.read_text()

    # The status value that turns a manifest into a declared wait is documented
    # in the reference and is the exact value the classifier requires.
    assert "status: waiting | complete | blocked | failed" in protocol
    assert 'WAITING_STATUS = "waiting"' in recovery

    # Every field the parser reads is documented, and every documented field is
    # the string the parser looks up — asserted in both directions so a rename
    # or an invented alias fails here rather than documenting a field the code
    # ignores.
    for name in (*REQUIRED_WAIT_FIELDS, *OPTIONAL_WAIT_FIELDS):
        assert f"`{name}`" in protocol, f"the reference omits `{name}`"
        assert f'manifest_data.get("{name}")' in recovery, (
            f"`{name}` is documented but the parser never reads it"
        )

    # The semantics the worker must rely on are stated, not merely implied.
    assert "runs without a shell" in protocol
    assert "matched, case-insensitively" in protocol
    assert "`exit:<code>`" in protocol
    assert "write the manifest once and leave it alone" in protocol


def test_worker_protocol_states_when_the_wait_declaration_applies() -> None:
    protocol = normalized(PROTOCOL.read_text())

    # The instruction: submitting a job the worker does not control means
    # declaring the wait and exiting, never polling from inside the turn.
    assert "submits a job it does not control" in protocol
    assert "write the wait into the manifest and exit" in protocol
    assert "rather than polling the job from inside the turn" in protocol
    assert "reads as waiting rather than as working or stalled" in protocol
    # The wake: the same sweep that lifts a provider hold wakes a declared wait.
    assert "resume-ready" in protocol
    assert "the moment the probe reports a terminal value" in protocol

    # The reason is stated plainly, with the cost mechanism rather than a rule.
    assert "Every check of a running job is a full model round-trip" in protocol
    assert "re-sends the whole context" in protocol
    assert "a metered lane" in protocol
    assert "a dated illustration" in protocol

    # And the boundary: a short bounded in-turn wait stays the simpler option;
    # the declaration is for waits long enough that polling costs more than a
    # resume.
    assert "A short bounded wait inside a turn" in protocol
    assert "polling them costs more than a resume" in protocol
