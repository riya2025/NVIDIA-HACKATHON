import { useEffect, useRef, useState } from "react";
import { AgentCard } from "./components/AgentCard";
import { createProject, getProject, openEventStream, triggerIncident } from "./api";
import { STAGES, type AgentStatus, type FoundryEvent, type Project } from "./types";

const DEFAULT_PROMPT =
  "A simple to-do list app: add a task with a title, mark tasks as done, " +
  "delete tasks, and filter by All / Active / Done.";

const LABELS: Record<string, string> = {
  architect: "Architect",
  frontend: "Frontend",
  backend: "Backend",
  devops: "DevOps",
  tester: "Tester",
  deployment: "Deployment",
  monitoring: "Monitoring",
  rca: "RCA",
  healing: "Self-Healing",
};

const FRONTEND_KEY = "frontend (React App)";
const BACKEND_KEY = "backend/main.py";

function fmtElapsed(ms: number): string {
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  return m > 0 ? `${m}m ${String(s % 60).padStart(2, "0")}s` : `${s}s`;
}

function fmtAgo(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  return `${m}m ${s % 60}s ago`;
}

type Theme = "light" | "dark";

function initialTheme(): Theme {
  if (typeof document !== "undefined") {
    const attr = document.documentElement.dataset.theme;
    if (attr === "light" || attr === "dark") return attr;
  }
  try {
    const saved = localStorage.getItem("foundry-theme");
    if (saved === "light" || saved === "dark") return saved;
    return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  } catch {
    return "dark";
  }
}

