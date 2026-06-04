// Prereq-aware fleet prompt builder.
// Exposes window.buildFleetPrompt(items, state, title, opts).
//   items  — array of plan objects to include (from M.inventory or custom)
//   state  — window.STATE (provides inventory for dep resolution)
//   title  — optional context string for the Orchestration block
//   opts   — { expandDeps?: boolean }  default true.
//            true  → fleet mode: walk depends_on and emit a full section per
//                    incomplete prerequisite (sprint-level orchestration).
//            false → handoff mode: emit ONLY the requested plan(s); their
//                    prerequisites appear as a one-line context note, never as
//                    work sections. The single-plan "generate prompt" button
//                    passes false — you asked for THIS plan's handoff, not its
//                    whole dependency tree.

(function () {
  // A plan is actionable (worth dispatching a worker at) only if it is neither
  // a finished/non-work doc nor already 100% complete. Reference/research docs,
  // archived/superseded/abandoned plans, and anything at impl≥1 carry no work.
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

  function buildSection(p, num, total, projectName, bySlug) {
    var decisions = p.decisions || [];
    var locked = decisions.filter(function(d) { return d.chosen || d.choice; });
    var open = decisions.filter(function(d) { return !(d.chosen || d.choice); });
    // Drive from the first UNRESOLVED followup (the live next step), not just [0].
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

    var txt = "─── " + num + "/" + total + " · " + p.slug + " ───\n";
    txt += "Plan:   " + p.slug + "\n";
    txt += "Title:  " + (p.title || p.slug) + "\n";
    txt += "Status: " + (p.status || "?") + (p.phase ? " · " + p.phase : "") + " · " + Math.round((p.impl || 0) * 100) + "% complete\n";
    if (p.ms) txt += "MS:     " + p.ms + "\n";
    if (p.sprint) txt += "Sprint: " + p.sprint + "\n";

    if (p.summary) txt += "\nSummary\n  " + p.summary + "\n";

    // Prerequisites — context only. The worker reads their state; it does NOT
    // re-do them. Incomplete prerequisites are flagged so the worker coordinates.
    var deps = (p.depends_on || []).filter(Boolean);
    if (deps.length) {
      txt += "\nPrerequisites (context — read their state, do not re-do)\n";
      txt += deps.map(function(slug) {
        var dp = bySlug && bySlug[slug];
        var note = dp ? pctStatus(dp) : "unknown";
        return "  " + slug + " (" + note + ")" + (dp && !isActionable(dp) ? "" : "  ⚠ still open — coordinate");
      }).join("\n") + "\n";
    }

    // Human feedback — highest priority for agents to read
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

    txt += "\nState to read\n  /plan/" + projectName + "/" + p.slug + "\n";
    txt += "\nLocked decisions (honour these)\n" + lockedBlock + "\n";
    txt += "\nOpen decisions (surface, do not resolve)\n" + openBlock + "\n";

    if (next && next.prompt) {
      // The authored §05 followup prompt IS the handoff — the carefully written
      // launch brief. Surface it as the task. It carries its own Done-when, so
      // do NOT append a generic trailer (that produced the confusing duplicate).
      txt += "\n── Handoff brief" + (next.title ? " · " + next.title : "") + " ──\n";
      txt += next.prompt.replace(/\s+$/, "") + "\n\n";
    } else if (next) {
      txt += "\nNext-up\n  " + (next.title || "") + "\n";
      if (next.body) txt += "  " + next.body + "\n";
      txt += "\nDone-when\n  1. Land the work described.\n  2. POST followup to /plan/" + projectName + "/" + p.slug + " with outcome.\n  3. Mark driving followup resolved.\n\n";
    } else {
      // No followup at all — generic fallback so the section is still actionable.
      txt += "\nDone-when\n  1. Land the work described.\n  2. POST followup to /plan/" + projectName + "/" + p.slug + " with outcome.\n  3. Mark driving followup resolved.\n\n";
    }
    return txt;
  }

  window.buildFleetPrompt = function buildFleetPrompt(items, state, title, opts) {
    opts = opts || {};
    var expandDeps = opts.expandDeps !== false; // default true (fleet); handoff passes false
    var projectName = (state && state.projects && state.projects[0] && state.projects[0].project)
      || (state && state.project) || "project";
    var inv = (state && state.inventory) || [];

    // Build bySlug from inventory, merge in richer data from items.
    var bySlug = {};
    for (var i = 0; i < inv.length; i++) bySlug[inv[i].slug] = inv[i];
    for (var j = 0; j < items.length; j++) {
      bySlug[items[j].slug] = Object.assign({}, bySlug[items[j].slug] || {}, items[j]);
    }

    // Resolve dispatch order. In fleet mode, walk depends_on (dependency-first)
    // and include only ACTIONABLE prerequisites. In handoff mode, take the
    // requested items as-is. Either way, non-actionable plans (shipped / done /
    // reference / research / archived / superseded / abandoned / 100%) never
    // become work sections.
    var visited = {};
    var order = [];
    function visit(slug) {
      if (visited[slug]) return;
      visited[slug] = true;
      var p = bySlug[slug];
      if (!p) return;
      if (expandDeps) {
        var deps = p.depends_on || [];
        for (var k = 0; k < deps.length; k++) visit(deps[k]);
      }
      if (!isActionable(p)) return; // finished / reference / 100% → skip
      order.push(slug);
    }
    for (var m = 0; m < items.length; m++) visit(items[m].slug);

    var n = order.length;
    if (n === 0) {
      // Nothing to dispatch. If a single plan was requested, explain why rather
      // than emitting an empty work prompt for a done/reference/100% plan.
      if (items.length === 1) {
        var rp = bySlug[items[0].slug] || items[0];
        return "(" + items[0].slug + " is " + pctStatus(rp) + " — no actionable work to dispatch. "
             + "Generate a prompt only for an in-progress plan.)";
      }
      return "(no actionable plans to include — all requested plans are complete or reference.)";
    }

    // Single plan: clean handoff, no orchestration wrapper.
    if (n === 1) {
      return buildSection(bySlug[order[0]], 1, 1, projectName, bySlug);
    }

    // Multi-plan: Orchestration block + per-plan sections.
    var txt = "Orchestration\n  You are coordinating a fleet of workers across " + n + " plans.\n";
    txt += "  Dispatch in the order below; honour dependency edges.\n";
    txt += "  Workers whose dependencies are satisfied may run in parallel.\n\n";
    txt += "Project: " + projectName + "\n";
    if (title) txt += "Goal:    " + title + "\n";
    txt += "\nExecution sequence (resolved from depends_on):\n";
    for (var idx = 0; idx < order.length; idx++) {
      var slug = order[idx];
      var p = bySlug[slug];
      var deps = (p && p.depends_on || []).filter(function(d) { return bySlug[d]; });
      txt += "  " + (idx + 1) + ". " + slug;
      if (deps.length) txt += "  (← " + deps.join(", ") + ")";
      txt += "\n";
    }
    txt += "\nEach plan's detail follows below.\n\n";
    for (var si = 0; si < order.length; si++) {
      txt += buildSection(bySlug[order[si]], si + 1, n, projectName, bySlug);
    }
    return txt;
  };
})();
