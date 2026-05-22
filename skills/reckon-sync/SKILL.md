---
name: reckon-sync
description: >-
  Set up or refresh the reckon plan infrastructure in a repo — ensures reckon
  skills are symlinked into ~/.claude/skills/, creates docs/, copies the 3-layer
  CSS (foundation/dashboard/project) and state.js from ~/Code/reckon/docs/_shared/,
  copies ui.jsx and state-loader.js from ~/Code/reckon/docs/ui/, sets up
  docs/state/<project>/, symlinks ~/docs-server/state/<project> into the repo,
  registers the project in ~/docs-server/mounts.json, drops .nojekyll, and seeds
  index.json. Idempotent — safe to re-run as a refresh after reckon updates.
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

### Step 0 — Link reckon skills into ~/.claude/skills/ and ~/.agents/skills/

Always run. Different agent runtimes look in different places:
- **Claude Code** resolves skills from `~/.claude/skills/`
- **Other agent systems** (Cursor, Aider, Continue, etc.) resolve from `~/.agents/skills/`

`~/.agents/skills` is kept as a symlink to `~/.claude/skills` so both paths see
the same set of skills without duplication. Only `~/.claude/skills` needs
individual skill symlinks maintained.

```bash
RECKON_SKILLS="$HOME/Code/reckon/skills"
CLAUDE_SKILLS="$HOME/.claude/skills"
AGENTS_SKILLS="$HOME/.agents/skills"

# Migrate legacy whole-dir symlink to dotfiles → real directory with individual links
if [ -L "$CLAUDE_SKILLS" ]; then
  LINK_TARGET="$(readlink "$CLAUDE_SKILLS")"
  rm "$CLAUDE_SKILLS"
  mkdir -p "$CLAUDE_SKILLS"
  for skill_dir in "$LINK_TARGET"/*/; do
    skill_name="$(basename "$skill_dir")"
    case "$skill_name" in reckon-*) continue;; esac
    ln -sfn "$skill_dir" "$CLAUDE_SKILLS/$skill_name"
    echo "linked (dotfiles) $skill_name"
  done
  echo "migrated $LINK_TARGET → individual symlinks in $CLAUDE_SKILLS"
fi

mkdir -p "$CLAUDE_SKILLS"

# Link each reckon skill into ~/.claude/skills/
for skill_dir in "$RECKON_SKILLS"/*/; do
  skill_name="$(basename "$skill_dir")"
  target="$CLAUDE_SKILLS/$skill_name"
  if [ -L "$target" ] && [ "$(readlink "$target")" = "$skill_dir" ]; then
    echo "ok (already linked) $skill_name"
  else
    ln -sfn "$skill_dir" "$target"
    echo "linked (reckon) $skill_name"
  fi
done

# Ensure ~/.agents/skills → ~/.claude/skills (covers Cursor, Aider, Continue, etc.)
if [ ! -L "$AGENTS_SKILLS" ] || [ "$(readlink "$AGENTS_SKILLS")" != "$CLAUDE_SKILLS" ]; then
  mkdir -p "$(dirname "$AGENTS_SKILLS")"
  # Remove if it's a real dir or wrong symlink
  [ -e "$AGENTS_SKILLS" ] && rm -rf "$AGENTS_SKILLS"
  ln -s "$CLAUDE_SKILLS" "$AGENTS_SKILLS"
  echo "linked ~/.agents/skills → ~/.claude/skills"
else
  echo "ok ~/.agents/skills → ~/.claude/skills"
fi
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
mkdir -p "$DOCS/_shared" "$DOCS/ui"

cp "$RECKON/docs/_shared/foundation.css" "$DOCS/_shared/foundation.css"
cp "$RECKON/docs/_shared/dashboard.css"  "$DOCS/_shared/dashboard.css"
cp "$RECKON/docs/_shared/state.js"       "$DOCS/_shared/state.js"

cp "$RECKON/docs/ui/ui.jsx"          "$DOCS/ui/ui.jsx"
cp "$RECKON/docs/ui/state-loader.js" "$DOCS/ui/state-loader.js"

# Versioned shell/component files referenced by project pages
for f in "$RECKON"/docs/ui/v*.jsx; do
  [ -f "$f" ] && cp "$f" "$DOCS/ui/$(basename "$f")"
done
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

### Step 5 — Seed index.json

```bash
INDEX="$DOCS/state/$PROJECT/index.json"
if [ ! -f "$INDEX" ]; then
  python3 - <<EOF
import json, subprocess, datetime
seed = {
  "updated": datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S'),
  "project": "$PROJECT", "doc": "index",
  "data": {
    "active_sprint_id": None,
    "projects": [{"project": "$PROJECT", "path": "$DOCS",
      "published": "", "owner": subprocess.getoutput('git config user.name'),
      "plans_count": 0, "active": 0, "blocked": 0, "pending": 0, "shipped": 0,
      "last_modified": datetime.date.today().isoformat(),
      "milestones": [], "top": [], "activity30": [],
      "tests_30d": {"pass": 0, "runs": 0}}],
    "inventory": [], "sprints": [], "blockers": [], "timeline": []
  }
}
json.dump(seed, open('$INDEX', 'w'), indent=2)
print('seeded $INDEX')
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

## CSS layout

Three layers, sourced from `~/Code/reckon/docs/_shared/`:

| File | Destination in project | Role |
|---|---|---|
| `foundation.css` | `docs/_shared/foundation.css` | Design tokens — colours, typography, spacing |
| `dashboard.css` | `docs/_shared/dashboard.css` | Plan widgets — cards, badges, sprint tables |
| `state.js` | `docs/_shared/state.js` | Browser persistence, POST-aware, version-aware |

Pages link to `_shared/foundation.css` and `_shared/dashboard.css`. The reckon
server also serves these via the `/_shared/<file>` route directly from
`~/Code/reckon/docs/_shared/` — the per-project copies ensure GitHub Pages
compatibility without a running server.

UI components (`ui.jsx`, `state-loader.js`, versioned `v*.jsx`) live in
`docs/ui/` and are sourced from `~/Code/reckon/docs/ui/`.

## Cross-references

- `~/Code/reckon/skills/reckon-create/SKILL.md` — create the first plan after sync.
- `~/Code/reckon/skills/` — canonical skill source; symlinked to `~/.claude/skills/` by Step 0.
- `~/Code/reckon/reckon/serve.py` — mounts.json path, state root, /_shared/ route.
- `~/Code/reckon/docs/_shared/` — canonical CSS and state.js source.
- `~/Code/reckon/docs/ui/` — canonical JSX component source.
