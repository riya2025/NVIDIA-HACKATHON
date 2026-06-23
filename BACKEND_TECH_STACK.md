# AI Foundry — Backend Tech Stack

The backend is the **control plane** for the autonomous agent workforce: it orchestrates the 7 agents, runs action-taking agents inside secure sandboxes, talks to NVIDIA inference, and streams everything to the UI.

---

## 1. Core Language & Runtime

| Tech | Version | Purpose |
|------|---------|---------|
| **Python** | 3.11+ | Primary backend language |
| **FastAPI** | latest | REST + WebSocket API framework |
| **Uvicorn** | latest | ASGI server (with `uvloop`/`httptools`) |
| **Pydantic v2** | latest | Schemas, validation, settings |

---

## 2. NVIDIA Agentic Stack

| Tech | Role | Notes |
|------|------|-------|
| **NVIDIA NemoClaw** | Secure agent runtime | OpenShell sandboxes, network policy + operator approval, routed inference, lifecycle mgmt ([repo](https://github.com/NVIDIA/NemoClaw)) |
| **NVIDIA OpenShell** | Sandbox/hardening | Capability drops, process limits, egress control |
| **NeMo Agent Toolkit (NAT)** | Observability & profiling | `IntermediateStepManager`, telemetry exporters, `nat serve`, official Web UI |
| **NVIDIA NIM — Llama Nemotron** | Reasoning + code generation | Hosted via `build.nvidia.com` (OpenAI-compatible API) |
| **NVIDIA NIM — Embeddings** | Vector embeddings for RAG | Used by RCA agent over logs/runbooks |
| **NeMo Retriever** | RAG pipeline | Root-cause analysis over logs |
| **NeMo Evaluator** | Reliability / regression / smoke tests | Tester + post-heal verification |

---

## 3. The 7-Agent Workforce (modules)

| Agent | Key libraries |
|-------|---------------|
| **Architect** | NIM Nemotron (reasoning), Pydantic (spec schema) |
| **Developer** | NemoClaw **LangChain Deep Agents Code** harness, code-gen via Nemotron |
| **Tester** | `pytest`, `ruff` (lint), `bandit`/`semgrep` (security), NeMo Evaluator — run in OpenShell sandbox |
| **Deployment** | Render API client (`httpx`), Docker build, GitHub API |
| **Monitoring** | Prometheus client, NAT exporters, webhook receivers |
| **RCA** | NIM Nemotron + NeMo Retriever (RAG) |
| **Self-Healing** | NAT `automatic_retries` (`patch_with_retry`), Render/GitHub APIs behind NemoClaw approval |

---

## 4. Orchestration & Agent Frameworks

| Tech | Purpose |
|------|---------|
| **NeMo Agent Toolkit** | Instrumentation, profiling, workflow serving (`nat serve`) |
| **LangChain / LangGraph** | Agent + tool orchestration (framework-agnostic, wrapped by NAT) |
| **LangChain Deep Agents Code** | Coding-agent harness (NemoClaw-supported) |

---

## 5. Data & State

| Tech | Purpose |
|------|---------|
| **PostgreSQL** | Projects, runs, incidents, audit log |
| **SQLAlchemy** + **Alembic** | ORM + migrations |
| **Redis** | Task queue / agent message bus + caching |
| **Milvus** (vector DB) | Logs, runbooks, embeddings for RAG |
| **MinIO / S3** | Trace artifacts, eval reports, generated code bundles |

> For the hackathon MVP, Postgres + an in-memory event bus is enough; Redis/Milvus/MinIO are "scale-up" choices.

---

## 6. Integrations (CI/CD & Deploy)

| Tech | Purpose |
|------|---------|
| **GitHub Actions API + Webhooks** | Monitor `workflow_run`, `check_run`, `deployment_status`; re-run jobs; auto-fix PRs |
| **Render API + Webhooks** | Deploy, rollback, restart, health/status events |
| **httpx** | Async HTTP client for all external APIs |
| **HMAC verification** | Signed-webhook validation (production safety) |

---

## 7. Observability & Telemetry

| Tech | Purpose |
|------|---------|
| **OpenTelemetry** | Distributed tracing of agent steps |
| **Phoenix / Langfuse** | NAT-compatible LLM/agent trace viewers |
| **Prometheus** | Metrics (CPU, memory, latency, error rate) |
| **Grafana** | Dashboards (optional) |
| **structlog** | Structured JSON logging |

---

## 8. Real-Time Communication

| Tech | Purpose |
|------|---------|
| **WebSockets** (FastAPI) | Live pipeline events + agent intermediate steps to the UI |
| **SSE / chat-stream** | NAT-compatible streaming endpoints (`/chat/stream`, `/generate/stream`) |

---

## 9. Security & Governance

| Tech | Purpose |
|------|---------|
| **NemoClaw network policy + operator approval** | Gate high-risk egress (git push, prod rollback) |
| **OpenShell sandbox hardening** | Capability drops, process/resource limits |
| **OAuth2 / JWT** | API auth |
| **Secret management** | `.env` (dev), vault/secret store (prod); never commit keys |

---

## 10. Testing & Quality

| Tech | Purpose |
|------|---------|
| **pytest** + **pytest-asyncio** | Unit/integration tests |
| **ruff** | Lint + format |
| **mypy** | Static typing |
| **bandit / semgrep** | Security scanning |
| **NeMo Evaluator** | Agent reliability / regression scoring |

---

## 11. Packaging & Deployment

| Tech | Purpose |
|------|---------|
| **Docker** | Containerized backend |
| **uv** / **pip** | Dependency management |
| **render.yaml** | Render deploy manifest |
| **GitHub Actions** | CI for the platform itself (build, test, deploy) |

---

## 12. Suggested Folder Structure

```
backend/
├── app/
│   ├── main.py              # FastAPI app (REST + WebSocket)
│   ├── config.py            # Pydantic settings (NVIDIA key, Render, DB)
│   ├── nvidia_client.py     # NIM Nemotron (OpenAI-compatible) client
│   ├── events.py            # Async event bus for live streaming
│   ├── models.py            # Pydantic schemas + state
│   ├── orchestrator.py      # Runs the 7-agent pipeline
│   ├── agents/
│   │   ├── base.py
│   │   ├── architect.py
│   │   ├── developer.py
│   │   ├── tester.py
│   │   ├── deployment.py
│   │   ├── monitoring.py
│   │   ├── rca.py
│   │   └── healing.py
│   ├── integrations/
│   │   ├── github.py        # Actions API + webhooks
│   │   └── render.py        # Render API + webhooks
│   └── api/
│       ├── projects.py
│       └── ws.py
├── requirements.txt
├── Dockerfile
└── render.yaml
```
