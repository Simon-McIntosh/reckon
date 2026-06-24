---
name: reckon-sync
description: >-
  Set up or refresh the reckon plan infrastructure in a repo — copies shared
  CSS, writes the SPA index.html, registers the project mount + state dir, and
  links the reckon skills into the agent skill dirs. Idempotent. Trigger verbs:
  "init plans / set up reckon / set up plans / refresh styles / sync plan system
  / update CSS from reckon / /reckon-sync".
allowed-tools: Read Write Edit Bash(*) Grep
---

# reckon-sync — set up or refresh plan infrastructure

## Fast path

- First-time setup / refresh CSS → `uv run --project ~/Code/reckon reckon sync <repo>/docs`
- Link the skills → run **Step S** below (the skill owns this; see why it isn't `install-skills`)
- Verify → `uv run --project ~/Code/reckon reckon doctor`

`reckon sync` does the CSS copy, the SPA `index.html`, `.nojekyll`, the
`state/<project>/` dir + config-home symlink, the `project.json`/`index.json`
seed, and the `mounts.json` registration in one idempotent command — it is the
single source of truth for that logic. The step-by-step below is the
**fallback** when the CLI is unavailable; only the skill-linking step is always
the skill's job (`reckon install-skills` copies into `~/.claude/skills/` only).

Replaces `plan-init` and `plan-style`. Idempotent — safe on already-initialised
repos. Canonical sources live in `~/Code/reckon`, not dotfiles.

## When to invoke

- "init plans" / "set up reckon" / "set up plans" / "refresh styles"
- "sync plan system" / "update CSS from reckon" / `/reckon-sync`
- Repo has no `docs/` or no `docs/_shared/`

## Hard rules

1. **Idempotent.** Every step is a no-op if already applied correctly.
2. **`reckon sync` owns the docs scaffold.** CSS, `index.html`, `.nojekyll`, the
   state dir + symlink, `project.json`/`index.json`, and `mounts.json` are all
   created/refreshed by the CLI. Do not hand-write any of them when the CLI is
   available — the fallback below exists only for when it is not.
3. **Never overwrite `docs/state/<project>/index.json`** — the CLI only seeds it
   when absent and otherwise preserves authored sprint/milestone data.
4. **Never overwrite per-plan HTML** the user has already authored.
5. **No per-plan state JSON.** Plan state lives as semantic HTML inside each plan's HTML file. The only JSON in `docs/state/<project>/` is `index.json` (project config) and `project.json` (sprint/milestone definitions).
6. **reckon-sync owns `mounts.json` and the state-dir symlink exclusively.** `reckon-create` does NOT touch these.
7. **Never use `/tmp`.** Use `$REPO_ROOT/.reckon-sync-tmp-$(date +%s)` if needed and clean it up.
8. **Never commit automatically.** Print a suggested commit message.

## What appears in the plan inventory

**Any HTML file under `docs/` is discovered — no content filter is applied.** Sparse plans (bare HTML with no `plan-*` meta) appear as `status=draft` with `<title>` as the title. Existence is sufficient.

**Excluded dirs** (children of the project's docs dir):
`_shared`, `ui`, `state`, `assets`, `images`, `archive`

**Excluded files:**
`index.html`, `sprint.html`, `sprints.html`, `milestones.html`, `decisions.html`, `inventory.html`, `blockers.html`, `implementation.html`, `questions.html`, `home.html`, `project.html`

**Research docs** (`<meta name="reckon-type" content="research">`) appear with a "research" banner in the SPA and `type="research"` in the discovery payload. They have no decision/followup workflow. If you see plans appearing with minimal metadata, they are genuinely sparse — not filtered out.

Per-stage history (`<plan>-shipped.html`, `*-locked.html`, …) lives under `docs/archive/` so it does not clutter the live inventory.

## Workflow

Two steps. **Step D** delegates the docs scaffold to the CLI; **Step S** links
the skills (the one job the CLI does not fully do). Then verify with `doctor`.

### Step D — Sync the docs scaffold (CLI)

```bash
REPO_ROOT="$(git rev-parse --show-toplevel)"
PROJECT="$(basename "$REPO_ROOT")"

uv run --project ~/Code/reckon reckon sync "$REPO_ROOT/docs"
# Pass --project to override the key (defaults to the docs parent dir name):
#   uv run --project ~/Code/reckon reckon sync "$REPO_ROOT/docs" --project my-key
```

`reckon sync` is idempotent and does **all** of:

- copies `_shared/foundation.css` + `_shared/dashboard.css` from the canonical
  `~/Code/reckon/docs/_shared/` (JSX is served live at `/_ui/<file>`; no
  per-project copies — use `reckon build` for offline/static deploys);
- writes `docs/index.html` (the SPA entry point) on first run, and refreshes it
  on later runs **only if** the existing file is already a reckon SPA — a
  hand-authored page is left untouched;
- drops `.nojekyll` for GitHub Pages;
- creates `docs/state/<project>/`, seeds `project.json` and `index.json` (only
  when absent / preserving authored sprint+milestone data), and symlinks
  `<config-home>/state/<project>` → it;
- registers `<project> → docs/` in `<config-home>/mounts.json` (re-read on every
  request — no server restart needed).

The CLI resolves `<config-home>` itself (`RECKON_HOME` env → `~/.config/reckon`
→ legacy `~/docs-server`), so the skill does not compute it. Confirm the
`PROJECT` key with the user on first-run.

### Step S — Link reckon skills into the agent skill dirs

The skill owns this leg. `reckon install-skills` **copies** the skill dirs into
`~/.claude/skills/` *only* — it does not touch `~/.agents/skills/`, does not use
symlinks, and does not clean up legacy whole-dir links. We want symlinks (so a
skill edit in `~/Code/reckon` is live immediately) into **both** runtime dirs,
so we do it here:

- `~/.claude/skills/` — Claude Code
- `~/.agents/skills/` — other agent runtimes

Enumerate skills by the **presence of a `SKILL.md`** in each subdirectory, not
by a name prefix — a future rename that drops the `reckon-` prefix must not
silently skip a skill. The family currently has six skills (`reckon-create`,
`reckon-edit`, `reckon-ship`, `reckon-status`, `reckon-sync`,
`reckon-sprint`), but never hardcode that list; discover it.

```bash
RECKON_SKILLS="$HOME/Code/reckon/skills"

# Skill dirs = subdirs that contain a SKILL.md (prefix-agnostic).
reckon_skill_dirs() {
  for d in "$RECKON_SKILLS"/*/; do
    [ -f "$d/SKILL.md" ] && printf '%s\n' "${d%/}"
  done
}

link_skills() {
  local DEST="$1"

  # Migrate a legacy whole-dir symlink (DEST itself is a symlink) → a real dir
  # of individual per-skill links.
  if [ -L "$DEST" ]; then
    LINK_TARGET="$(readlink "$DEST")"
    rm "$DEST"
    mkdir -p "$DEST"
    for skill_dir in "$LINK_TARGET"/*/; do
      [ -f "$skill_dir/SKILL.md" ] || continue
      skill_name="$(basename "$skill_dir")"
      ln -sfn "${skill_dir%/}" "$DEST/$skill_name"
    done
  fi

  mkdir -p "$DEST"

  # Link each canonical skill dir individually.
  while IFS= read -r skill_dir; do
    skill_name="$(basename "$skill_dir")"
    target="$DEST/$skill_name"
    if [ -L "$target" ] && [ "$(readlink "$target")" = "$skill_dir" ]; then
      echo "  ok   $skill_name"
    else
      ln -sfn "$skill_dir" "$target"
      echo "  link $skill_name"
    fi
  done < <(reckon_skill_dirs)

  # Remove dead links pointing into reckon/skills/ (handles renames/deletes).
  # Glob entries directly (not "*/") so broken symlinks — which no longer
  # resolve to a directory — are still seen.
  for link in "$DEST"/*; do
    if [ -L "$link" ]; then
      target="$(readlink "$link")"
      case "$target" in
        "$RECKON_SKILLS"/*)
          [ -e "$link" ] || { rm "$link"; echo "  removed dead link $(basename "$link")"; }
          ;;
      esac
    fi
  done
}

echo "~/.claude/skills:"; link_skills "$HOME/.claude/skills"
echo "~/.agents/skills:"; link_skills "$HOME/.agents/skills"
```

### Verify

```bash
uv run --project ~/Code/reckon reckon doctor
```

`doctor` checks the skills are present, `mounts.json` is reachable with every
mounted dir existing, and the MCP config registers the `reckon` server.

Confirm to user:

> **reckon-sync complete — docs/ synced via `reckon sync`, skills linked into
> `~/.claude/skills` + `~/.agents/skills`, verified with `reckon doctor`.**
> Run `/reckon-create <slug>` to add a plan.

Suggested commit: `docs(plans): sync plan infrastructure from reckon canonical`

## Fallback — CLI unavailable

Run this **only** when `reckon sync` cannot run (no reckon checkout / broken
install). It reproduces Step D by hand and will drift from the CLI — prefer the
CLI whenever possible. Step S (skill linking) above is unaffected: run it
regardless.

```bash
REPO_ROOT="$(git rev-parse --show-toplevel)"
PROJECT="$(basename "$REPO_ROOT")"
DOCS="$REPO_ROOT/docs"
RECKON="$HOME/Code/reckon"

# Config home: RECKON_HOME env → ~/.config/reckon (preferred) → ~/docs-server
# (legacy fallback). Mirrors reckon._store._config_home so the symlink and
# mounts.json land where the server reads them.
if [ -n "$RECKON_HOME" ]; then
  CONFIG_HOME="$RECKON_HOME"
elif [ -d "$HOME/.config/reckon" ]; then
  CONFIG_HOME="$HOME/.config/reckon"
elif [ -d "$HOME/docs-server" ]; then
  CONFIG_HOME="$HOME/docs-server"      # legacy install — still resolves
else
  CONFIG_HOME="$HOME/.config/reckon"   # fresh install → XDG location
fi
mkdir -p "$CONFIG_HOME/state"

# CSS (overwrites system-owned CSS only; never per-plan HTML)
mkdir -p "$DOCS/_shared"
cp "$RECKON/docs/_shared/foundation.css" "$DOCS/_shared/foundation.css"
cp "$RECKON/docs/_shared/dashboard.css"  "$DOCS/_shared/dashboard.css"

# index.html — create on first run, refresh only if already a reckon SPA.
INDEX="$DOCS/index.html"
if [ ! -f "$INDEX" ] || grep -q '_shared/' "$INDEX" 2>/dev/null; then
  cat > "$INDEX" <<HTMLEOF
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="docs-project" content="${PROJECT}">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>reckon · ${PROJECT}</title>
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
  echo "skipped index.html — not a reckon SPA (manual review needed)"
fi

# .nojekyll
[ -f "$DOCS/.nojekyll" ] || touch "$DOCS/.nojekyll"

# State dir + config-home symlink (migrate a real dir → symlink first)
mkdir -p "$DOCS/state/$PROJECT"
if [ -d "$CONFIG_HOME/state/$PROJECT" ] && [ ! -L "$CONFIG_HOME/state/$PROJECT" ]; then
  mv "$CONFIG_HOME/state/$PROJECT"/*.json "$DOCS/state/$PROJECT/" 2>/dev/null || true
  rmdir "$CONFIG_HOME/state/$PROJECT"
fi
[ -L "$CONFIG_HOME/state/$PROJECT" ] || ln -s "$DOCS/state/$PROJECT" "$CONFIG_HOME/state/$PROJECT"

# Register in mounts.json (python3 — jq may be absent)
MOUNTS="$CONFIG_HOME/mounts.json"
[ -f "$MOUNTS" ] || echo '{}' > "$MOUNTS"
python3 - "$MOUNTS" "$PROJECT" "$DOCS" <<'EOF'
import json, sys
mpath, p, d = sys.argv[1], sys.argv[2], sys.argv[3]
data = json.load(open(mpath))
if p not in data:
    data[p] = d
    json.dump(data, open(mpath, 'w'), indent=2)
    print(f'registered {p} -> {d}')
else:
    print(f'{p} already registered')
EOF

# Seed index.json (project config only) when absent
PROJ="$DOCS/state/$PROJECT/index.json"
[ -f "$PROJ" ] || python3 - "$PROJ" "$PROJECT" <<'EOF'
import json, sys, datetime
path, project = sys.argv[1], sys.argv[2]
seed = {
  "updated": datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S'),
  "project": project,
  "doc": "index",
  "data": {"active_sprint_id": None, "sprints": [], "milestones": []},
}
json.dump(seed, open(path, 'w'), indent=2)
print(f'seeded {path}')
EOF
```

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

`reckon sync` copies two layers from `~/Code/reckon/docs/_shared/`:

| File | Role |
|---|---|
| `foundation.css` | Design tokens — colours, typography, spacing |
| `dashboard.css` | Plan widgets — cards, badges, sprint tables |

JSX UI components are served by the reckon server at `/_ui/<file>` directly from `~/Code/reckon/docs/ui/`. No per-project copies.

## Cross-references

- `~/Code/reckon/reckon/cli.py` — **canonical source** for the docs scaffold
  (`sync`), the SPA `index.html` template, `install-skills`, and `doctor`. The
  Fallback section here mirrors `sync`; if they diverge, the CLI wins.
- `reckon-create/SKILL.md` — create the first plan after sync.
- `~/Code/reckon/PLAN-FORMAT.md` — canonical format (semantic HTML, endpoints, exclusion lists).
- `~/Code/reckon/reckon/serve.py` — mounts.json path, `_NON_PLAN_FILES`, `_NON_PLAN_DIRS`.
