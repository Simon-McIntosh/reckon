---
name: reckon-reset
description: >-
  Announce an orchestrator usage-limit reset through the monitor, once. Use
  whenever the lead says the limits have reset, the weekly limit reset, or the
  subscription or account was switched so sessions can continue, or asks to
  broadcast a reset. Trigger phrases: "limits reset", "limits reset, continue",
  "switched sub", "new login, carry on", "broadcast the reset", "/reckon-reset".
  Worker lanes, holds and lifts are out of scope.
allowed-tools: Bash(*)
---

# reckon-reset — announce a reset through the monitor, once

The monitor is the channel: the station publishes the reset as a `limit` row to
every project's pane, and a session that draws it resumes. This skill records
the reset and confirms the station published it. It sends nothing else, and
nothing follows it up.

## No follow-ups

A reset is one message. Never relay it, message sessions directly about it,
re-announce it, or schedule anything on a five-hour or any other clock. By the
time a follow-up arrives the lead may have switched account again, and the
second message describes a state that no longer holds. The station can take
several minutes to publish when its tick is slow; a late row is not a missing
one.

## 1. Record the reset

Use the lead's own words as the reason, and `--clock seven_day` when the lead
names the weekly limit (`five_hour` otherwise). Switching subscription or
account counts as a reset of that clock. The clock names which limit was
cleared; it schedules nothing.

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

## 3. If it has not published

Wait for the station's current tick to finish (`tick_started_at` and
`tick_finished_at` in the station store) before concluding anything. If a full
tick passes with no row naming this reset, report it to the lead as a monitor
defect and stop there. Do not message sessions yourself: a direct copy sent
late is exactly the stale follow-up this skill exists to prevent.

## 4. Report

One line: the reset id and clock, and either "published to N projects" or "not
published after a full tick", naming the defect.

## When a session sees the reset

A `limit` row naming `claude-orchestrators` means continue: re-read the
obligations view and carry on, dispatching included. Do not relay it to other
sessions; the monitor already did. The reset never changes a worker lane.
