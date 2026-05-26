---
name: reckon-sync
description: >-
  Set up or refresh the reckon plan infrastructure in a repo — ensures reckon
  skills are symlinked into ~/.claude/skills/, creates docs/, copies the 3-layer
  CSS (foundation/dashboard/project) from ~/Code/reckon/docs/_shared/ for GitHub
  Pages compatibility (the live server serves CSS/JSX directly via /_shared/ and
  /_ui/ routes — no per-project JSX copies needed), sets up docs/state/<project>/
  with index.json for project config only (no per-plan state JSON), symlinks
  ~/docs-server/state/<project> into the repo, and registers the project in
  ~/docs-server/mounts.json. Plans are auto-discovered from any HTML file in
  docs/ — no opt-in meta tag required. Idempotent.
  Trigger verbs: "init plans / set up reckon / set up plans / refresh styles /
  sync plan system / update CSS from reckon / /reckon-sync".
allowed-tools: Read Write Edit Bash(*) Grep
---

# reckon-sync — set up or refresh plan infrastructure

Replaces `plan-init` and `plan-style`. Idempotent — safe on already-initialised
repos. Canonical sources live in `~/Code/reckon`, not dotfiles. The reckon
server serves `/_shared/` from there directly; per-project copies ensure
GitHub Pages compatibility.

## When to invoke

- "init plans" / "set up reckon" / "set up plans" / "refresh styles"
- "sync plan system" / "update CSS from reckon" / `/reckon-sync`
- Repo has no `docs/` or no `docs/_shared/`

**When NOT to invoke:** if `docs/` is absent but the project should already
have plans, check `reckon-create` first — `reckon-sync` creates infrastructure;
`reckon-create` creates plan pages.

## Hard rules

1. **Idempotent.** Every step is a no-op if already applied correctly.
2. **Never overwrite `docs/state/<project>/index.json`** — only seed when absent.
3. **Never overwrite per-plan HTML** the user has already authored.
4. **Never create per-plan state JSON sidecars.** Plan state lives in each plan's
   HTML island (`<script type="application/json" id="reckon-state">`).
   The only JSON in `docs/state/<project>/` is `index.json` (project config).
5. **reckon-sync owns `mounts.json` and the state-dir symlink exclusively.**
   `reckon-create` does NOT register mounts or create symlinks.
6. **Never use `/tmp`.** Use `$REPO_ROOT/.reckon-sync-tmp-$(date +%s)` if
   needed and clean it up before exiting.
7. **Never commit automatically.** Print a suggested commit message.
8. **Use `python3` for JSON manipulation** — `jq` may not be available.

## Where state lives

**The plan HTML is the sole store.** All plan data (status, impl, decisions,
followups, comments, questions, research, notes) lives in the HTML file's
`<script type="application/json" id="reckon-state">` island. Live edits
(browser clicks, MCP tools) rewrite the HTML in place.

`docs/state/<project>/index.json` holds **project-level config only**:
sprints, milestones, `active_sprint_id`, timeline. It is not per-plan state.

The reckon server reads plan state by parsing each HTML file's island.
`reckon-sync` creates and symlinks the state directory so the server can
write project config (index.json) back to the repo.

## Workflow

### Intent detection

| Condition | Intent |
|---|---|
| `docs/` absent **or** `docs/_shared/` absent | **first-run** — all steps run |
| Both present | **refresh** — Steps 0 and 2 only; steps 3–5 are no-ops |

### Step 0 — Link reckon skills and clean up dead links

Always run. Each reckon skill is linked individually into both skill
directories so they are independently correct:
- **`~/.claude/skills/`** — Claude Code
- **`~/.agents/skills/`** — other agent runtimes (Cursor, Aider, Continue, etc.)

After linking, dead symlinks pointing into `~/Code/reckon/skills/` are removed
from both dirs so renamed or deleted skills don't leave stale entries.

