---
name: reckon-reset
description: >-
  Broadcast an orchestrator usage-limit reset so that every live coordinator
  session resumes: record the reset once, confirm the monitor published it, and
  message every live session directly. Use whenever the lead says the limits
  have reset, the weekly limit reset, the subscription was switched or the
  account changed so sessions can continue, or asks to broadcast or resend a
  reset. Trigger phrases: "limits reset", "limits reset, continue", "switched
  sub", "new login, carry on", "broadcast the reset", "/reckon-reset". Worker
  lanes, holds and lifts are out of scope.
allowed-tools: Bash(*) ListAgents SendMessage
---

# reckon-reset — make sure every live session hears the reset

A reset is only useful if the sessions stopped at the limit hear it. The monitor
row is one channel and it has failed silently: on 14 November a reset was
recorded and counted as reaching seventeen panes, and no pane drew it, because
the row was written only to the producers' streams while every pane read the
station. The registration-based "unreached" list is not a list of live sessions
either: it names every session that ever held a follower registration, almost
all of them long ended. So this skill does three things, every time, in order.

## 1. Record the reset once

Use the lead's own words as the reason. Use `--clock seven_day` when the lead
names the weekly limit, `five_hour` otherwise. Switching to another
subscription or account counts as a reset of the clock the lead names.

```bash
reckon crew budget --reset --used --group claude-orchestrators \
  --clock five_hour --reason "<the lead's words>"
```

Read the JSON it prints and keep the event `id`. A repeat for the same clock
within ten minutes records nothing new, so a second announcer is harmless.
Never touch the worker lane: no `--lift`, no change to `claude-sub`, no lane
named on any dispatch because of a reset.

## 2. Confirm the monitor published it

Within one station tick your own pane draws a `limit` row naming
`claude-orchestrators`. Confirm it from the station's events rather than by
waiting for the pane:

```bash
.venv/bin/python - <<'EOF'
from reckon.crew import station_store
store = station_store.StationStore.at_runtime_root()
rows = [e for e in store.events() if e.get("to_state") == "limit-reset"]
for row in rows[-6:]:
    print(row.get("time"), row.get("project"), row.get("audience"), row.get("detail"))
EOF
```

One row per mounted project, audience `*`, naming the event id from step 1,
means the broadcast reached every station-fed pane. No row within two minutes
means the monitor path is broken: say so in the report, and step 3 still covers
every session.

## 3. Message every live session directly

The session listing, not the registration list, is the authority on who is
live. Call `ListAgents` and send each listed peer the same message with
`SendMessage`, addressed by the name the listing prints:

> Usage limits have reset (<the lead's words>, <UTC time>, <clock>): resume
> your work now. If you stopped at the usage limit, re-read your obligations
> and continue where you left off, dispatching included. This covers
> orchestrator sessions only; worker lanes, holds and lifts are unchanged.

Send to every peer, busy or idle: a message waits in the inbox of a busy
session and is read at its next tool round. A send that returns a delivery
notice saying the session holds or refuses messages is reported by name, so
the lead can resume it by hand.

## 4. Report

State, in four lines: the reset event id and clock; how many station panes the
row reached (step 2), or that it did not publish; how many live sessions were
messaged and their names; and any session that could not be reached. Do not
report the registration "unreached" count as sessions missed: it is the history
of ended sessions, not a list of live ones.

## When a session receives the reset

A session that sees a `limit` row naming `claude-orchestrators`, or this
message, continues: it re-reads its obligations view and carries on where it
stopped, dispatching included. The reset never changes a worker lane.
