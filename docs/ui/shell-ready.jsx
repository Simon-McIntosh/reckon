// Reckon shell ready module.
const { useCallback, useEffect, useMemo, useRef, useState } = React;

function ProjectStateLoadPanel({ load }) {
  const frame = {
    maxWidth: 640,
    margin: "64px auto",
    padding: "28px 32px",
    border: "1px solid var(--line)",
    borderRadius: "var(--radius-lg)",
    background: "var(--bg)",
    boxShadow: "var(--shadow)",
  };
  const label = {
    margin: 0,
    color: load.phase === "error" ? "var(--bad)" : "var(--muted)",
    fontFamily: "var(--mono)",
    fontSize: 11,
    fontWeight: 700,
    letterSpacing: ".08em",
    textTransform: "uppercase",
  };

  if (load.phase === "error") {
    const response = load.httpStatus === null
      ? (load.message || "Request failed before an HTTP response arrived")
      : `HTTP ${load.httpStatus}`;
    return React.createElement(
      "main",
      { role: "alert", style: frame },
      React.createElement("p", { style: label }, "Discovery failed"),
      React.createElement(
        "h1",
        { style: { margin: "8px 0 10px", fontSize: 22, color: "var(--ink)" } },
        "Project state unavailable",
      ),
      React.createElement(
        "p",
        { style: { margin: "0 0 20px", color: "var(--ink-2)", lineHeight: 1.55 } },
        "The project shell cannot render trusted state because discovery did not complete.",
      ),
      React.createElement(
        "dl",
        { style: { display: "grid", gridTemplateColumns: "max-content 1fr", gap: "8px 16px", margin: 0, fontSize: 13 } },
        React.createElement("dt", { style: { color: "var(--muted)" } }, "Endpoint"),
        React.createElement("dd", { style: { margin: 0 } }, React.createElement("code", null, load.endpoint)),
        React.createElement("dt", { style: { color: "var(--muted)" } }, "Response"),
        React.createElement("dd", { style: { margin: 0, fontFamily: "var(--mono)" } }, response),
      ),
    );
  }

  return React.createElement(
    "main",
    { role: "status", style: { ...frame, textAlign: "center" } },
    React.createElement("p", { style: label }, "Discovery in progress"),
    React.createElement(
      "h1",
      { style: { margin: "8px 0 10px", fontSize: 20, color: "var(--ink)" } },
      "Loading plan state…",
    ),
    React.createElement(
      "p",
      { style: { margin: 0, color: "var(--muted)", fontFamily: "var(--mono)", fontSize: 12 } },
      `${load.endpoint} · ${load.elapsedSeconds}s elapsed`,
    ),
  );
}

function ReadyGate({ children }) {
  const [ready, setReady] = useState(!window.STATE_READY);
  const [error, setError] = useState(window.STATE_ERROR || null);
  const [elapsedAt, setElapsedAt] = useState(Date.now());
  useEffect(() => {
    if (!window.STATE_READY) return undefined;
    let active = true;
    const updateElapsed = () => {
      if (active) setElapsedAt(Date.now());
    };
    const timer = window.setInterval(updateElapsed, 1000);
    window.STATE_READY.then(
      () => {
        window.clearInterval(timer);
        if (active) setReady(true);
      },
      cause => {
        window.clearInterval(timer);
        if (active) setError(cause);
      },
    );
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, []);
  const load = window.projectStateLoadView
    ? window.projectStateLoadView(error, elapsedAt)
    : {
        phase: error ? "error" : "pending",
        endpoint: "project state",
        httpStatus: null,
        message: error?.message || "",
        elapsedSeconds: 0,
      };
  if (!ready) {
    return <ProjectStateLoadPanel load={load} />;
  }
  return children;
}


window.ReckonShell = window.ReckonShell || {};
window.ReckonShell.ready = { ProjectStateLoadPanel, ReadyGate };
