export type AgentStatus = "idle" | "running" | "success" | "failed";

export const STAGES = [
  "architect",
  "frontend",
  "backend",
  "devops",
  "tester",
  "deployment",
  "monitoring",
  "rca",
  "healing",
] as const;

export type Stage = (typeof STAGES)[number];

export interface FoundryEvent {
  project_id: string;
  agent: string;
  type: string;
  message: string;
  data: Record<string, any>;
  ts: number;
}

export interface Metrics {
  status: string;
  cpu: number;
  memory: number;
  latency_ms: number;
  error_rate: number;
}

export interface Incident {
  id: string;
  title: string;
  root_cause?: string;
  action?: string;
  resolved: boolean;
}

export interface Project {
  id: string;
  name: string;
  description: string;
  architecture?: Record<string, any>;
  deploy_url?: string;
  metrics: Metrics;
  incidents: Incident[];
  pipeline_status: string;
}