export default function App() {
  const [name, setName] = useState("Quick To-Do");
  const [description, setDescription] = useState(DEFAULT_PROMPT);
  const [theme, setTheme] = useState<Theme>(initialTheme);
  const [project, setProject] = useState<Project | null>(null);
  const [events, setEvents] = useState<FoundryEvent[]>([]);
  const [logQuery, setLogQuery] = useState("");
  const [statuses, setStatuses] = useState<Record<string, AgentStatus>>({});
  const [lastLog, setLastLog] = useState<Record<string, string>>({});
  const [code, setCode] = useState<Record<string, { preview: string; chars: number }>>({});
  const [pipelineStatus, setPipelineStatus] = useState("pending");
  const [monitorMsg, setMonitorMsg] = useState<string>("");
  const [monitorTs, setMonitorTs] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [connected, setConnected] = useState(false);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [now, setNow] = useState<number>(Date.now());
  const projectId = useRef<string | null>(null);
  const logsEnd = useRef<HTMLDivElement>(null);
  const liveAppRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem("foundry-theme", theme);
    } catch {
      /* storage unavailable (private mode) — non-fatal */
    }
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", theme === "light" ? "#eef2f8" : "#070b12");
  }, [theme]);

  useEffect(() => {
    const ws = openEventStream((e: FoundryEvent) => {
      if (projectId.current && e.project_id !== projectId.current) return;
      setEvents((prev) => [...prev, e]);

      if (e.type === "agent_started") setStatuses((s) => ({ ...s, [e.agent]: "running" }));
      if (e.type === "agent_completed") setStatuses((s) => ({ ...s, [e.agent]: "success" }));
      if (e.type === "agent_failed") setStatuses((s) => ({ ...s, [e.agent]: "failed" }));
      if (e.type === "agent_log") setLastLog((l) => ({ ...l, [e.agent]: e.message }));
      if (e.type === "code_preview" && e.data?.path)
        setCode((c) => ({ ...c, [e.data.path]: { preview: e.data.preview, chars: e.data.chars } }));
      if (e.type === "deployed" && e.data?.url) {
        // Auto-open the freshly deployed app in a new tab.
        try {
          window.open(e.data.url, "_blank", "noopener");
        } catch {
          /* popup blocked — the embedded iframe + link still work */
        }
      }
      if (e.type === "pipeline" && e.data?.pipeline_status)
        setPipelineStatus(e.data.pipeline_status);
      if (e.type === "incident") {
        setPipelineStatus("degraded");
        setStatuses((s) => ({ ...s, rca: "idle", healing: "idle" }));
      }
      if (e.type === "healed") setPipelineStatus("healed");

      // Continuous-monitoring signals from the watchdog (active for the whole
      // lifetime of a deployed app): heartbeat, armed notice, and alerts.
      if (
        e.agent === "monitoring" &&
        (e.type === "metrics" || e.type === "alert" || e.type === "monitoring_armed")
      ) {
        setMonitorMsg(e.message);
        setMonitorTs(Date.now());
      }

      if (projectId.current) getProject(projectId.current).then(setProject).catch(() => {});
    });
    ws.onopen = () => setConnected(true);
    ws.onclose = () => setConnected(false);
    return () => ws.close();
  }, []);

  useEffect(() => {
    logsEnd.current?.scrollIntoView({ behavior: "smooth" });
  }, [events]);

  useEffect(() => {
    if (project?.deploy_url) {
      liveAppRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }, [project?.deploy_url]);

  const terminal =
    pipelineStatus === "live" ||
    pipelineStatus === "healed" ||
    pipelineStatus === "failed";

  useEffect(() => {
    // Tick while a build is in progress OR while an app is deployed (so the
    // "monitoring active / last check Ns ago" indicator stays live).
    const ticking = (startedAt !== null && !terminal) || !!project?.deploy_url;
    if (!ticking) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [startedAt, terminal, project?.deploy_url]);

  async function onGenerate() {
    setBusy(true);
    setEvents([]);
    setStatuses({});
    setLastLog({});
    setCode({});
    setPipelineStatus("running");
    setMonitorMsg("");
    setMonitorTs(null);
    setStartedAt(Date.now());
    setNow(Date.now());
    try {
      const p = await createProject(name, description);
      projectId.current = p.id;
      setProject(p);
    } finally {
      setBusy(false);
    }
  }

  async function onIncident() {
    if (projectId.current) await triggerIncident(projectId.current);
  }

  const m = project?.metrics;
  const live = pipelineStatus === "live" || pipelineStatus === "healed";

  const completedCount = STAGES.filter((s) => statuses[s] === "success").length;
  const failed = STAGES.some((s) => statuses[s] === "failed");
  const progressPct = Math.round((completedCount / STAGES.length) * 100);
  const currentStage = STAGES.find((s) => statuses[s] === "running");
  const elapsed = startedAt !== null ? fmtElapsed(now - startedAt) : null;

  const frontendStatus: AgentStatus = statuses["frontend"] ?? "idle";
  const backendStatus: AgentStatus = statuses["backend"] ?? "idle";
  const devopsStatus: AgentStatus = statuses["devops"] ?? "idle";
  const isGenerating =
    frontendStatus === "running" ||
    backendStatus === "running" ||
    devopsStatus === "running";
  const frontendCode = code[FRONTEND_KEY];
  const backendCode = code[BACKEND_KEY];

  const logFilter = logQuery.trim().toLowerCase();
  const filteredEvents = logFilter
    ? events.filter((e) =>
        `${e.agent} ${e.message} ${e.type}`.toLowerCase().includes(logFilter),
      )
    : events;

  const activity = currentStage
    ? lastLog[currentStage] ?? `${LABELS[currentStage]} working…`
    : terminal
      ? pipelineStatus === "failed"
        ? "Pipeline failed — see event stream"
        : "Pipeline complete"
      : startedAt !== null
        ? "Working…"
        : "Idle — describe an app and click Build & Deploy";

  return (
    <div className="app">
      <header className="top">
        <div>
          <h1>
            <span className="logo-dot" /> AI Foundry
          </h1>
          <div className="subtitle">
            Autonomous Software Delivery — Generate → Deploy → Monitor → Self-Heal
          </div>
        </div>
        <div className="top-right">
          <span className={`pill ${pipelineStatus}`}>{pipelineStatus.toUpperCase()}</span>
          <button
            className="theme-toggle"
            onClick={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
            title={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
            aria-label="Toggle color theme"
          >
            {theme === "dark" ? "☀" : "☾"}
          </button>
        </div>
      </header>

      <div className="composer">
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Project name" />
        <input
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="Describe the app to build"
          style={{ flex: 2 }}
        />
        <button onClick={onGenerate} disabled={busy}>
          {busy ? "Starting…" : "Build & Deploy"}
        </button>
        <button className="danger" onClick={onIncident} disabled={!live}>
          Kill Service
        </button>
      </div>

      {startedAt !== null && (
        <div className={`statusbar ${failed ? "failed" : terminal ? "done" : "active"}`}>
          <div className="statusbar-top">
            <div className="status-activity">
              <span className={`status-dot ${currentStage ? "pulse" : ""}`} />
              <span className="status-text">{activity}</span>
            </div>
            <div className="status-meta">
              <span className="status-count">
                {completedCount}/{STAGES.length} stages
              </span>
              {elapsed && <span className="status-elapsed">⏱ {elapsed}</span>}
            </div>
          </div>

          <div className="progress-track">
            <div
              className={`progress-fill ${failed ? "failed" : terminal ? "done" : ""}`}
              style={{ width: `${progressPct}%` }}
            />
          </div>

          <div className="stepper">
            {STAGES.map((stage) => {
              const st = statuses[stage] ?? "idle";
              return (
                <div key={stage} className={`step ${st}`}>
                  <span className="step-marker">
                    {st === "success" ? "✓" : st === "failed" ? "✕" : st === "running" ? <span className="spinner" /> : ""}
                  </span>
                  <span className="step-label">{LABELS[stage]}</span>
                </div>
              );
            })}
          </div>
        </div>
      )}

      <div className="grid">
        <div className="panel">
          <h2>Agent Pipeline</h2>
          <div className="pipeline">
            {STAGES.map((stage) => (
              <AgentCard
                key={stage}
                stage={stage}
                status={statuses[stage] ?? "idle"}
                last={lastLog[stage]}
                up={
                  connected &&
                  (stage === "monitoring" || stage === "rca" || stage === "healing")
                }
              />
            ))}
          </div>
        </div>

        <div className="col-right">
          <div className="panel" ref={liveAppRef}>
            <div className="codegen-head">
              <h2 style={{ margin: 0 }}>Live Application</h2>
              {project?.deploy_url ? (
                <a
                  className="badge done-badge"
                  href={project.deploy_url}
                  target="_blank"
                  rel="noreferrer"
                  style={{ textDecoration: "none" }}
                >
                  ↗ open in new tab
                </a>
              ) : (
                statuses["deployment"] === "running" && (
                  <span className="badge live-badge">● deploying</span>
                )
              )}
            </div>
            {project?.deploy_url ? (
              <a
                className="deploy-url"
                href={project.deploy_url}
                target="_blank"
                rel="noreferrer"
              >
                {project.deploy_url} ↗
              </a>
            ) : (
              <div className="deploy-url">not deployed yet</div>
            )}

            {(project?.vercel_url ||
              project?.render_url ||
              project?.repo_url ||
              project?.local_url ||
              project?.local_api_url) && (
              <div className="deploy-targets">
                {project?.repo_url && (
                  <div className="dt-row">
                    <span className="dt-plat github">GitHub</span>
                    <span className="dt-kind">Source repo</span>
                    <a className="dt-link" href={project.repo_url} target="_blank" rel="noreferrer">
                      {project.repo_url} ↗
                    </a>
                  </div>
                )}
                {project?.vercel_url && (
                  <div className="dt-row">
                    <span className="dt-plat vercel">Vercel</span>
                    <span className="dt-kind">Frontend</span>
                    <a className="dt-link" href={project.vercel_url} target="_blank" rel="noreferrer">
                      {project.vercel_url} ↗
                    </a>
                  </div>
                )}
                {project?.render_url && (
                  <div className="dt-row">
                    <span className="dt-plat render">Render</span>
                    <span className="dt-kind">Backend API</span>
                    <a className="dt-link" href={project.render_url} target="_blank" rel="noreferrer">
                      {project.render_url} ↗
                    </a>
                    <a
                      className="dt-docs"
                      href={project.render_url.replace(/\/?$/, "/") + "docs"}
                      target="_blank"
                      rel="noreferrer"
                    >
                      Swagger /docs ↗
                    </a>
                    <span className="dt-note">builds from GitHub · live in a few min</span>
                  </div>
                )}
                {project?.local_url && (
                  <div className="dt-row">
                    <span className="dt-plat local">{project.docker ? "Local · Docker" : "Local"}</span>
                    <span className="dt-kind">Frontend (live preview)</span>
                    <a className="dt-link" href={project.local_url} target="_blank" rel="noreferrer">
                      {project.local_url} ↗
                    </a>
                  </div>
                )}
                {project?.local_api_url && (
                  <div className="dt-row">
                    <span className="dt-plat local">{project.docker ? "Local · Docker" : "Local"}</span>
                    <span className="dt-kind">Backend API (live preview)</span>
                    <a className="dt-link" href={project.local_api_url} target="_blank" rel="noreferrer">
                      {project.local_api_url} ↗
                    </a>
                    <a
                      className="dt-docs"
                      href={project.local_api_url.replace(/\/?$/, "/") + "docs"}
                      target="_blank"
                      rel="noreferrer"
                    >
                      Swagger /docs ↗
                    </a>
                  </div>
                )}
              </div>
            )}

            {project?.deploy_url && (
              <iframe
                className="app-frame"
                src={project.deploy_url}
                title="Live application preview"
              />
            )}
            <div className="metrics">
              <Metric label="Status" value={m?.status ?? "—"} good={m?.status === "healthy"} text />
              <Metric label="CPU" value={m ? `${m.cpu}%` : "—"} />
              <Metric label="Memory" value={m ? `${m.memory}%` : "—"} />
              <Metric label="Latency" value={m ? `${m.latency_ms}ms` : "—"} />
            </div>

            {project?.deploy_url && (
              <div className={`monitor-banner ${pipelineStatus === "degraded" ? "alert" : "ok"}`}>
                <span className="mon-dot" />
                <div className="mon-text">
                  <div className="mon-title">
                    Monitoring · RCA · Self-Heal —{" "}
                    {pipelineStatus === "degraded" ? "incident detected, healing…" : "active"}
                  </div>
                  <div className="mon-sub">
                    {monitorMsg || "Health-checking the live app continuously; auto-heals on failure."}
                    {monitorTs !== null && <span className="mon-time"> · {fmtAgo(now - monitorTs)}</span>}
                  </div>
                </div>
              </div>
            )}
            {project?.incidents?.map((inc) => (
              <div key={inc.id} className={`incident ${inc.resolved ? "resolved" : ""}`}>
                <div className="title">
                  {inc.resolved ? "✓ " : "⚠ "}
                  {inc.title}
                </div>
                {inc.root_cause && <div className="meta">RCA: {inc.root_cause}</div>}
                {inc.action && <div className="meta">Action: {inc.action}</div>}
              </div>
            ))}
          </div>

          {(isGenerating || frontendCode || backendCode) && (
            <div className="panel codegen">
              <div className="codegen-head">
                <h2 style={{ margin: 0 }}>Code Generation</h2>
                {isGenerating && <span className="badge live-badge">● generating</span>}
                {!isGenerating && (frontendCode || backendCode) && (
                  <span className="badge done-badge">✓ generated</span>
                )}
              </div>
              <CodeTarget
                label="Frontend — React"
                generating={frontendStatus === "running" && !frontendCode}
                info={frontendCode}
              />
              <CodeTarget
                label="Backend — FastAPI (main.py)"
                generating={backendStatus === "running" && !backendCode}
                info={backendCode}
              />
              <CodeTarget
                label="DevOps — Dockerfile / compose"
                generating={devopsStatus === "running"}
                done={devopsStatus === "success"}
                doneText="artifacts written"
              />
            </div>
          )}

          {Object.keys(code).length > 0 && (
            <div className="panel">
              <h2>Generated Code (live)</h2>
              {Object.entries(code).map(([path, info]) => (
                <div key={path} className="code-block">
                  <div className="code-head">
                    {path} <span className="chars">{info.chars.toLocaleString()} chars</span>
                  </div>
                  <pre>{info.preview}</pre>
                </div>
              ))}
            </div>
          )}

          <div className="panel">
            <div className="codegen-head">
              <h2 style={{ margin: 0 }}>Live Event Stream</h2>
              <input
                className="log-search"
                value={logQuery}
                onChange={(e) => setLogQuery(e.target.value)}
                placeholder="Search logs…"
                aria-label="Search event stream"
              />
            </div>
            <div className="logs">
              {events.length === 0 ? (
                <div className="empty">No events yet. Click “Build & Deploy”.</div>
              ) : filteredEvents.length === 0 ? (
                <div className="empty">No events match “{logQuery}”.</div>
              ) : (
                filteredEvents.map((e, i) => (
                  <div key={i} className={`logline ${e.type}`}>
                    <span className="tag">[{e.agent}]</span>
                    <span className="txt">{e.message}</span>
                  </div>
                ))
              )}
              <div ref={logsEnd} />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function CodeTarget({
  label,
  generating,
  info,
  done,
  doneText,
}: {
  label: string;
  generating: boolean;
  info?: { preview: string; chars: number };
  done?: boolean;
  doneText?: string;
}) {
  const isDone = !!info || !!done;
  const state = isDone ? "done" : generating ? "generating" : "pending";
  return (
    <div className={`code-target ${state}`}>
      <span className="ct-icon">
        {isDone ? "✓" : generating ? <span className="spinner" /> : "○"}
      </span>
      <span className="ct-label">{label}</span>
      <span className="ct-status">
        {info
          ? `${info.chars.toLocaleString()} chars`
          : done
            ? doneText ?? "done"
            : generating
              ? "writing…"
              : "queued"}
      </span>
    </div>
  );
}

function Metric({
  label,
  value,
  good,
  text,
}: {
  label: string;
  value: string;
  good?: boolean;
  text?: boolean;
}) {
  const cls = text ? (good ? "ok" : value !== "—" ? "bad" : "") : "";
  return (
    <div className="metric">
      <div className="label">{label}</div>
      <div className={`value ${cls}`}>{value}</div>
    </div>
  );
}
