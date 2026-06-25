import type { Project } from "./types";

export async function createProject(name: string, description: string): Promise<Project> {
  const res = await fetch("/api/projects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, description }),
  });
  if (!res.ok) throw new Error("Failed to create project");
  return res.json();
}

export async function getProject(id: string): Promise<Project> {
  const res = await fetch(`/api/projects/${id}`);
  if (!res.ok) throw new Error("Failed to fetch project");
  return res.json();
}

export async function triggerIncident(id: string): Promise<void> {
  await fetch(`/api/projects/${id}/incident`, { method: "POST" });
}

export function openEventStream(onEvent: (e: any) => void): WebSocket {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = (msg) => onEvent(JSON.parse(msg.data));
  return ws;
}
