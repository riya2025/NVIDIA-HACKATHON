import type { AgentStatus } from "../types";

const ICONS: Record<string, string> = {
  architect: "◆",
  frontend: "▢",
  backend: "{}",
  devops: "⚙",
  developer: "</>",
  tester: "✓",
  deployment: "▲",
  monitoring: "◉",
  rca: "?",
  healing: "✚",
};

const LABELS: Record<string, string> = {
  architect: "Architect",
  frontend: "Frontend",
  backend: "Backend",
  devops: "DevOps",
  developer: "Developer",
  tester: "Tester",
  deployment: "Deployment",
  monitoring: "Monitoring",
  rca: "RCA",
  healing: "Self-Healing",
};

export function AgentCard({
  stage,
  status,
  last,
  up,
}: {
  stage: string;
  status: AgentStatus;
  last?: string;
  // For the always-on ops agents (monitoring/rca/healing): show as "up" while
  // idle so it's clear they're armed and watching from the start.
  up?: boolean;
}) {
  const showActive = !!up && status === "idle";
  const cls = showActive ? "active" : status;
  return (
    <div className={`agent ${cls}`}>
      <div className="icon">{ICONS[stage] ?? "•"}</div>
      <div className="body">
        <div className="name">
          <span>{LABELS[stage] ?? stage} Agent</span>
          <span className="status">
            {status === "running" ? (
              <span className="spinner" />
            ) : showActive ? (
              <span className="up-dot-wrap">● up</span>
            ) : (
              status
            )}
          </span>
        </div>
        <div className="last">{last ?? (showActive ? "armed · watching" : "waiting…")}</div>
      </div>
    </div>
  );
}
