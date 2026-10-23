"""
Provider-aware LLM settings: which provider to use, per-provider API keys and
models, and a factory for the active TaskRunner.

Supports Anthropic (Claude), OpenAI (ChatGPT) and Google (Gemini). Each provider
has its own stored key + model; an env var always overrides the stored key so
production/CI can inject one. The selected provider decides which runner the
link-drafter and story narrator use — everything downstream is provider-agnostic
(it only sees a TaskRunner).
"""

from __future__ import annotations

import os

from django.conf import settings as django_settings

from ..models import Setting

PROVIDERS = {
    "anthropic": {"label": "Anthropic (Claude)", "env": ("ANTHROPIC_API_KEY",)},
    "openai": {"label": "OpenAI (ChatGPT)", "env": ("OPENAI_API_KEY",),
               "default_model": "gpt-4o"},
    "gemini": {"label": "Google (Gemini)", "env": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
               "default_model": "gemini-2.0-flash"},
}
DEFAULT_PROVIDER = "anthropic"

_SEL = "llm_provider"


def _default_model(provider: str) -> str:
    if provider == "anthropic":
        return django_settings.GRAPH_LLM_MODEL
    return PROVIDERS[provider].get("default_model", "")


def get_provider() -> str:
    p = Setting.get(_SEL, DEFAULT_PROVIDER)
    return p if p in PROVIDERS else DEFAULT_PROVIDER


def set_provider(provider: str) -> None:
    if provider in PROVIDERS:
        Setting.put(_SEL, provider)


def _env_key(provider: str) -> str:
    for name in PROVIDERS[provider]["env"]:
        if os.environ.get(name):
            return os.environ[name]
    return ""


def get_api_key(provider: str | None = None) -> str:
    provider = provider or get_provider()
    return _env_key(provider) or Setting.get(f"llm_key:{provider}", "")


def set_api_key(provider: str, value: str) -> None:
    value = (value or "").strip()
    key = f"llm_key:{provider}"
    if value:
        Setting.put(key, value)
    else:
        Setting.objects.filter(name=key).delete()


def get_model(provider: str | None = None) -> str:
    provider = provider or get_provider()
    return Setting.get(f"llm_model:{provider}", "") or _default_model(provider)


def set_model(provider: str, value: str) -> None:
    value = (value or "").strip()
    key = f"llm_model:{provider}"
    if value:
        Setting.put(key, value)
    else:
        Setting.objects.filter(name=key).delete()


def key_source(provider: str | None = None) -> str:
    provider = provider or get_provider()
    if _env_key(provider):
        return "env"
    if Setting.get(f"llm_key:{provider}"):
        return "stored"
    return "none"


def key_hint(provider: str | None = None) -> str:
    key = get_api_key(provider)
    return f"…{key[-4:]}" if len(key) >= 4 else ""


def is_configured(provider: str | None = None) -> bool:
    return bool(get_api_key(provider))


def active_task_runner():
    """The TaskRunner for the selected provider (raises if no key)."""
    provider = get_provider()
    if not is_configured(provider):
        raise RuntimeError(f"No API key configured for {provider}.")
    from .llm import AnthropicTaskRunner, GeminiTaskRunner, OpenAITaskRunner

    runners = {
        "anthropic": AnthropicTaskRunner,
        "openai": OpenAITaskRunner,
        "gemini": GeminiTaskRunner,
    }
    return runners[provider](model=get_model(provider))


def status() -> dict:
    """UI status: the selected provider and per-provider configuration."""
    selected = get_provider()
    return {
        "provider": selected,
        "model": get_model(selected),
        "configured": is_configured(selected),
        "providers": [
            {
                "id": pid,
                "label": meta["label"],
                "model": get_model(pid),
                "configured": is_configured(pid),
                "source": key_source(pid),
                "hint": key_hint(pid),
            }
            for pid, meta in PROVIDERS.items()
        ],
    }
