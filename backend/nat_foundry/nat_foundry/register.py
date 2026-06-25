"""AI Foundry workflow registered with the NeMo Agent Toolkit.

Exposes an `ai_foundry_architect` workflow function that turns an app
requirement into a concrete tech design using a NIM (Nemotron) LLM. NAT serves
this over FastAPI (`nat serve`) and instruments it with observability/profiling.
"""
from __future__ import annotations

# Corporate networks often intercept TLS; use the OS trust store (which holds the
# corporate root CA) so NIM/openai calls validate cleanly without disabling TLS.
try:  # pragma: no cover - best effort
    import truststore

    truststore.inject_into_ssl()
except Exception:  # noqa: BLE001
    pass

from nat.builder.builder import Builder
from nat.builder.framework_enum import LLMFrameworkEnum
from nat.builder.function_info import FunctionInfo
from nat.cli.register_workflow import register_function
from nat.data_models.component_ref import LLMRef
from nat.data_models.function import FunctionBaseConfig

ARCHITECT_SYSTEM = (
    "You are the AI Foundry Architect agent, a senior software architect. "
    "Given an application requirement, respond ONLY with a compact JSON object with keys: "
    "frontend, backend, database, deployment, rationale. Target an AWS ECS (Fargate) deployment."
)


class ArchitectConfig(FunctionBaseConfig, name="ai_foundry_architect"):
    """Configuration for the AI Foundry Architect workflow."""

    llm_name: LLMRef


@register_function(config_type=ArchitectConfig)
async def ai_foundry_architect(config: ArchitectConfig, builder: Builder):
    llm = await builder.get_llm(config.llm_name, wrapper_type=LLMFrameworkEnum.LANGCHAIN)

    async def _design(requirement: str) -> str:
        prompt = f"{ARCHITECT_SYSTEM}\n\nRequirement: {requirement}\n\nJSON design:"
        response = await llm.ainvoke(prompt)
        return getattr(response, "content", str(response))

    yield FunctionInfo.from_fn(
        _design,
        description="Turn an application requirement into a concrete tech design (JSON).",
    )