```bash
RECKON_SKILLS="$HOME/Code/reckon/skills"

link_skills() {
  local DEST="$1"

  # Migrate legacy whole-dir symlink to dotfiles → real directory
  if [ -L "$DEST" ]; then
    LINK_TARGET="$(readlink "$DEST")"
    rm "$DEST"
    mkdir -p "$DEST"
    for skill_dir in "$LINK_TARGET"/*/; do
      skill_name="$(basename "$skill_dir")"
      case "$skill_name" in reckon-*) continue;; esac
      ln -sfn "$skill_dir" "$DEST/$skill_name"
      echo "  linked (migrated) $skill_name"
    done
    echo "  migrated $LINK_TARGET → individual symlinks in $DEST"
  fi

  mkdir -p "$DEST"

  # Link each reckon skill
  for skill_dir in "$RECKON_SKILLS"/*/; do
    skill_name="$(basename "$skill_dir")"
    target="$DEST/$skill_name"
    if [ -L "$target" ] && [ "$(readlink "$target")" = "$skill_dir" ]; then
      echo "  ok   $skill_name"
    else
      ln -sfn "$skill_dir" "$target"
      echo "  link $skill_name"
    fi
  done

  # Remove dead links that point into the reckon skills dir (handles renames)
  for link in "$DEST"/*/; do
    link="${link%/}"
    if [ -L "$link" ]; then
      target="$(readlink "$link")"
      # Only clean up links that were ours (point into reckon/skills/)
      case "$target" in
        "$RECKON_SKILLS"/*)
          if [ ! -e "$link" ]; then
            rm "$link"
            echo "  removed dead link $(basename "$link")"
          fi
          ;;
      esac
    fi
  done
}

echo "~/.claude/skills:"
link_skills "$HOME/.claude/skills"

echo "~/.agents/skills:"
link_skills "$HOME/.agents/skills"
```

### Step 1 — Detect intent

```bash
REPO_ROOT="$(git rev-parse --show-toplevel)"
PROJECT="$(basename "$REPO_ROOT")"
DOCS="$REPO_ROOT/docs"
RECKON="$HOME/Code/reckon"

[ ! -d "$DOCS" ] || [ ! -d "$DOCS/_shared" ] && INTENT=first-run || INTENT=refresh
echo "intent=$INTENT  project=$PROJECT"
```

Confirm the `PROJECT` key with the user on first-run (sets URL prefix and
`state/` path).

### Step 2 — Copy canonical CSS files

Always run. Overwrites system-owned CSS only; never touches per-plan HTML
or `state/index.json`.

```bash
mkdir -p "$DOCS/_shared"

# CSS — copied for GitHub Pages static hosting.
# The live server serves these directly via /_shared/ from the reckon install.
cp "$RECKON/docs/_shared/foundation.css" "$DOCS/_shared/foundation.css"
cp "$RECKON/docs/_shared/dashboard.css"  "$DOCS/_shared/dashboard.css"
```

Note: `state.js` is NOT copied — it was a legacy standalone-interactivity
helper that is no longer used. JSX components are served by the reckon server
at `/_ui/<file>`. For full offline/static deployment, use `reckon build`.

### Step 2b — Create or update docs/index.html (SPA entry point)

On first-run, create `docs/index.html` as the v7 SPA entry point. On refresh,
update only if the file already uses the v7 format (loads from `_shared/` and `ui/`).
**Never overwrite** an `index.html` that is a hand-authored plan page.

