---
name: reckon-plot
description: >-
  Use when the lead points at a figure the reckon SPA serves (a
  /<project>/figures/... or #figure/ link, a pasted screenshot, a docs/figures
  path) and asks for it to be fixed, redrawn, relabelled, restyled or iterated
  on, or when a review returns defects in rendered figures. Figure work runs in
  the current session, not through reckon-build or crew dispatch. Trigger verbs:
  "fix this figure / redraw / replot / relabel / the labels overlap / the lines
  overlap / this figure does not make sense / iterate on the figures /
  /reckon-plot". New analysis or numbers behind a figure are reckon-build work;
  plan text is reckon-edit work.
allowed-tools: Read Write Edit Bash(*) Grep mcp__reckon__read_plan mcp__reckon__edit_plan mcp__reckon__crew
---

# reckon-plot — figures, edited in the session

The figure is the feedback unit: the lead reviews the rendered file, so the loop
that matters is edit, render, look. In-session that loop takes seconds. Through
a worker it takes a dispatch, a review and an integration per round, and flagged
figures have needed several rounds. So this skill is the exception to
reckon-build's coordinator-only rule: the session edits the drawing code,
renders, looks and commits, itself.

The plot-style rules in `~/.agents/AGENTS.md` (the sections "Plot style" and
"Every figure is checked and reviewed as an artifact") and the figure section of
the repository's own `AGENTS.md` apply in full. This skill does not restate them.

## Fan-out boundary

In a crew-managed repository, investigation and review fan-out is `reckon crew dispatch` work under the investigate and review roles; harness-native background agents bypass the run ledger, manifests and calibration and are refused by the guard. A figure loop needs neither: do it yourself.

## Scope

| In the session | A reckon-build node |
|---|---|
| A figure under `docs/figures/<topic>/`, the function that draws it, the style and symbol helpers it draws through, its captions | New data, a new analysis, or a changed model or number the figure reports |
| What is drawn and how: topology, labels, spacing, colour roles, annotations, captions | A restyle sweep across many figures: brief it precisely and tell the lead which went out |
| A `--only` flag or a layout check the figure needs | |

## Canvas

Draw on the slide canvas unless the figure needs more height: 14 × 7.2 in, an
aspect of 1.94:1, with the file declaring a 16 cm height (31.1 × 16 cm) so it
inserts on a slide at that size. An SVG declares it through its `width` and
`height`, and a PNG drawn at 200 dpi through its resolution. Every image of one
build sequence shares the canvas. A figure with many stacked panels may take
more height, keeping the 14 in width. The repository's save function holds the
canvas and the declaration (`~/.agents/AGENTS.md`, "Plot style").

## The loop

1. **Resolve the figure.** A `/<project>/figures/<slug>` URL or a
   `#figure/<slug>` link names `docs/figures/<slug>`. Find the writer with
   `grep -rln "<file>.svg" scripts/ src/`, then the function inside it. Find
   every caption site with `grep -rln "<topic>/<file>" docs/ scripts/`.
2. **Check the claim.** Run `git status --short -- <paths>` and
   `git log --since="2 hours ago" -- <paths>`, and read
   `crew(project, view="scopes")`. A topic directory is the allocation unit
   (reckon-create hard rule 8). If a live run's write path covers it, stop and
   report; never edit under a running worker.
3. **Read what the figure asserts.** Before moving a mark, read the source of
   truth the figure draws: the register, netlist, evidence row or data owner.
   Correct the content first, because a tidy wrong drawing is worse than an
   untidy right one. Take values from the owning module, not from literals.
4. **Make it render alone.** If the script draws several figures, or needs data
   this figure does not, add `--only <name>` (reuse the repository's flag if it
   has one). One figure should render in seconds. Never rerun a whole pipeline
   to see one figure.
5. **Render into the main checkout.** Write to the main checkout's
   `docs/figures/<topic>/`. The SPA serves the working tree of the mounted
   `docs/` (`~/.config/reckon/mounts.json`) and revalidates on every load, so a
   refresh shows the new file. A worktree or scratch copy is invisible to it.
   Cap the threads (`OMP_NUM_THREADS=2`, `nice -n 10`). Save through the
   repository's checked save function. A refusal is the loop working: move the
   label, never suppress the check.
6. **Look.** Run `bash <this skill>/scripts/look.sh <svg> <scratch-dir> [WxH+X+Y ...]`,
   then Read the PNG and every crop. A passing save check is not a look. Check
   what the text check cannot see: lines drawn on top of each other, a wire
   through a symbol, crowded parts, a label naming the wrong part, a switched
   trace drawn as a solid block, a colour that carries no meaning. Crop a zoom
   wherever marks are dense.
7. **Repeat 4 to 6** until the PNG is right. Then show the lead the PNG (Read
   puts it in the session; use `SendUserFile` when they follow remotely) and
   the served file, `http://localhost:8765/<project>/figures/<slug>`, which the
   SPA's Figures tab also lists.

## Finish

- **Captions.** Change the caption where it is written, which is usually the
  script that writes a section partial, and in that partial. Then rebuild any
  assembled document with its own assembly command. The assembled diff must
  touch only the caption lines. Never hand-edit an assembled document.
- **Guards.** A check you add for a defect the save check missed (for example
  overlapping wires in a schematic) lands with a test that has a negative
  control.
- **Gate.** Run only the tests that reach the touched files
  (`grep -rln "<module>" tests/`), and lint by delta against `HEAD`.
- **Commit** each accepted round with explicit paths and a body that says what
  the figure now shows and how it was checked. Push where the repository has a
  remote. The SPA showing the working tree is not a reason to leave it
  uncommitted.
- **Record.** If a plan or evidence claim rested on the old drawing, such as a
  topology or a value, record the correction through reckon-edit. In-session
  work writes no run, ledger row or manifest.

## Common mistakes

| Mistake | Instead |
|---|---|
| Dispatching a worker for a flagged figure | Edit it in the session |
| Rerunning the full script with its data to see one figure | `--only <name>` |
| Trusting the save check or the SVG source | Rasterise and Read the PNG |
| Respacing a circuit whose topology is wrong | Read the register first, then lay it out |
| Rendering in a worktree the SPA does not serve | Render into the main checkout |
| Hand-editing an assembled document | Its assembly command |
| Reporting a fix the lead cannot see | Give the PNG and the served figure URL |

## Report

State what the figure showed before and what it shows now, in plain words; the
paths changed; the checks run, with their result; the commit sha as the command
printed it; and what remains, with the reason.
