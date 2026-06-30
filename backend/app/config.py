"""Application configuration from environment variables / .env."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # NVIDIA NIM (OpenAI-compatible)
    nvidia_api_key: str = ""
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    # Reasoning model (RCA + default). NVIDIA's purpose-built reasoning NIM.
    nemotron_model: str = "nvidia/llama-3.3-nemotron-super-49b-v1.5"
    # Code-specialized model for the Developer agent. GLM is the fastest/most
    # complete coder currently healthy on the NIM endpoint (benchmarked).
    codegen_model: str = "z-ai/glm-5.1"
    # Planning model for the Architect (small JSON stack design). Nemotron is the
    # reasoning model; thinking is disabled in nvidia_client so output stays tight.
    architect_model: str = "nvidia/llama-3.3-nemotron-super-49b-v1.5"
    # Route the Architect through the NAT server (nat serve -> Nemotron). Slower,
    # but showcases the NeMo Agent Toolkit. Off by default for speed.
    architect_use_nat: bool = False
    # Disable TLS verification for the NVIDIA call (dev-only, for networks
    # that intercept SSL). Never enable in production.
    nvidia_insecure_ssl: bool = False
    # Disable TLS verification for ALL outbound deploy traffic (GitHub API,
    # Render API, the `vercel`/`git` CLIs). Dev-only, for SSL-intercepting
    # networks (corporate proxy / antivirus). When left False it inherits the
    # NVIDIA flag so a single NVIDIA_INSECURE_SSL=true covers everything.
    insecure_ssl: bool = False

    # NeMo Agent Toolkit (NAT) server. When set, agent reasoning is routed
    # through the NAT `nat serve` workflow instead of calling NIM directly.
    nat_url: str = ""

    # Public base URL of this control-plane API, as reachable from a browser.
    # Generated apps post client-side JS errors here so Monitoring/RCA can flag them.
    public_api_base: str = "http://localhost:8000"

    # Deployment runtime: "docker" runs the generated app as real Docker Compose
    # containers (frontend + backend); "local" serves via Vite dev + uvicorn child
    # processes. Docker falls back to local automatically if the build/up fails.
    deploy_mode: str = "docker"
    docker_build_timeout_s: int = 900
    # Host used in the deployed app's URLs (frontend, backend, injected API_BASE).
    # Empty = auto-detect this machine's LAN IP so the links work from other
    # devices on the same network; set to "localhost" to keep it machine-local,
    # or to a fixed IP/hostname to override (e.g. a public/Tailscale address).
    public_host: str = ""

    # Continuous monitoring watchdog. Runs from app startup: periodically
    # health-checks every deployed project and, when one goes unhealthy for
    # `monitor_fail_threshold` consecutive checks, automatically runs the
    # RCA -> Self-Heal flow (which restarts the container in docker mode).
    monitor_enabled: bool = True
    monitor_interval_s: float = 20.0
    monitor_fail_threshold: int = 2
    monitor_heartbeat_every: int = 6  # emit a "healthy" heartbeat every N ticks

    # Cloud deploy targets: Vercel hosts the frontend, Render hosts the FastAPI
    # backend + managed Postgres. When the matching tokens below are set, the
    # Deployment agent performs a REAL deploy and surfaces the returned URLs;
    # otherwise it runs the local Docker preview under the same Render/Vercel
    # narration so the demo always shows a live app.
    vercel_token: str = ""
    vercel_org_id: str = ""        # optional; needed for non-interactive CLI deploys
    vercel_project_id: str = ""    # optional; links to an existing Vercel project
    # Team scope (slug) for token-auth deploys. Vercel CLI 54+ requires this
    # explicitly in non-interactive mode when the token belongs to a team.
    vercel_scope: str = ""
    render_api_key: str = ""
    render_deploy_hook_url: str = ""  # simplest real trigger: POST to deploy
    render_region: str = "oregon"
    render_plan: str = "free"
    render_db_plan: str = "free"

    # GitHub: when github_token is set, the Deployment agent creates a PUBLIC
    # repo for the generated app and pushes it (real `git` push). This is what
    # the Render API path deploys from (Render clones the public repo). Needs a
    # PAT with `repo` scope (classic) or contents+administration (fine-grained).
    github_token: str = ""
    git_author_name: str = "AI Foundry"
    git_author_email: str = "deploy@aifoundry.local"
    # Retries for the GitHub create+push (the Render deploy depends on it). A
    # transient OS "too many open files" (Errno 24) — common on Windows when an
    # antivirus file-filter is in the path — usually clears on a short backoff.
    github_push_retries: int = 3

    # Supabase: managed Postgres + REST/Realtime. When supabase_url is set, the
    # database is provisioned on Supabase (instead of Render Postgres) and the
    # watchdog health-checks the Supabase project too.
    supabase_url: str = ""          # https://<ref>.supabase.co
    supabase_anon_key: str = ""     # public anon key (used for REST calls)
    supabase_db_url: str = ""       # postgresql://postgres:<pwd>@db.<ref>.supabase.co:5432/postgres

    # Managed Postgres the Deployment agent provisions on Render (default when
    # Supabase is not configured).
    db_engine: str = "postgresql"
    db_engine_version: str = "16"
    db_master_username: str = "appadmin"
    db_port: int = 5432

    # Behaviour
    demo_mode: bool = True
    request_timeout_s: float = 60.0
    cors_origins: str = "*"

    # Client-side error handling. Generated apps beacon browser JS errors back here;
    # debounce them so one broken app can't spawn an RCA storm. Set
    # client_error_rca=False to disable automatic RCA on browser errors entirely.
    client_error_rca: bool = True
    client_error_cooldown_s: float = 45.0

    # Frontend build-gate: after generating src/App.jsx we run a real `vite build`.
    # On failure the build error is fed back to the codegen model to auto-fix the
    # code, up to this many times, before falling back to a known-good scaffold.
    frontend_build_retries: int = 2
    # Backend build-gate: after generating backend/main.py we py_compile + import it.
    # On failure the error is fed back to the model to auto-fix, up to this many times,
    # before falling back to a minimal FastAPI scaffold.
    backend_build_retries: int = 2

    # --- RCA agent -----------------------------------------------------------
    # Run RCA as a LangGraph tool-calling ReAct agent (fetch_logs +
    # query_incident_history). Falls back to a single reasoning call on error.
    rca_use_react: bool = True
    # Model RCA reasons with. Must support tool calling for the ReAct loop;
    # otherwise the agent transparently falls back to a plain completion.
    rca_model: str = "nvidia/llama-3.3-nemotron-super-49b-v1.5"
    rca_max_iterations: int = 4

    # --- Caching / persistence (pluggable; zero-infra by default) ------------
    # LLM response cache backend: memory | sqlite | redis. SQLite persists across
    # restarts with no server; redis is a drop-in when a server is available.
    cache_enabled: bool = True
    cache_backend: str = "sqlite"
    cache_path: str = ".cache/llm_cache.sqlite"
    # LangGraph checkpointer + long-term store backends: memory | sqlite | redis.
    # Default memory (zero infra); sqlite/redis are best-effort (graceful
    # fallback to memory if the optional package/server is unavailable).
    checkpoint_backend: str = "memory"
    store_backend: str = "memory"
    redis_url: str = "redis://localhost:6379/0"

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def tls_verify_off(self) -> bool:
        """True when outbound TLS verification should be disabled. Inherits the
        NVIDIA flag so one NVIDIA_INSECURE_SSL=true covers all deploy traffic."""
        return self.insecure_ssl or self.nvidia_insecure_ssl


settings = Settings()
