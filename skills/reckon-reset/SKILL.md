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
every project's pane, and a session that draws it resumes. A session whose
follower is not live draws nothing, so the reset names those sessions and this
skill sends each of them the same announcement once, at the moment it is
recorded. Nothing follows it up.

## No follow-ups

A reset is one message, sent once. Apart from step 2's single delivery to the
sessions the monitor cannot reach, never relay it, message sessions about it
later, re-announce it, or schedule anything on a five-hour or any other clock. By the
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

Keep the event `id` the JSON prints, and its `reach`: `reached` lists the
sessions whose live follower will draw the row, `unreached` the live sessions
it cannot reach, and `unreached_names` the name each of those answers to. A repeat for the same clock within ten
minutes records nothing new, so a second announcer is harmless. Never touch the
worker lane: no `--lift`, no change to `claude-sub`.

## 2. Tell the sessions the monitor cannot reach, once

For each name in `unreached_names`, send one peer message now, in the same
words as the reset (for example "limits reset (five_hour) at <time> UTC: <the
lead's words>; orchestrator sessions may continue"). Send it in the same turn as
step 1 and never again. A session listed in `unreached` with no name has no live
process to message; leave it.

## 3. Confirm the station published it

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

## 4. If it has not published

Wait for the station's current tick to finish (`tick_started_at` and
`tick_finished_at` in the station store) before concluding anything. If a full
tick passes with no row naming this reset, report it to the lead as a monitor
defect and stop there. Do not message the reached sessions as a fallback: a
direct copy sent late is exactly the stale follow-up this skill exists to
prevent.

## 5. Report

One line: the reset id and clock, how many sessions the monitor reaches and
which were messaged directly, and either "published to N projects" or "not
published after a full tick", naming the defect.

## When a session sees the reset

A `limit` row naming `claude-orchestrators` means continue: re-read the
obligations view and carry on, dispatching included. Do not relay it to other
sessions; the monitor already did. The reset never changes a worker lane.
