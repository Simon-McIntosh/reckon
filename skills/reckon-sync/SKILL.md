---
name: reckon-sync
description: >-
  Set up or refresh the reckon plan infrastructure in a repo — ensures reckon
  skills are symlinked into ~/.claude/skills/, creates docs/, copies the 3-layer
  CSS (foundation/dashboard/project) from ~/Code/reckon/docs/_shared/ for GitHub
  Pages compatibility (the live server serves CSS/JSX directly via /_shared/ and
  /_ui/ routes — no per-project JSX copies needed), sets up docs/state/<project>/,
  symlinks ~/docs-server/state/<project> into the repo, and registers the project
  in ~/docs-server/mounts.json. Plans are auto-discovered from HTML <meta name="plan-*">
  tags — no index.json inventory required. Idempotent.
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
2. **Never overwrite `docs/state/<project>/*.json`** — only seed when absent.
3. **Never overwrite per-plan HTML** the user has already authored.
4. **reckon-sync owns `mounts.json` and the state-dir symlink exclusively.**
   `reckon-create` does NOT register mounts or create symlinks.
5. **Never use `/tmp`.** Use `$REPO_ROOT/.reckon-sync-tmp-$(date +%s)` if
   needed and clean it up before exiting.
6. **Never commit automatically.** Print a suggested commit message.
7. **Use `python3` for JSON manipulation** — `jq` may not be available.

## Workflow

### Intent detection

| Condition | Intent |
|---|---|
| `docs/` absent **or** `docs/_shared/` absent | **first-run** — all steps run |
| Both present | **refresh** — Steps 0 and 2 only; steps 3–4 are no-ops |

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

### Step 2 — Copy canonical files

Always run. Overwrites system-owned files only; never touches per-plan HTML
or `state/*.json`.

```bash
mkdir -p "$DOCS/_shared"

# CSS only — the live server serves JSX/JS via /_ui/ directly from the reckon
# install; per-project copies are only needed for GitHub Pages static hosting.
cp "$RECKON/docs/_shared/foundation.css" "$DOCS/_shared/foundation.css"
cp "$RECKON/docs/_shared/dashboard.css"  "$DOCS/_shared/dashboard.css"

# state.js is copied for legacy standalone plan pages that load it directly.
# SPA pages (index.html → shell.jsx) do not need it.
cp "$RECKON/docs/_shared/state.js"       "$DOCS/_shared/state.js"
```

Note: JSX components (`ui.jsx`, `shell.jsx`, `bits.jsx`, etc.) are NOT copied here.
The reckon server serves them from `/_ui/<file>` directly. For full offline/static
deployment, use `reckon build` instead of `reckon sync`.

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

### Step 5 — Seed project.json (sprint/milestone definitions only)

Plans are auto-discovered from HTML `<meta name="plan-*">` tags — no inventory
in JSON required. The only state that lives here is project-level structure
(sprint themes, milestone names) that has no natural home in an individual plan.

```bash
PROJ="$DOCS/state/$PROJECT/project.json"
if [ ! -f "$PROJ" ]; then
  python3 - <<EOF
import json, datetime
seed = {
  "updated": datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S'),
  "project": "$PROJECT", "doc": "project",
  "data": {
    "sprints":    [],
    "milestones": [],
    "blockers":   []
  }
}
json.dump(seed, open('$PROJ', 'w'), indent=2)
print('seeded $PROJ')
EOF
fi
```

Do NOT write `_version` into the seed — the reckon server manages that field.

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

## Plan HTML style guide

### How reckon discovers plan pages

`GET /_discover/<project>` scans every `.html` file in the mounted docs
directory (excluding `_shared/`, `ui/`, `state/`, `assets/` subdirectories
and known infrastructure pages like `index.html`, `sprint.html`, etc.).

**A page is included in the inventory only if its `<head>` contains:**
```html
<meta name="plan-status" content="active">
```
This single tag is the opt-in. Without it the page is ignored by discovery.

