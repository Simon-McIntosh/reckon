# Prior-art and depth review of a plan before its first implementation node

You are reviewing a plan's design before its first implementation node is
released. The rubber-duck review reads the plan for internal correctness; this
one reads it against the codebase it is about to extend. Its purpose is to catch
the two failures a plan cannot see in itself: machinery that already exists and
is being rebuilt, and a design that hides its complexity behind a spread of
shallow wrappers instead of behind one deep module.

The review is advisory. The author answers each finding by acting on it or by
declining it with a one-line reason. Your findings are scored on whether they
**would have changed the plan**.

## The rubric

Score the design against each item below and emit one verdict per item. Every
one of the five items is named by the slug in parentheses, because you must emit
a verdict for each:

1. **reuse_search** (`reuse_search`) — the plan searched for existing machinery
   before proposing new machinery. Search the codebases named by the plan for a
   mechanism that already does what the plan is about to add. This is the reuse
   map made unfailable by memory: a reviewer runs it whether or not the plan's
   author did. A plan that adds a subsystem without citing what it searched is a
   finding.
2. **deep_module** (`deep_module`) — a new module hides substantial behaviour
   behind an interface smaller than what it hides. A module that spreads its
   complexity across many small wrappers over the same machinery adds surface
   without adding depth, and is a finding.
3. **thin_wrapper** (`thin_wrapper`) — a plan that adds a thin layer over
   existing machinery must say why that machinery cannot be extended instead.
   A wrapper with no such justification is a finding: the honest change is
   usually an extension of the existing mechanism, not a parallel one.
4. **duplicate_owner** (`duplicate_owner`) — a duplicate of shared
   infrastructure is a finding naming the owner to extend. Placement, path
   resolution, scheduler queries and state readers are the recurring duplicates;
   when the plan re-implements one, emit a finding that names the mechanism and
   the file that owns it, so the author can extend the owner rather than fork it.
5. **interface_budget** (`interface_budget`) — cite the current week's four
   figures handed to you in the brief: `public_definitions`, `cli_options`,
   `mcp_views` and `refusal_families`. Hold every section that adds to any of
   these families to one rule: the section names, in its own text, what it
   retires or merges, or states why nothing can be. A section adding surface
   without either is a finding. Use the supplied counts rather than guessing
   them or substituting a count from another revision.

RUBRIC_ITEMS: reuse_search, deep_module, thin_wrapper, duplicate_owner, interface_budget

## What to read

Read the plan's design sections, then read the code the design would extend or
replace. For each mechanism the plan proposes, name the existing mechanism it
most resembles and say whether the plan extends it, wraps it, or duplicates it.
A claim about prior art is checked by opening the code, not by reading the plan's
account of it.

## Every instance of a type, not the first

A finding of one type is a census, not a sample. When you find one instance of a
defect — an unplanned importer of a module the plan is meant to replace, a
duplicate owner of a shared mechanism, a missed reuse of machinery that already
exists — you search for every instance of that type in the same pass and list
them all under one finding, naming each file and line. A finding that reports
only the first instance hands the author a partial census without saying so, and
the rest are found later, one review at a time. The census is what makes a
finding actionable: an author who sees every site of one defect fixes them in a
single pass, and a defect type reported once is a defect type that recurs.

## What to emit

For each of the five items, emit one `RUBRIC` line. Then emit one `FINDING`
line per defect, and on each finding carry the would-change verdict and its
reason. When a finding is a census of many instances of one type, that one
`FINDING` line carries every instance of that type, each with its own
`<file>:<line>`. Emit the lines exactly in these forms and nothing else with
these prefixes:

```
RUBRIC <item>: <pass, or the finding it produced — one sentence citing the mechanism or file it is about>
FINDING <item> <file>:<line> — <what is wrong and why it matters> — WOULD_CHANGE_THE_PLAN: <yes|no> — REASON: <one line: why this would, or would not, change the plan>
```

**Every item needs a `RUBRIC` line,** for the same reason as the content review:
an omitted item reads like a checked one and is reported absent rather than
silently taken as a finding.

**Every finding needs a `WOULD_CHANGE_THE_PLAN` verdict and a `REASON`.** The
review is scored on whether a finding would change the plan, so a finding
without them cannot be scored.

A duplicate finding must name the owning file. A finding that says "this looks
duplicated" without naming what to extend has not done the work the item exists
for.

Example of a complete emission:

```
RUBRIC reuse_search: pass — the plan searched the two codebases it names and cites the mechanism it extends.
RUBRIC deep_module: pass — one module hides the record shape, the store and the join behind three functions.
RUBRIC thin_wrapper: a finding — the proposed wrapper adds no behaviour the existing reader lacks.
RUBRIC duplicate_owner: a finding — the plan re-implements placement rather than extending its owner.
RUBRIC interface_budget: a finding — public_definitions <supplied count>, cli_options <supplied count>, mcp_views <supplied count>, refusal_families <supplied count>; the section adds a CLI option without naming what it retires or merges, or why nothing can be.
FINDING duplicate_owner reckon/crew/placement.py — the plan adds a second placement path beside the existing one — WOULD_CHANGE_THE_PLAN: yes — REASON: a forked placement path drifts from the owner and the pilot measured three such duplicates already.
FINDING thin_wrapper <plan>#<node> — the wrapper forwards to the existing reader without justifying why it cannot be extended — WOULD_CHANGE_THE_PLAN: yes — REASON: the plan should extend the reader or delete the wrapper.
FINDING interface_budget <plan>#<node> — the section adds a CLI option without naming what it retires or merges, or why nothing can be — WOULD_CHANGE_THE_PLAN: yes — REASON: the section must account for the added interface surface in its own text.
```
