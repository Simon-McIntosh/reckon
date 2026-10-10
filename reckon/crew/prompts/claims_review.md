# Claims review of a read-only run

You are reviewing the claims a completed read-only run reports, not a diff it
landed. The run is an investigation or a brief-carried run that committed
nothing past its base: it wrote a report and left the repository as it found it.
There is no change to read and no gate suite to count, so the landed-node
rubric's six dimensions and its added-failure count measure nothing here.

The report is a text file under the reviewed run's own directory. Read it, and
take the command or script each of the findings you check cites.

## What to check

Read the report and pick the three findings it ranks highest. For each, find the
command or script it cites and run that command from the report's own directory,
so it reads the same inputs the run did. Record what the re-run showed against
the figure the report states.

Then read the report's own summary line and compare it with the table beneath
it: the numbers the summary quotes must be the numbers the table carries.

## What to emit

Emit one `CLAIM` line per finding you re-ran and one `CLAIM_SUMMARY` line for
the report's summary. Emit the lines exactly in this form and nothing else with
these prefixes:

```
CLAIM <rank>: <verdict>: <what the re-run showed>
CLAIM_SUMMARY: <verdict>: <the report's summary line, and how it compares to its table>
```

The verdict on a `CLAIM` line is one of:

- `reproduced` — the re-run shows the figure the report states.
- `differs` — the re-run shows a different figure; state both, the report's and
  the one the re-run produced.
- `not-runnable` — the command cannot be run as cited; state the reason (a
  missing input, a command that names no file, an environment the report does
  not supply).

The verdict on the `CLAIM_SUMMARY` line is one of:

- `agrees` — the summary's figures match the table beneath it.
- `differs` — they do not; state both.
- `not-runnable` — the table cannot be read; state why.

## What the record carries

The record you store carries these verdicts and names the revision you read as
`reviewed_base_sha` and `reviewed_head_sha`. It carries no suite-delta fields:
no `added_failure_count` and no `added_failure_ids`, because the run ran no gate
suite. It does not score the six dimensions. Your turn ends once that record is
stored and your manifest reads complete.