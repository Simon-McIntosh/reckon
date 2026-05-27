// Prereq-aware fleet prompt builder.
// Exposes window.buildFleetPrompt(items, state, title).
//   items  — array of plan objects to include (from M.inventory or custom)
//   state  — window.STATE (provides inventory for dep resolution)
//   title  — optional context string for the Orchestration block

(function () {
  function buildSection(p, num, total, projectName) {
    var decisions = p.decisions || [];
    var locked = decisions.filter(function(d) { return d.chosen || d.choice; });
    var open = decisions.filter(function(d) { return !(d.chosen || d.choice); });
    var next = (p.followups || [])[0];

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
    txt += "Status: " + (p.status || "?") + (p.phase ? " · " + p.phase : "") + "\n";
    if (p.ms) txt += "MS:     " + p.ms + "\n";
    if (p.sprint) txt += "Sprint: " + p.sprint + "\n";
    if (p.summary) txt += "\nSummary\n  " + p.summary + "\n";
    txt += "\nState to read\n  /plan/" + projectName + "/" + p.slug + "\n";
    txt += "\nLocked decisions (honour these)\n" + lockedBlock + "\n";
    txt += "\nOpen decisions (surface, do not resolve)\n" + openBlock + "\n";
    if (next) {
      txt += "\nNext-up\n  " + (next.title || "") + "\n";
      if (next.body) txt += "  " + next.body + "\n";
      if (next.prompt) txt += "\n" + next.prompt + "\n";
    }
    txt += "\nDone-when\n  1. Land the work described.\n  2. POST followup to /plan/" + projectName + "/" + p.slug + " with outcome.\n  3. Mark driving followup resolved.\n\n";
    return txt;
  }

  window.buildFleetPrompt = function buildFleetPrompt(items, state, title) {
    var projectName = (state && state.projects && state.projects[0] && state.projects[0].project)
      || (state && state.project) || "project";
    var inv = (state && state.inventory) || [];

    // Build bySlug from inventory, merge in extra data from items
    var bySlug = {};
    for (var i = 0; i < inv.length; i++) bySlug[inv[i].slug] = inv[i];
    for (var j = 0; j < items.length; j++) {
      bySlug[items[j].slug] = Object.assign({}, bySlug[items[j].slug] || {}, items[j]);
    }

    // Topological sort (post-order = dependency-first)
    var visited = {};
    var order = [];
    function visit(slug) {
      if (visited[slug]) return;
      visited[slug] = true;
      var p = bySlug[slug];
      if (!p) return;
      var deps = p.depends_on || [];
      for (var k = 0; k < deps.length; k++) visit(deps[k]);
      order.push(slug);
    }
    for (var m = 0; m < items.length; m++) visit(items[m].slug);

    var n = order.length;
    if (n === 0) return "(no plans to include)";

    // Single plan: simplified prompt
    if (n === 1) {
      return buildSection(bySlug[order[0]], 1, 1, projectName);
    }

    // Multi-plan: Orchestration block + per-plan sections
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
      txt += buildSection(bySlug[order[si]], si + 1, n, projectName);
    }
    return txt;
  };
})();