### Canonical plan `<head>` meta tags

```html
<!-- Required for discovery -->
<meta name="plan-status"    content="active">          <!-- active|pending|blocked|shipped -->

<!-- Strongly recommended -->
<meta name="plan-slug"      content="my-plan">         <!-- defaults to filename stem -->
<meta name="plan-title"     content="Human title">     <!-- defaults to <title> text -->
<meta name="plan-summary"   content="One-line synopsis">

<!-- Categorisation (used by filters and fleet rollup) -->
<meta name="plan-milestone" content="M1">
<meta name="plan-roi"       content="high">            <!-- high|mid|low -->
<meta name="plan-effort"    content="M">               <!-- S|M|L|XL -->
<meta name="plan-sprint"    content="S1">              <!-- omit if unscheduled -->
```

### Minimal plan page boilerplate

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="docs-project"  content="my-project">
  <meta name="plan-status"   content="active">
  <meta name="plan-slug"     content="my-plan">
  <meta name="plan-title"    content="My Plan">
  <meta name="plan-summary"  content="One sentence.">
  <meta name="plan-milestone" content="M1">
  <meta name="plan-roi"      content="high">
  <meta name="plan-effort"   content="M">
  <title>My Plan · my-project</title>
  <link rel="stylesheet" href="../_shared/foundation.css">
  <link rel="stylesheet" href="../_shared/dashboard.css">
  <script src="https://unpkg.com/react@18.3.1/umd/react.production.min.js" crossorigin></script>
  <script src="https://unpkg.com/react-dom@18.3.1/umd/react-dom.production.min.js" crossorigin></script>
  <script src="https://unpkg.com/@babel/standalone@7.29.0/babel.min.js" crossorigin></script>
</head>
<body>
  <div id="root"></div>
  <script src="../ui/state-loader.js"></script>
  <script type="text/babel" src="../ui/ui.jsx"></script>
  <!-- your plan-specific JSX here -->
</body>
</html>
```

### State JSON vs meta tags

| What | Where |
|---|---|
| Current status, impl fraction, open decisions | `state/<project>/<slug>.json` (written by the SPA) |
| Initial/authored metadata (milestone, roi, effort) | `<meta name="plan-*">` tags |
| Sprint/milestone definitions (theme, dates) | `state/<project>/project.json` |

The SPA writes to the state JSON; the HTML meta tags are the authored defaults
read at discovery time. The state JSON takes precedence for `status` when it
exists — the meta tag value is the fallback.

## CSS layout

Three layers, sourced from `~/Code/reckon/docs/_shared/`:

| File | Destination in project | Role |
|---|---|---|
| `foundation.css` | `docs/_shared/foundation.css` | Design tokens — colours, typography, spacing |
| `dashboard.css` | `docs/_shared/dashboard.css` | Plan widgets — cards, badges, sprint tables |
| `state.js` | `docs/_shared/state.js` | Browser persistence for legacy standalone plan pages |

UI components (`ui.jsx`, `state-loader.js`, `bits.jsx`, `shell.jsx`, etc.) are served
by the reckon server at `/_ui/<file>` directly from `~/Code/reckon/docs/ui/`.
Per-project copies are **not** created by `reckon sync`. Use `reckon build` to produce
a fully self-contained static bundle for CI/GitHub Pages deployment.

## Cross-references

- `~/Code/reckon/skills/reckon-create/SKILL.md` — create the first plan after sync.
- `~/Code/reckon/skills/` — canonical skill source; symlinked to `~/.claude/skills/` by Step 0.
- `~/Code/reckon/reckon/serve.py` — mounts.json path, state root, /_shared/ route, /_discover/ endpoint.
- `~/Code/reckon/docs/_shared/` — canonical CSS and state.js source.
- `~/Code/reckon/docs/ui/` — canonical JSX component source.
