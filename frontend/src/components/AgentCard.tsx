import type { AgentStatus } from "../types";

const ICONS: Record<string, string> = {
  architect: "◆",
  developer: "</>",
  tester: "✓",
  deployment: "▲",
  monitoring: "◉",
  rca: "?",
  healing: "✚",
};

const LABELS: Record<string, string> = {
  architect: "Architect",
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
}: {
  stage: string;
  status: AgentStatus;
  last?: string;
}) {
  return (
    <div className={`agent ${status}`}>
      <div className="icon">{ICONS[stage] ?? "•"}</div>
      <div className="body">
        <div className="name">
          <span>{LABELS[stage] ?? stage} Agent</span>
          <span className="status">
            {status === "running" ? <span className="spinner" /> : status}
          </span>
        </div>
        <div className="last">{last ?? "waiting…"}</div>
      </div>
    </div>
  );
}
