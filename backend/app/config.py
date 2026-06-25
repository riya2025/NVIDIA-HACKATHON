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
    nemotron_model: str = "nvidia/llama-3.3-nemotron-super-49b-v1"
    # Faster, code-specialized model for the Developer agent's code generation.
    codegen_model: str = "qwen/qwen3-next-80b-a3b-instruct"
    # Architect only needs to emit a small JSON stack design, so it uses a fast
    # instruct model instead of the slow Nemotron reasoning model (~20s -> ~3s).
    architect_model: str = "qwen/qwen3-next-80b-a3b-instruct"
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

    # Behaviour
    demo_mode: bool = True
    request_timeout_s: float = 60.0
    cors_origins: str = "*"

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
