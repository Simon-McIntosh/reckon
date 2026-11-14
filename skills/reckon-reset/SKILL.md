---
name: reckon-reset
description: >-
  Announce an orchestrator usage-limit reset through the monitor and confirm
  every pane was sent it. Use whenever the lead says the limits have reset, the
  weekly limit reset, or the subscription or account was switched so sessions
  can continue, or asks to broadcast or resend a reset. Trigger phrases:
  "limits reset", "limits reset, continue", "switched sub", "new login, carry
  on", "broadcast the reset", "/reckon-reset". Worker lanes, holds and lifts are
  out of scope.
allowed-tools: Bash(*) ListAgents SendMessage
---

# reckon-reset — announce a reset through the monitor, and check it went out

The monitor is the channel: the station publishes the reset as a `limit` row to
every project's pane, and a session that draws it resumes. This skill records
the reset, confirms the station published it, and messages sessions directly
only when it did not.

## 1. Record the reset

Use the lead's own words as the reason, and `--clock seven_day` when the lead
names the weekly limit (`five_hour` otherwise). Switching subscription or
account counts as a reset of that clock.

```bash
reckon crew budget --reset --used --group claude-orchestrators \
  --clock five_hour --reason "<the lead's words>"
```

Keep the event `id` the JSON prints. A repeat for the same clock within ten
minutes records nothing new, so a second announcer is harmless. Never touch the
worker lane: no `--lift`, no change to `claude-sub`.

## 2. Confirm the station published it

Within a station tick there is one row per mounted project, audience `*`, whose
detail names the reset:

```bash
.venv/bin/python - <<'EOF'
from reckon.crew import station_store
store = station_store.StationStore.at_runtime_root()
rows = [e for e in store.events() if e.get("to_state") == "limit-reset"]
for row in rows[-12:]:
    print(row.get("time"), row.get("project"), row.get("audience"), row.get("detail"))
EOF
```

Your own pane draws the same `limit` row.

## 3. Only if it did not publish

If no row naming this reset appears within two minutes, the monitor path is
broken. Then, and only then, call `ListAgents` and send each listed peer one
`SendMessage`:

> Usage limits have reset (<the lead's words>, <UTC time>, <clock>): resume your
> work now. Orchestrator sessions only; worker lanes are unchanged.

Report the broken broadcast as a monitor defect.

## 4. Report

One line: the reset id and clock, and either "published to N projects" or "not
published; messaged M live sessions directly". The registration-based
`unreached` list in the reset's JSON is not a list of live sessions and is not
reported as sessions missed.

## When a session sees the reset

A `limit` row naming `claude-orchestrators` means continue: re-read the
obligations view and carry on, dispatching included. The reset never changes a
worker lane.
