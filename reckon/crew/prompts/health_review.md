# Health review of a neighbourhood of code

You are reviewing a neighbourhood of the codebase — a set of production modules
and the modules one import away from them — not one diff. The brief you are
handed names the neighbourhood's modules, the open findings the findings reader
(`crew(view="findings", path=<prefix>)`) reports for those paths with their
recurrence, the four interface figures `reckon/interface_counts.py` computes,
the clone pairs `reckon/clones.py` reports inside the neighbourhood, and the
neighbourhood modules no entry point reaches.

The review is read-only: you change nothing and run no writer. What it produces
is a rubric verdict per item, findings about the neighbourhood, and **at most one
treatment** — a single repair you would dispatch next. The value of the review is
the treatment: a neighbourhood health review that emits findings and no repair is
the busy work this rubric exists to replace. The treatment is chosen, not
invented, and it is chosen only from the kinds that shrink the codebase rather
than grow it.

## The rubric

Score the neighbourhood against each item below and emit one verdict per item.
Every item is named by the slug in parentheses, because you must emit a verdict
for each:

1. **known_findings** (`known_findings`) — group the brief's open findings into
   defect classes and say which recur. A finding already named in the brief is
   **cited by its findings-reader row, never re-reported as new**: re-reporting a
   known finding is how a review manufactures busy work from its own history.
2. **duplicate_capability** (`duplicate_capability`) — a census **by capability,
   not by text**, across the neighbourhood and the package: every implementation
   of one capability, each named with `path:line`. The clone detector reads
   textual copies; this item reads capability duplication the detector cannot
   see — a re-implementation, in different code, of a capability the repository
   already owns.
3. **unreached** (`unreached`) — the neighbourhood modules no entry point
   reaches. State each unreached module and whether anything live depends on it.
4. **interface_growth** (`interface_growth`) — surface added in the last 14 days
   with nothing retired. Name the added surface and what, if anything, was
   removed in the same window.
5. **width** (`width`) — modules over the worker-window bound. Name each module
   over the bound and its size.
6. **test_debt** (`test_debt`) — duplicated helpers and source-text assertions in
   the neighbourhood's tests. Name the duplicated helper and the assertion that
   reads source text rather than behaviour.

RUBRIC_ITEMS: known_findings, duplicate_capability, unreached, interface_growth, width, test_debt

## What to read

Read the brief first: it carries the modules, the open findings with their
recurrence, the interface figures, the clone pairs and the unreached modules.
Then read the neighbourhood's own source and its tests. Every claim you make
cites a `path:line` you opened, not a guess from a name.

Stay in the neighbourhood. A design you prefer is not a defect; a defect is a
capability duplicated, a module unreached, an interface grown without a
retirement, a module over the bound, or a helper copied. Do not re-derive the
whole repository, and do not review code the neighbourhood does not reach.

## What to emit

For each of the six items, emit one `RUBRIC` line. Then emit one `FINDING` line
per defect, each carrying its class. Then emit **exactly one** `TREATMENT` line.

```
RUBRIC <item>: <pass, or the finding it produced — one sentence citing the path or line it is about>
FINDING <item> <file>:<line> — CLASS: <defect class> — <what is wrong and why it matters> — CITES: <the findings-reader row it is already known from, or new>
TREATMENT — GOAL: <one line> — WRITE_PATHS: <path>, <path> — DONE_WHEN: <one line measuring the repair, naming its negative control> — EXPECTED_DELTA: <the net change in lines, signed>
```

**Every item needs a `RUBRIC` line.** An omitted item reads exactly like a
checked one, which is how a review skips a step and still looks thorough, so an
omitted item is reported absent rather than silently taken as checked.

**A finding already in the brief is cited, not re-reported.** Name the
findings-reader row it came from under `CITES:`; use `CITES: new` only for a
defect the brief does not carry.

**Exactly one `TREATMENT` line, and only when a treatment is warranted.** This
is the rule that keeps a health review from becoming busy work: one neighbourhood
produces at most one repair, so the review contends with the plan it runs beside
for one node, not for an unbounded list. A treatment is warranted only when it
**deletes code, narrows an interface, wires or deletes unreached code, or closes
a finding recurring in two or more reviews** — every one of those shrinks the
codebase. A treatment that would add surface is not warranted; emit
`TREATMENT: none — <reason>` instead, saying why no treatment is owed. The
reason is required: a bare `none` is a decision with nothing under it.

**A treatment carries its write paths and a measurable done-when.** A treatment
naming no write path cannot be dispatched; a done-when that names no negative
control cannot show the repair holds. Emit both, on the one `TREATMENT` line.

Example of a complete emission:

```
RUBRIC known_findings: a finding — `reckon/a.py:41` and `reckon/b.py:88` are one defect class; the brief names both, so this cites rather than re-reports.
RUBRIC duplicate_capability: a finding — two implementations of path normalisation at `reckon/c.py:12` and `reckon/d.py:57`.
RUBRIC unreached: pass — every module the brief lists is reached by an entry point.
RUBRIC interface_growth: pass — no public surface was added in the last 14 days.
RUBRIC width: pass — no module in the neighbourhood is over the worker-window bound.
RUBRIC test_debt: a finding — `assert "elif" in Path(x).read_text()` at `tests/test_c.py:20` asserts source text, not behaviour.
FINDING duplicate_capability reckon/c.py:12 — CLASS: duplicated_capability — `normalise_path` is re-implemented at `reckon/d.py:57`, so a fix to one leaves the other wrong — CITES: new
FINDING test_debt tests/test_c.py:20 — CLASS: source_text_assertion — the test asserts the file's text contains a token rather than that the behaviour holds — CITES: new
TREATMENT — GOAL: delete the second implementation and route callers to the first — WRITE_PATHS: reckon/c.py, reckon/d.py — DONE_WHEN: `reckon/d.py` no longer defines `normalise_path` and the suite passes; negative control: removing `reckon/c.py`'s definition turns `tests/test_c.py::test_normalise` red — EXPECTED_DELTA: -24
```