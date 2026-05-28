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
repos. Canonical sources live in `~/Code/reckon`, not dotfiles.

## When to invoke

- "init plans" / "set up reckon" / "set up plans" / "refresh styles"
- "sync plan system" / "update CSS from reckon" / `/reckon-sync`
- Repo has no `docs/` or no `docs/_shared/`

## Hard rules

1. **Idempotent.** Every step is a no-op if already applied correctly.
2. **Never overwrite `docs/state/<project>/index.json`** — only seed when absent.
3. **Never overwrite per-plan HTML** the user has already authored.
4. **No per-plan state JSON.** Plan state lives as semantic HTML inside each plan's HTML file. The only JSON in `docs/state/<project>/` is `index.json` (project config only).
5. **reckon-sync owns `mounts.json` and the state-dir symlink exclusively.** `reckon-create` does NOT touch these.
6. **Never use `/tmp`.** Use `$REPO_ROOT/.reckon-sync-tmp-$(date +%s)` if needed and clean it up.
7. **Never commit automatically.** Print a suggested commit message.
8. **Use `python3` for JSON manipulation** — `jq` may not be available.

## What appears in the plan inventory

**Any HTML file under `docs/` is discovered — no content filter is applied.** Sparse plans (bare HTML with no `plan-*` meta) appear as `status=draft` with `<title>` as the title. Existence is sufficient.

**Excluded dirs** (children of the project's docs dir):
`_shared`, `ui`, `state`, `assets`, `images`, `archive`

**Excluded files:**
`index.html`, `sprint.html`, `sprints.html`, `milestones.html`, `decisions.html`, `inventory.html`, `blockers.html`, `implementation.html`, `questions.html`, `home.html`, `project.html`

**Research docs** (`<meta name="reckon-type" content="research">`) appear with a "research" banner in the SPA and `type="research"` in the discovery payload. They have no decision/followup workflow. If you see plans appearing with minimal metadata, they are genuinely sparse — not filtered out.

Per-stage history (`<plan>-shipped.html`, `*-locked.html`, …) lives under `docs/archive/` so it does not clutter the live inventory.

## Workflow

### Intent detection

| Condition | Intent |
|---|---|
| `docs/` absent **or** `docs/_shared/` absent | **first-run** — all steps |
| Both present | **refresh** — Steps 0 and 2 only; steps 3–5 are no-ops |

### Step 0 — Link reckon skills and clean up dead links

Always run. Link each reckon skill individually into both skill dirs:
- `~/.claude/skills/` — Claude Code
- `~/.agents/skills/` — other agent runtimes

```bash
RECKON_SKILLS="$HOME/Code/reckon/skills"

link_skills() {
  local DEST="$1"

  # Migrate legacy whole-dir symlink → individual links
  if [ -L "$DEST" ]; then
    LINK_TARGET="$(readlink "$DEST")"
    rm "$DEST"
    mkdir -p "$DEST"
    for skill_dir in "$LINK_TARGET"/*/; do
      skill_name="$(basename "$skill_dir")"
      case "$skill_name" in reckon-*) continue;; esac
      ln -sfn "$skill_dir" "$DEST/$skill_name"
    done
  fi

  mkdir -p "$DEST"

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

  # Remove dead links pointing into reckon/skills/ (handles renames/deletes)
  for link in "$DEST"/*/; do
    link="${link%/}"
    if [ -L "$link" ]; then
      target="$(readlink "$link")"
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

echo "~/.claude/skills:"; link_skills "$HOME/.claude/skills"
echo "~/.agents/skills:"; link_skills "$HOME/.agents/skills"
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

Confirm the `PROJECT` key with the user on first-run.

### Step 2 — Copy canonical CSS files

Always run. Overwrites system-owned CSS only; never touches per-plan HTML or `index.json`.

```bash
mkdir -p "$DOCS/_shared"
cp "$RECKON/docs/_shared/foundation.css" "$DOCS/_shared/foundation.css"
cp "$RECKON/docs/_shared/dashboard.css"  "$DOCS/_shared/dashboard.css"
```

JSX components are served by the reckon server at `/_ui/<file>` from `~/Code/reckon/docs/ui/`. Per-project JSX copies are NOT created. Use `reckon build` for offline/static deployment.

### Step 2b — Create or update docs/index.html (SPA entry point)

On first-run, create `docs/index.html`. On refresh, update only if the file already uses v7 format. Never overwrite a hand-authored plan page.

```bash
INDEX="$DOCS/index.html"
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
  echo "skipped index.html — not v7 SPA (manual review needed)"
fi
```

### Step 3 — State directory setup

```bash
mkdir -p "$DOCS/state/$PROJECT" ~/docs-server/state

# Migrate real directory → symlink
if [ -d ~/docs-server/state/"$PROJECT" ] && [ ! -L ~/docs-server/state/"$PROJECT" ]; then
  mv ~/docs-server/state/"$PROJECT"/*.json "$DOCS/state/$PROJECT/" 2>/dev/null || true
  rmdir ~/docs-server/state/"$PROJECT"
fi

[ -L ~/docs-server/state/"$PROJECT" ] || \
  ln -s "$DOCS/state/$PROJECT" ~/docs-server/state/"$PROJECT"
```

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

### Step 5 — Seed index.json (project config only)

```bash
PROJ="$DOCS/state/$PROJECT/index.json"
if [ ! -f "$PROJ" ]; then
  python3 - <<EOF
import json, datetime
seed = {
  "updated": datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S'),
  "project": "$PROJECT",
  "data": {
    "active_sprint_id": None,
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

### Step 6 — Drop .nojekyll

```bash
[ -f "$DOCS/.nojekyll" ] || touch "$DOCS/.nojekyll"
```

Confirm to user:

> **reckon-sync complete — docs/ ready, docs-server registered.**
> Run `/reckon-create <slug>` to add a plan.

Suggested commit: `docs(plans): sync plan infrastructure from reckon canonical`

## Canonical plan meta tags

```html
<!-- required -->
<meta name="docs-project"  content="my-project">

<!-- authored scalars — fill in when creating a plan -->
<meta name="reckon-type"   content="plan">         <!-- plan | research -->
<meta name="plan-slug"     content="my-plan">      <!-- default: filename stem -->
<meta name="plan-summary"  content="One-line synopsis">
<meta name="plan-roi"      content="high">         <!-- high|mid|low -->
<meta name="plan-effort"   content="M">            <!-- S|M|L|XL -->
<meta name="plan-tier"     content="sonnet">       <!-- haiku|sonnet|opus -->
<meta name="plan-milestone" content="M1">
<meta name="plan-sprint"   content="S1">
<meta name="plan-depends-on" content="slug-a,research-x">

<!-- server-written — do NOT author these -->
<meta name="plan-status"   content="draft">
<meta name="plan-impl"     content="0.0">
<meta name="plan-version"  content="1">
<meta name="plan-modified" content="2026-05-28">
```

## CSS layout

Two layers copied from `~/Code/reckon/docs/_shared/`:

| File | Role |
|---|---|
| `foundation.css` | Design tokens — colours, typography, spacing |
| `dashboard.css` | Plan widgets — cards, badges, sprint tables |

JSX UI components are served by the reckon server at `/_ui/<file>` directly from `~/Code/reckon/docs/ui/`. No per-project copies.

## Cross-references

- `reckon-create/SKILL.md` — create the first plan after sync.
- `~/Code/reckon/PLAN-FORMAT.md` — canonical format (semantic HTML, endpoints, exclusion lists).
- `~/Code/reckon/reckon/serve.py` — mounts.json path, `_NON_PLAN_FILES`, `_NON_PLAN_DIRS`.
