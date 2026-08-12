const CREW_POLL_INTERVAL_MS = 3000;

function formatCrewElapsed(seconds) {
  if (seconds == null || Number.isNaN(Number(seconds))) return "—";
  const total = Math.max(0, Math.floor(Number(seconds)));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const remainder = total % 60;
  if (hours) return `${hours}h ${String(minutes).padStart(2, "0")}m`;
  if (minutes) return `${minutes}m ${String(remainder).padStart(2, "0")}s`;
  return `${remainder}s`;
}

function formatCrewActivity(stamp) {
  if (!stamp) return "—";
  const parsed = new Date(stamp);
  if (Number.isNaN(parsed.getTime())) return stamp;
  return parsed.toLocaleString([], {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function CrewView() {
  const [runs, setRuns] = useState([]);
  const [error, setError] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [refreshedAt, setRefreshedAt] = useState(null);

  useEffect(() => {
    let active = true;

    const poll = async () => {
      try {
        const response = await fetch("/crew", { cache: "no-store" });
        if (!response.ok) throw new Error(`crew route returned ${response.status}`);
        const payload = await response.json();
        if (!Array.isArray(payload.runs)) throw new Error("crew route returned no run list");
        if (!active) return;
        setRuns(payload.runs);
        setError("");
        setRefreshedAt(new Date());
      } catch (cause) {
        if (!active) return;
        setError(cause instanceof Error ? cause.message : "crew route unavailable");
      } finally {
        if (active) setLoaded(true);
      }
    };

    poll();
    const timer = window.setInterval(poll, CREW_POLL_INTERVAL_MS);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, []);

  const phaseStyle = (phase) => ({
    display: "inline-block",
    minWidth: 58,
    padding: "2px 7px",
    borderRadius: 999,
    fontFamily: "var(--mono)",
    fontSize: 11,
    textAlign: "center",
    color: phase === "done" ? "var(--good)" : phase === "working" ? "var(--accent)" : "var(--muted)",
    background: phase === "done" ? "var(--good-2)" : phase === "working" ? "var(--accent-2)" : "var(--bg-2)",
  });

  return (
    <div className="r-page wide">
      <div style={{ display: "flex", alignItems: "baseline", gap: 12, marginBottom: 18 }}>
        <div>
          <div className="r-eyebrow">Crew</div>
          <h1 style={{ margin: "2px 0 0", fontSize: 22 }}>Live runs</h1>
        </div>
        <span style={{ fontFamily: "var(--mono)", color: "var(--muted)", fontSize: 11 }}>
          {runs.length} visible · polls every {CREW_POLL_INTERVAL_MS / 1000}s
          {refreshedAt ? ` · refreshed ${refreshedAt.toLocaleTimeString()}` : ""}
        </span>
      </div>

      {error && (
        <div role="status" style={{ color: "var(--bad)", marginBottom: 12 }}>
          {error}
        </div>
      )}

      {!loaded ? (
        <div style={{ color: "var(--muted)" }}>Loading live runs…</div>
      ) : runs.length === 0 ? (
        <div style={{ color: "var(--muted)" }}>No live runs.</div>
      ) : (
        <div className="r-plan-html">
          <table aria-label="Live crew runs">
            <thead>
              <tr>
                <th>Member</th>
                <th>Role</th>
                <th>Plan</th>
                <th>Section</th>
                <th>Model</th>
                <th>Effort</th>
                <th>Elapsed</th>
                <th>Phase</th>
                <th>Last activity</th>
                <th>Gate</th>
              </tr>
            </thead>
            <tbody>
              {runs.map(run => (
                <tr key={`${run.project || ""}:${run.run_id}`}>
                  <td><code>{run.member || "—"}</code></td>
                  <td>{run.role || "—"}</td>
                  <td>
                    {run.plan_href ? <a href={run.plan_href}>{run.plan || "—"}</a> : (run.plan || "—")}
                    {run.sprint_href && (
                      <div><a href={run.sprint_href} style={{ fontSize: 11, color: "var(--muted)" }}>sprint</a></div>
                    )}
                  </td>
                  <td>{run.plan_href ? <a href={run.plan_href}>{run.section || "—"}</a> : (run.section || "—")}</td>
                  <td><code>{run.model || "—"}</code></td>
                  <td>{run.effort || "—"}</td>
                  <td><code>{formatCrewElapsed(run.elapsed_seconds)}</code></td>
                  <td><span style={phaseStyle(run.phase)}>{run.phase || "idle"}</span></td>
                  <td title={run.last_activity || ""}>{formatCrewActivity(run.last_activity)}</td>
                  <td>{run.gate || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

window.CrewView = CrewView;
