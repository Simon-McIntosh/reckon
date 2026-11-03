# Triage the ready-idle reckon sections that carry no dispatchable node contract

Input: the file `docs/state/reckon/triage/ready-idle-input.txt` lists, one per line, `<plan-slug> <section-id>` for every reckon plan section declared `implementable` in `plan-section-declarations`, with no live crew run and no unlanded `plan-section-depends-on` wait, whose prose has no parseable "Done when … Write paths: …" contract. There are about 91.

For each line, read that section in `docs/plans/<slug>.html` and the code it names, then classify it as exactly one of:

- `done`: the code at HEAD already meets the section's done-when. Cite the file and line, or the commit (`git log -S`), that meets it.
- `stale`: the section describes a mechanism the codebase has since replaced or removed, so building it now would be wrong. Cite what replaced it.
- `dispatchable`: still to do. Give a node contract: `goal` (one deliverable, no semicolon), `done_when` (a measurable test or measurement, naming a new test file under tests/ where the work changes code), `write_paths` (repo-relative files the node writes, test file included), `negative_control` (one sentence: what makes the new test fail), and `reasoning` (`standard` or `deep`) and `estimated_hours`.
- `unclear`: you cannot decide without the plan owner. One sentence on why.

Do not edit any plan, and do not write product code. Write one JSON file, `docs/state/reckon/triage/ready-idle-sections.json`: a list of objects with `plan`, `section`, `class`, `evidence` (one or two sentences with file:line or sha), and for `dispatchable` the contract fields above. Validate it with `python -c "import json;json.load(open(...))"` and record the counts per class in the manifest.
