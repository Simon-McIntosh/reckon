# Figure review

Review the rendered images attached to this run. Judge the visual artifact the
reader will see, not only the source file or its diff. If an attachment cannot
be opened, state that and do not invent a visual verdict.

This review is read only. Do not edit, create, move, delete, redraw, relabel or
reformat any file, including a figure or its caption. Report findings in the
review record only.

For every attached figure, report one `RUBRIC <item>: <verdict>` line for each
item below. A verdict names what is visible and either passes or states the
specific defect:

- `legibility`: the figure is readable at a glance.
- `data_ink`: the data carry the visual weight, without gradients or decorative
  fills.
- `decoration`: no gradient, background fill or decorative styling distracts
  from the data.
- `contrast`: text clears the contrast of its background.
- `table_form`: the image does not depict information that is naturally a
  table.
- `label_collision`: labels are not clipped, overlapping each other, or
  overlapping the data.

Treat a caption that only repeats the figure's title as a finding, but do not
change it.

## The shape a finding takes

Record every finding as an object with these four fields:

- `file` — the source figure path the finding concerns.
- `line` — a line in that source, or `null` when the finding concerns only the
  rendered image and no source line is useful.
- `severity` — one of the two declared severities below.
- `summary` — what is wrong and why it matters to a reader.

**Say whether each finding blocks.** `severity` is one of:

- `blocking` — reserved for a defect that stops a reader reading the figure: a
  clipped or overlapping label, unreadable text, or data the figure does not
  let a reader see.
- `follow-on` — anything else worth recording; the defect does not hold the
  node.

Use no severity outside these two. The word stays in your text as prose where
no gate can read it, and a finding carrying no severity is read as holding the
node, so a follow-on left unlabelled refuses a promotion it should not.

## The record to write

Write the review record at one of the declared write paths. It must identify
the project, reviewed run, review run, reviewed base and head, carry
`"rubric": "figure"`, include the rubric verdicts and findings in the shape
above, and use a recognised review status. A figure record carries no `scores`:
omit them rather than writing the six text dimensions as `null`, and do not
claim suite-delta measurements for a figure review.
