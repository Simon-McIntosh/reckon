// Fleet prompt builder. One builder for every "generate prompt" surface —
// the single-plan button and the sprint fleet button both call it, so the
// output format never diverges.
//
//   window.buildFleetPrompt(items, state, title, opts)      — sync; items must
//       already carry decisions/followups/comments (the plan view hydrates).
//   window.buildFleetPromptAsync(items, state, title, opts) — fetches each
//       plan's live state first (the sprint surfaces use this; the lean
//       /_discover inventory has no decisions/followups).
//
// Design (settled with the lead, 2026-06-04):
//   • Fleet framing ALWAYS — even for a single plan (an efficient, grounded
//     pattern). Orchestrators are told to consult advisers at critical points.
//   • Requested items only. Dependencies are NEVER auto-dispatched as work
//     sections; they are called out SOFTLY per section for the orchestrator to
//     judge (a partial / strategy-level dependency is context, not a blocker).
//     Dependencies that are themselves requested items order the sequence.
//   • Decisions are injected LIVE, once, by the builder; the §05 handoff brief
//     must not re-list them (it goes stale). No per-section plan-URL line — the
//     Working-context preamble already says to read live plan state.

(function () {
  var NONACTIONABLE = {
    shipped: 1, done: 1, archived: 1, superseded: 1,
    abandoned: 1, reference: 1, research: 1, historical: 1,
  };
  function isActionable(p) {
    if (!p) return false;
    if (NONACTIONABLE[(p.status || "").toLowerCase()]) return false;
    if ((p.impl || 0) >= 1) return false;
    return true;
  }
  function pctStatus(p) {
    return (p && p.status ? p.status : "?") + " · " + Math.round(((p && p.impl) || 0) * 100) + "%";
  }

  function groundingBlock(projectName) {
    return ""
      + "Working context — ground yourself before acting\n"
      + "  Repository: the " + projectName + " repo. FIRST read its AGENTS.md and\n"
      + "    CLAUDE.md at the repo root — they declare the primary branch,\n"
      + "    commit/push discipline, build & test commands, compute/SLURM rules, and\n"
      + "    safety constraints. Honour them over any default behaviour.\n"
      + "  Branch: commit to the project's primary branch as declared in AGENTS.md;\n"
      + "    never create feature/topic branches unilaterally. Commit and push each\n"
      + "    coherent change.\n"
      + "  Plan state: read each plan's live decisions/followups/version before\n"
      + "    editing; record outcomes back into the plan (reckon-ship) as work lands.\n"
      + "  Advisers (mandatory at critical points): before committing to an approach,\n"
      + "    before any irreversible or outward-facing action, and before declaring\n"
      + "    work done — consult an adviser (stronger-reviewer / advisor tool) and\n"
      + "    weigh the feedback. Don't crystallise an approach or claim completion\n"
      + "    without one.\n";
  }

  function orchestrationBlock(n, projectName, title, sprintMeta) {
    var t = "\nOrchestration\n";
    if (n === 1) {
      t += "  You are the orchestrator for this task. Do the work yourself or dispatch\n"
        +  "  sub-workers as useful. Honour the locked decisions below and surface (do\n"
        +  "  not unilaterally resolve) the open ones. Pull in a dependency only if you\n"
        +  "  judge it a genuine prerequisite. Consult advisers at the critical points\n"
        +  "  named in Working context.\n";
    } else {
      t += "  You are coordinating a fleet of workers across " + n + " plans. Dispatch in\n"
        +  "  the order below; honour dependency edges. Workers whose dependencies are\n"
        +  "  satisfied may run in parallel. Honour locked decisions, never resolve open\n"
        +  "  decisions unilaterally, and consult advisers at the critical points named\n"
        +  "  in Working context.\n";
    }
    t += "\nProject: " + projectName + "\n";
    if (sprintMeta && sprintMeta.id) t += "Sprint:  " + sprintMeta.id + "\n";
    if (title) t += "Goal:    " + title + "\n";
    if (sprintMeta && sprintMeta.window) t += "Window:  " + sprintMeta.window + "\n";
    return t;
  }

  function buildSection(p, num, total, projectName, bySlug) {
    var decisions = p.decisions || [];
    var locked = decisions.filter(function(d) { return d.chosen || d.choice; });
    var open = decisions.filter(function(d) { return !(d.chosen || d.choice); });
    var fus = p.followups || [];
    var next = fus.filter(function(f) { return !(f.resolved_at || f.status === "resolved"); })[0] || fus[0];

    var lockedBlock = locked.length === 0 ? "  (none)"
      : locked.map(function(d) {
          var line = "  " + d.key + " → " + (d.chosen || d.choice);
          if (d.rationale) line += "\n      reason: " + d.rationale;
          return line;
        }).join("\n");
    var openBlock = open.length === 0 ? "  (none)"
      : open.map(function(d) { return "  " + d.key + " — " + (d.title || d.key); }).join("\n");

    var txt = "\n─── " + num + "/" + total + " · " + p.slug + " ───\n";
    txt += "Plan:   " + p.slug + "\n";
    txt += "Title:  " + (p.title || p.slug) + "\n";
    txt += "Status: " + (p.status || "?") + (p.phase ? " · " + p.phase : "") + " · " + Math.round((p.impl || 0) * 100) + "% complete\n";
    if (p.ms) txt += "MS:     " + p.ms + "\n";
    if (p.sprint) txt += "Sprint: " + p.sprint + "\n";
    if (p.justification) txt += "Why (sprint): " + p.justification + "\n";

    if (p.summary) txt += "\nSummary\n  " + p.summary + "\n";

    var deps = (p.depends_on || []).filter(Boolean);
    if (deps.length) {
      var depList = deps.map(function(slug) {
        var dp = bySlug && bySlug[slug];
        return "    " + slug + " (" + (dp ? pctStatus(dp) : "unknown") + ")";
      }).join("\n");
      txt += "\nDependencies of this plan (soft — orchestrator's call)\n"
           + "  Decide whether each is actually required for THIS task before acting:\n"
           + "  a partial or strategy-level dependency is context, not a hard blocker,\n"
           + "  and is NOT auto-dispatched. Pull one in only if you judge it a genuine\n"
           + "  prerequisite.\n" + depList + "\n";
    }

    txt += "\nLocked decisions (honour these)\n" + lockedBlock + "\n";
    txt += "\nOpen decisions (surface, do not resolve)\n" + openBlock + "\n";

    // Human feedback — highest priority for agents to read.
    var allComments = [];
    var comments = p.comments || {};
    Object.keys(comments).forEach(function(sid) {
      (comments[sid] || []).forEach(function(c) { if (c && c.body) allComments.push({ sid: sid, c: c }); });
    });
    if (allComments.length > 0) {
      txt += "\nHuman feedback (act on these — reviewer intent, highest priority)\n";
      allComments.forEach(function(item) {
        var c = item.c;
        txt += "  §" + item.sid + " · " + (c.who || "user") + " · " + (c.when || "") + "\n";
        if (c.quote) txt += "      quoted text: \"" + (c.quote.length > 200 ? c.quote.slice(0, 200) + "…" : c.quote) + "\"\n";
        txt += "      comment: " + c.body + "\n";
      });
    }

    if (next && next.prompt) {
      txt += "\n── Handoff brief" + (next.title ? " · " + next.title : "") + " ──\n";
      txt += next.prompt.replace(/\s+$/, "") + "\n";
    } else if (next) {
      txt += "\nNext-up\n  " + (next.title || "") + "\n";
      if (next.body) txt += "  " + next.body + "\n";
      txt += "\nDone-when\n  1. Land the work described.\n  2. Record a followup outcome on the plan.\n  3. Mark the driving followup resolved.\n";
    } else {
      txt += "\nDone-when\n  1. Land the work described.\n  2. Record a followup outcome on the plan.\n  3. Mark the driving followup resolved.\n";
    }
    return txt;
  }

  window.buildFleetPrompt = function buildFleetPrompt(items, state, title, opts) {
    opts = opts || {};
    var projectName = (state && state.projects && state.projects[0] && state.projects[0].project)
      || (state && state.project) || "project";
    var inv = (state && state.inventory) || [];

    var bySlug = {};
    for (var i = 0; i < inv.length; i++) bySlug[inv[i].slug] = inv[i];
    for (var j = 0; j < items.length; j++) {
      bySlug[items[j].slug] = Object.assign({}, bySlug[items[j].slug] || {}, items[j]);
    }

    // Order the REQUESTED items by their mutual dependencies (dependency-first).
    // Never recurse into non-requested plans — external deps stay soft context.
    var reqSet = {};
    for (var r = 0; r < items.length; r++) reqSet[items[r].slug] = true;
    var visited = {};
    var order = [];
    function visit(slug) {
      if (visited[slug]) return;
      visited[slug] = true;
      var p = bySlug[slug];
      if (!p) return;
      var deps = p.depends_on || [];
      for (var k = 0; k < deps.length; k++) if (reqSet[deps[k]]) visit(deps[k]);
      if (!isActionable(p)) return; // finished / reference / 100% → skip
      order.push(slug);
    }
    for (var m = 0; m < items.length; m++) visit(items[m].slug);

    var n = order.length;
    if (n === 0) {
      if (items.length === 1) {
        var rp = bySlug[items[0].slug] || items[0];
        return "(" + items[0].slug + " is " + pctStatus(rp) + " — no actionable work to dispatch. "
             + "Generate a prompt only for an in-progress plan.)";
      }
      return "(no actionable plans to include — all requested plans are complete or reference.)";
    }

    var sprintMeta = opts.sprint || null;
    var txt = groundingBlock(projectName);
    txt += orchestrationBlock(n, projectName, title, sprintMeta);

    if (n > 1) {
      txt += "\nExecution sequence (dependency-ordered within this set):\n";
      for (var idx = 0; idx < order.length; idx++) {
        var s = order[idx];
        var d2 = (bySlug[s].depends_on || []).filter(function(d) { return reqSet[d]; });
        txt += "  " + (idx + 1) + ". " + s + (d2.length ? "  (← " + d2.join(", ") + ")" : "") + "\n";
      }
      txt += "\nEach plan's section follows.\n";
    }

    for (var si = 0; si < order.length; si++) {
      txt += buildSection(bySlug[order[si]], si + 1, n, projectName, bySlug);
    }
    return txt;
  };

  // Hydrate lean inventory items with live per-plan state, then build. Sprint
  // surfaces pass /_discover inventory entries (no decisions/followups), so they
  // must hydrate or every section shows "(none)" decisions and no handoff brief.
  window.buildFleetPromptAsync = async function buildFleetPromptAsync(items, state, title, opts) {
    var projectName = (state && state.projects && state.projects[0] && state.projects[0].project)
      || (state && state.project) || "project";
    var hydrated = await Promise.all((items || []).map(async function(it) {
      try {
        var resp = await fetch("/plan/" + projectName + "/" + encodeURIComponent(it.slug), { cache: "no-store" });
        if (!resp.ok) return it;
        var j = await resp.json();
        var d = (j && j.data) || j || {};
        return Object.assign({}, it, {
          decisions: d.decisions || it.decisions || [],
          followups: d.followups || it.followups || [],
          comments: d.comments || it.comments || {},
          depends_on: d.depends_on || it.depends_on || [],
          summary: it.summary || d.summary || "",
        });
      } catch (e) {
        return it;
      }
    }));
    return window.buildFleetPrompt(hydrated, state, title, opts);
  };
})();