```bash
INDEX="$DOCS/index.html"

# Detect v7 SPA: loads from _shared/ or has docs-project meta tag
IS_V7_ALREADY=false
if [ -f "$INDEX" ] && grep -q '_shared/' "$INDEX" 2>/dev/null; then
  IS_V7_ALREADY=true
fi

if [ "$INTENT" = "first-run" ] || [ "$IS_V7_ALREADY" = "true" ]; then
  cat > "$INDEX" <<HTMLEOF
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="docs-project" content="${PROJECT}">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>reckon · plan system</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700&family=Geist+Mono:wght@400;500&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="/_shared/foundation.css">
  <link rel="stylesheet" href="/_shared/dashboard.css">
  <link rel="stylesheet" href="/_ui/project.css">
  <link rel="stylesheet" href="/_ui/styles-base.css">
  <link rel="stylesheet" href="/_ui/styles.css">
  <script src="https://unpkg.com/react@18.3.1/umd/react.development.js" integrity="sha384-hD6/rw4ppMLGNu3tX5cjIb+uRZ7UkRJ6BPkLpg4hAu/6onKUg4lLsHAs9EBPT82L" crossorigin="anonymous"></script>
  <script src="https://unpkg.com/react-dom@18.3.1/umd/react-dom.development.js" integrity="sha384-u6aeetuaXnQ38mYT8rp6sbXaQe3NL9t+IBXmnYxwkUI2Hw4bsp2Wvmx4yRQF1uAm" crossorigin="anonymous"></script>
  <script src="https://unpkg.com/@babel/standalone@7.29.0/babel.min.js" integrity="sha384-m08KidiNqLdpJqLq95G/LEi8Qvjl/xUYll3QILypMoQ65QorJ9Lvtp2RXYGBFj1y" crossorigin="anonymous"></script>
</head>
<body>
  <div id="root"></div>
  <script src="/_ui/state-loader.js"></script>
  <script type="text/babel" src="/_ui/ui.jsx"></script>
  <script type="text/babel" src="/_ui/bits.jsx"></script>
  <script type="text/babel" src="/_ui/decision.jsx"></script>
  <script type="text/babel" src="/_ui/cockpit.jsx"></script>
  <script type="text/babel" src="/_ui/plan.jsx"></script>
  <script type="text/babel" src="/_ui/sprint.jsx"></script>
  <script type="text/babel" src="/_ui/graph.jsx"></script>
  <script type="text/babel" src="/_ui/shell.jsx"></script>
</body>
</html>
HTMLEOF
  echo "wrote $INDEX (project=$PROJECT)"
else
  echo "skipped index.html — file exists and is not a v7 SPA (manual review needed)"
fi
```

### Step 3 — State directory setup

```bash
mkdir -p "$DOCS/state/$PROJECT" ~/docs-server/state

# Migrate real directory → repo path, then replace with symlink
if [ -d ~/docs-server/state/"$PROJECT" ] && [ ! -L ~/docs-server/state/"$PROJECT" ]; then
  mv ~/docs-server/state/"$PROJECT"/*.json "$DOCS/state/$PROJECT/" 2>/dev/null || true
  rmdir ~/docs-server/state/"$PROJECT"
fi

# Symlink: ~/docs-server/state/<project> → <repo>/docs/state/<project>/
[ -L ~/docs-server/state/"$PROJECT" ] || \
  ln -s "$DOCS/state/$PROJECT" ~/docs-server/state/"$PROJECT"
```

State files live in the repo (`docs/state/<project>/`) and are git-tracked.
The reckon server writes through the symlink at `~/docs-server/state/<project>`.
Only `index.json` (project config) lives here — not per-plan state JSON.

### Step 4 — Register in mounts.json

```bash
MOUNTS=~/docs-server/mounts.json
[ -f "$MOUNTS" ] || echo '{}' > "$MOUNTS"

python3 - <<EOF
import json
p, d = '$PROJECT', '$DOCS'
data = json.load(open('$MOUNTS'))
if p not in data:
    data[p] = d
    json.dump(data, open('$MOUNTS', 'w'), indent=2)
    print(f'registered {p} → {d}')
else:
    print(f'{p} already registered')
EOF
```

`mounts.json` is re-read on every request — no server restart needed.
Start the server if not running: `uv run --project ~/Code/reckon reckon serve`

### Step 5 — Seed index.json (project config only)

`index.json` holds sprint/milestone definitions and project-level config. Plans
are auto-discovered by the server parsing each HTML file's island — no plan
inventory in `index.json` is required.

```bash
PROJ="$DOCS/state/$PROJECT/index.json"
if [ ! -f "$PROJ" ]; then
  python3 - <<EOF
import json, datetime
seed = {
  "updated": datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S'),
  "project": "$PROJECT",
  "data": {
    "active_sprint_id": null,
    "sprints":    [],
    "milestones": [],
    "timeline":   []
  }
}
json.dump(seed, open('$PROJ', 'w'), indent=2)
print('seeded $PROJ')
EOF
fi
```

