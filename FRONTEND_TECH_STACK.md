# AI Foundry — Frontend Tech Stack

The frontend is the **operator console** for the AI workforce. It uses **two complementary front-ends**:

- **NeMo Agent Toolkit Web UI** (official NVIDIA component) — agent chat, intermediate-steps visualization, and Human-in-the-Loop (HITL) approvals.
- **AI Foundry Operator Console** (custom) — ops dashboards: pipeline progress, app health, CI/CD, incidents, governance.

---

## 1. Core Framework & Language

| Tech | Version | Purpose |
|------|---------|---------|
| **React** | 18+ | UI library |
| **TypeScript** | 5+ | Type-safe development |
| **Vite** | latest | Build tool + dev server (fast HMR) |

> The official **NeMo Agent Toolkit UI** is built on **Next.js 13+ / React 18** — used as-is for chat + HITL.

---

## 2. Styling & UI Components

| Tech | Purpose |
|------|---------|
| **Tailwind CSS** | Utility-first styling |
| **shadcn/ui** | Accessible, composable component primitives |
| **Radix UI** | Headless primitives (dialogs, dropdowns, tooltips) |
| **lucide-react** | Icon set |
| **Framer Motion** | Animations / transitions |

---

## 3. State & Data Fetching

| Tech | Purpose |
|------|---------|
| **TanStack Query (React Query)** | Server state, caching, polling |
| **Zustand** | Lightweight client/global state |
| **Axios / Fetch** | HTTP requests to the backend REST API |

---

## 4. Real-Time & Streaming

| Tech | Purpose |
|------|---------|
| **WebSocket API** | Live pipeline events + agent intermediate steps |
| **Server-Sent Events (SSE)** | Streaming responses from `nat serve` (`/chat/stream`) |
| **reconnecting-websocket** | Resilient socket connections |

---

## 5. Visualization

| Tech | Purpose |
|------|---------|
| **React Flow** | Live agent topology / pipeline graph |
| **Recharts** | Metrics charts (CPU, memory, latency, error rate) |
| **react-markdown** + **rehype** | Render agent output / logs with syntax highlighting |
| **Shiki / Prism** | Code syntax highlighting for generated code |

---

## 6. Key Views (Operator Console)

| View | What it shows |
|------|---------------|
| **Pipeline View** | Live `Generate → Test → Deploy` progress per agent |
| **Dashboard** | Deployed app health: status, CPU, memory, latency (Recharts + **embedded Grafana panels**) |
| **CI/CD View** | GitHub Actions runs + AWS (ECS) deploy status + auto-fix PRs |
| **Self-Healing Console** | Incident timeline: detect → diagnose → heal, with approve/reject |
| **Governance Center** | NemoClaw network-policy approvals, violations, audit trail |
| **Reliability Lab** | NeMo Evaluator scorecards & regression trends |

---

## 7. NeMo Agent Toolkit UI (official)

| Feature | Purpose |
|---------|---------|
| **Chat interface** | Talk to the agent workforce |
| **Intermediate-steps visualization** | See agent reasoning / tool calls live |
| **Human-in-the-Loop (HITL)** | Approve/reject high-risk self-healing actions over WebSocket |
| **Dark/Light theme** | Built-in |
| **WebSocket + HTTP modes** | HITL requires WebSocket mode |

> Connect it to the backend via Settings → HTTP URL + WebSocket URL (`nat serve`).

---

## 8. Forms & Validation

| Tech | Purpose |
|------|---------|
| **React Hook Form** | Form state management |
| **Zod** | Schema validation (shared shape with backend Pydantic) |

---

## 9. Routing

| Tech | Purpose |
|------|---------|
| **React Router** | Client-side routing (custom console) |
| **Next.js App Router** | Routing for the NAT UI (official) |

---

## 10. Testing & Quality

| Tech | Purpose |
|------|---------|
| **Vitest** | Unit testing |
| **React Testing Library** | Component testing |
| **Playwright** | E2E testing (demo flow) |
| **ESLint + Prettier** | Lint + format |

---

## 11. Packaging & Deployment

| Tech | Purpose |
|------|---------|
| **Docker** | Containerized frontend |
| **Nginx** | Static serving (prod) |
| **npm / pnpm** | Dependency management |
| **Amazon S3 + CloudFront** | Static hosting + CDN |

---

## 12. Suggested Folder Structure

```
frontend/
├── src/
│   ├── main.tsx
│   ├── App.tsx
│   ├── api/
│   │   ├── client.ts        # REST client
│   │   └── socket.ts        # WebSocket hook
│   ├── store/               # Zustand stores
│   ├── components/
│   │   ├── ui/              # shadcn/ui components
│   │   ├── AgentGraph.tsx   # React Flow topology
│   │   └── MetricsChart.tsx # Recharts
│   ├── pages/
│   │   ├── Pipeline.tsx
│   │   ├── Dashboard.tsx
│   │   ├── CICD.tsx
│   │   ├── SelfHealing.tsx
│   │   ├── Governance.tsx
│   │   └── Reliability.tsx
│   └── lib/
├── index.html
├── vite.config.ts
├── tailwind.config.ts
├── package.json
└── Dockerfile
```
