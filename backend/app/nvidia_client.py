"""Wrapper around the NVIDIA NIM OpenAI-compatible endpoint (Nemotron).

Falls back to deterministic canned responses in demo mode / when no key is set,
so the full pipeline runs offline.
"""
from __future__ import annotations

from typing import Optional

from .cache import cache_get, cache_set, make_request_key
from .config import settings
from .logging_config import log

try:  # openai is optional at runtime in demo mode
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore


class NvidiaClient:
    def __init__(self) -> None:
        self._client = None
        if settings.nvidia_api_key and not settings.demo_mode and OpenAI is not None:
            # Fail fast: cap each request so a throttled/degraded NIM model falls
            # back (see complete()) instead of hanging on the SDK's 10-min default.
            timeout_s = settings.request_timeout_s
            http_client = None
            if settings.nvidia_insecure_ssl:
                import httpx

                http_client = httpx.Client(verify=False, timeout=timeout_s)
                log.warning("NVIDIA TLS verification DISABLED (dev only)")
            self._client = OpenAI(
                base_url=settings.nvidia_base_url,
                api_key=settings.nvidia_api_key,
                http_client=http_client,
                timeout=timeout_s,
                max_retries=1,
            )
            log.info("NVIDIA client live (model={})", settings.nemotron_model)
        else:
            log.info("NVIDIA client in DEMO mode (canned responses)")

    @property
    def live(self) -> bool:
        return self._client is not None

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        model: Optional[str] = None,
        max_tokens: int = 1024,
        temperature: float = 0.4,
        thinking: bool = False,
    ) -> str:
        if self._client is None:
            return _canned(prompt, system)
        target_model = model or settings.nemotron_model
        cache_key = make_request_key(
            model=target_model,
            system=system,
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            thinking=thinking,
        )
        cached = cache_get(cache_key)
        if cached is not None:
            log.debug("LLM cache hit ({})", target_model)
            return cached
        try:
            # Llama-Nemotron reasoning is toggled via the system prompt. Disable it
            # for code/JSON generation so the token budget is spent on real output,
            # not internal <think> traces. Only Nemotron understands this directive.
            if "nemotron" in target_model.lower():
                directive = "detailed thinking on" if thinking else "detailed thinking off"
                sys_content = f"{directive}\n\n{system}".strip()
            else:
                sys_content = system.strip()
            messages = [
                {"role": "system", "content": sys_content},
                {"role": "user", "content": prompt},
            ]
            resp = self._client.chat.completions.create(
                model=target_model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            content = resp.choices[0].message.content or ""
            cache_set(cache_key, content)
            return content
        except Exception as exc:  # noqa: BLE001
            log.warning("NVIDIA call failed ({}); using fallback", exc)
            return _canned(prompt, system)

    def complete_code(
        self,
        prompt: str,
        *,
        system: str = "",
        model: Optional[str] = None,
        max_tokens: int = 4096,
        temperature: float = 0.3,
        max_continuations: int = 4,
    ) -> str:
        """Like complete(), but transparently continues when the model hits the
        output-token ceiling (finish_reason == "length").

        Code generation routinely exceeds a single response's token budget; a
        truncated file ends mid-statement and never compiles. We append the
        partial output and ask the model to continue exactly where it stopped,
        concatenating the pieces into one complete file.
        """
        if self._client is None:
            return _canned(prompt, system)
        target_model = model or settings.nemotron_model
        if "nemotron" in target_model.lower():
            sys_content = f"detailed thinking off\n\n{system}".strip()
        else:
            sys_content = system.strip()
        messages = [
            {"role": "system", "content": sys_content},
            {"role": "user", "content": prompt},
        ]
        pieces: list[str] = []
        try:
            for _ in range(max_continuations + 1):
                resp = self._client.chat.completions.create(
                    model=target_model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                choice = resp.choices[0]
                chunk = choice.message.content or ""
                pieces.append(chunk)
                if getattr(choice, "finish_reason", None) != "length":
                    break
                # Truncated: feed the partial back and continue the file.
                messages.append({"role": "assistant", "content": chunk})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Continue the file from EXACTLY where you stopped. "
                            "Output ONLY the remaining code — do not repeat any "
                            "previous lines, do not add explanation or markdown."
                        ),
                    }
                )
            full = "".join(pieces)
            log.debug("complete_code: {} part(s), {} chars ({})", len(pieces), len(full), target_model)
            return full
        except Exception as exc:  # noqa: BLE001
            log.warning("NVIDIA code call failed ({}); using fallback", exc)
            return "".join(pieces) or _canned(prompt, system)


def _canned(prompt: str, system: str) -> str:
    blob = (system + " " + prompt).lower()
    if "architect" in blob:
        return (
            '{"frontend": "Next.js", "backend": "FastAPI", "database": "PostgreSQL", '
            '"deployment": "Docker + AWS ECS", '
            '"rationale": "Fast-to-ship SaaS stack; ECS Fargate for self-healing demo."}'
        )
    if "root cause" in blob or "rca" in blob:
        return (
            "Root cause: backend ECS task exited (exit code 137 / OOM). ALB target "
            "became unhealthy and /health returned 503. Recommended: roll back to the "
            "previous task-definition revision and raise the memory limit."
        )
    return "[demo] " + prompt[:200]


nvidia = NvidiaClient()
