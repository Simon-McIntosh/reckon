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
change it. Anchor each finding to the source figure path and line when the
source has a useful line; otherwise use the figure path and state that the
finding concerns the rendered image.

Write the review record at one of the declared write paths. It must identify
the project, reviewed run, review run, reviewed base and head, carry
`"rubric": "figure"`, include the rubric verdicts and findings, and use a
recognised review status. The text review's scores do not measure this rubric;
do not add them or claim suite-delta measurements for a figure review.
