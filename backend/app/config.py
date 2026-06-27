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

    # NeMo Agent Toolkit (NAT) server. When set, agent reasoning is routed
    # through the NAT `nat serve` workflow instead of calling NIM directly.
    nat_url: str = ""

    # Public base URL of this control-plane API, as reachable from a browser.
    # Generated apps post client-side JS errors here so Monitoring/RCA can flag them.
    public_api_base: str = "http://localhost:8000"

    # AWS deploy target
    aws_region: str = "ap-south-1"
    ecs_cluster: str = "ai-foundry"
    ecr_repo: str = "ai-foundry/apps"

    # Managed database the Deployment agent provisions (simulated AWS RDS/Aurora).
    db_engine: str = "aurora-postgresql"
    db_engine_version: str = "16.4"
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


settings = Settings()