Do NOT write `version` into the seed — the reckon server manages that field.

### Step 6 — Drop .nojekyll

```bash
[ -f "$DOCS/.nojekyll" ] || touch "$DOCS/.nojekyll"
```

Confirm to the user when done:

> **reckon-sync complete — docs/ ready, docs-server registered.**
> Run `/reckon-create <slug>` to add a plan.

Suggested commit:
```
docs(plans): sync plan infrastructure from reckon canonical
```

## How reckon discovers plans

`GET /_discover/<project>` scans every `.html` file in the mounted docs
directory and surfaces each one as a plan. **Any HTML file is a plan —
existence is sufficient.** No `plan-status` meta opt-in is required.

**Excluded dirs:** `_shared`, `ui`, `state`, `assets`, `images`, `archive`.
**Excluded files:** `index.html`, `sprints.html`, `milestones.html`,
`decisions.html`, `inventory.html`, `blockers.html`, `questions.html`,
`home.html`, `project.html`.

Per-stage history (`<plan>-shipped.html`, `*-locked.html`, …) lives under
`docs/archive/` so it does not appear in the live inventory.

The server parses each plan's `<script type="application/json" id="reckon-state">`
island for structured state. A bare page with no island surfaces with
`status=draft` and its `<title>` as the title.

## Canonical plan `<head>` meta tags (optional enrichment)

`<meta>` tags provide fallback values when the island omits a field.
The island always wins when present. These tags are **not required** for discovery.

```html
<!-- Optional enrichment — island fields take precedence -->
<meta name="docs-project" content="my-project">  <!-- required for correct routing -->
<meta name="plan-slug"    content="my-plan">      <!-- defaults to filename stem -->
<meta name="plan-title"   content="Human title">  <!-- defaults to <title> text -->
<meta name="plan-summary" content="One-line synopsis">
<meta name="plan-milestone" content="M1">
<meta name="plan-roi"     content="high">          <!-- high|mid|low -->
<meta name="plan-effort"  content="M">             <!-- S|M|L|XL -->
<meta name="plan-sprint"  content="S1">
```

## Minimal plan page anatomy

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="docs-project" content="<project>">
  <meta name="plan-slug"    content="<slug>">
  <title>My Plan · <project></title>
  <link rel="stylesheet" href="/_shared/foundation.css">
  <link rel="stylesheet" href="/_shared/dashboard.css">
</head>
<body>
  <main class="plan-doc">
    <!-- authored prose -->
  </main>

  <script type="application/json" id="reckon-state">
  {
    "slug": "<slug>",
    "title": "My Plan",
    "status": "draft",
    "decisions": {},
    "followups": []
  }
  </script>
</body>
</html>
```

## CSS layout

Two layers, sourced from `~/Code/reckon/docs/_shared/`:

| File | Destination in project | Role |
|---|---|---|
| `foundation.css` | `docs/_shared/foundation.css` | Design tokens — colours, typography, spacing |
| `dashboard.css` | `docs/_shared/dashboard.css` | Plan widgets — cards, badges, sprint tables |

UI components (`ui.jsx`, `shell.jsx`, `bits.jsx`, etc.) are served
by the reckon server at `/_ui/<file>` directly from `~/Code/reckon/docs/ui/`.
Per-project copies are **not** created by `reckon sync`. Use `reckon build` to produce
a fully self-contained static bundle for CI/GitHub Pages deployment.

## Cross-references

- `~/Code/reckon/skills/reckon-create/SKILL.md` — create the first plan after sync.
- `~/Code/reckon/skills/` — canonical skill source; symlinked to `~/.claude/skills/` by Step 0.
- `~/Code/reckon/PLAN-FORMAT.md` — canonical format (island schema, endpoints, what is gone).
- `~/Code/reckon/reckon/serve.py` — mounts.json path, state root, /_shared/ route, /_discover/ endpoint.
- `~/Code/reckon/docs/_shared/` — canonical CSS source.
- `~/Code/reckon/docs/ui/` — canonical JSX component source.
