"""
The LLM seam for the root-cause drafter.

Kept deliberately thin and behind a Protocol so the drafter service never imports
the Anthropic SDK directly: tests and offline runs use `StubLLMClient`, real runs
use `AnthropicLLMClient`. The prompt-building and (pure, testable) JSON parsing
live here; DB work lives in the drafter service.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Protocol

# Substrings that mark a transient, retryable provider error (overload / rate limit
# / gateway). Providers all surface these as 429 / 5xx — a short backoff usually
# rides out the spike, which is what was making Gemini story generation flaky.
_TRANSIENT = (
    "503", "502", "504", "500", "429", "overloaded", "unavailable",
    "rate limit", "ratelimit", "timeout", "timed out", "temporarily",
)


def _call_with_retries(do, attempts: int = 3, base_delay: float = 1.0):
    """Call `do()`, retrying transient provider errors with exponential backoff."""
    for attempt in range(attempts):
        try:
            return do()
        except Exception as exc:  # noqa: BLE001 — classify by message, re-raise if not transient
            transient = any(s in str(exc).lower() for s in _TRANSIENT)
            if not transient or attempt == attempts - 1:
                raise
            time.sleep(base_delay * (2 ** attempt))


@dataclass
class ChangeContext:
    """One causing change, as evidence for the model."""

    identifier: str
    title: str
    body: str = ""
    diff: str = ""


@dataclass
class DraftContext:
    """Everything the model sees about one incident."""

    incident_title: str
    incident_description: str = ""
    severity: str = ""
    changes: list[ChangeContext] = field(default_factory=list)


@dataclass
class RootCauseDraft:
    summary: str
    category: str = ""
    detail: str = ""
    confidence: float = 0.5


class LLMClient(Protocol):
    def draft_root_causes(self, context: DraftContext) -> list[RootCauseDraft]:
        ...


# ---------------------------------------------------------------------------
# Prompt + parsing (pure, unit-tested)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are a senior site-reliability engineer performing root-cause analysis. "
    "Given an incident and the code change(s) believed to have caused it, draft 1-3 "
    "candidate root causes. Respond with ONLY a JSON array, no prose. Each element: "
    '{"summary": <=120 chars, "category": short snake_case, "detail": 2-4 sentences, '
    '"confidence": float 0-1 for how likely this is the true cause}.'
)


def build_user_prompt(context: DraftContext) -> str:
    lines = [
        f"INCIDENT: {context.incident_title}",
    ]
    if context.severity:
        lines.append(f"SEVERITY: {context.severity}")
    if context.incident_description:
        lines.append(f"DESCRIPTION:\n{context.incident_description}")
    if context.changes:
        lines.append("\nSUSPECTED CAUSING CHANGES:")
        for c in context.changes:
            lines.append(f"\n--- change {c.identifier}: {c.title}")
            if c.body:
                lines.append(c.body)
            if c.diff:
                lines.append(f"diff:\n{c.diff}")
    else:
        lines.append("\n(No causing change linked yet; infer from the incident alone.)")
    return "\n".join(lines)


def parse_drafts(text: str) -> list[RootCauseDraft]:
    """Parse the model's JSON array into drafts, defensively and without raising."""
    payload = _extract_json_array(text)
    if payload is None:
        return []
    drafts: list[RootCauseDraft] = []
    for item in payload:
        if not isinstance(item, dict) or not item.get("summary"):
            continue
        drafts.append(
            RootCauseDraft(
                summary=str(item["summary"])[:512],
                category=str(item.get("category", ""))[:64],
                detail=str(item.get("detail", "")),
                confidence=_clamp_confidence(item.get("confidence", 0.5)),
            )
        )
    return drafts


def _extract_json_array(text: str):
    text = text.strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, list) else None
    except json.JSONDecodeError:
        pass
    # Model wrapped the array in prose or a code fence: grab the outermost [...].
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, list) else None
    except json.JSONDecodeError:
        return None


def _clamp_confidence(value) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.5


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------

class AnthropicLLMClient:
    """Real client. Reads credentials from the environment via the SDK."""

    def __init__(self, model: str, max_tokens: int = 2000):
        self.model = model
        self.max_tokens = max_tokens

    def draft_root_causes(self, context: DraftContext) -> list[RootCauseDraft]:
        import anthropic  # imported lazily so offline paths never need the SDK

        from .app_settings import get_api_key

        client = anthropic.Anthropic(api_key=get_api_key("anthropic") or None)
        response = client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_user_prompt(context)}],
        )
        text = "".join(
            block.text for block in response.content if block.type == "text"
        )
        return parse_drafts(text)


class TaskRunner(Protocol):
    def run(self, system: str, user: str) -> str:
        ...


class RunnerLLMClient:
    """
    Root-cause drafting over ANY provider: wraps a TaskRunner and reuses the same
    prompt + pure JSON parsing, so Gemini/OpenAI/Anthropic all work the same way.
    """

    def __init__(self, runner: "TaskRunner"):
        self.runner = runner

    def draft_root_causes(self, context: DraftContext) -> list[RootCauseDraft]:
        return parse_drafts(self.runner.run(SYSTEM_PROMPT, build_user_prompt(context)))


class AnthropicTaskRunner:
    """Single-shot completion via the Anthropic SDK."""

    def __init__(self, model: str, max_tokens: int = 4000):
        self.model = model
        self.max_tokens = max_tokens

    def run(self, system: str, user: str) -> str:
        import anthropic

        from .app_settings import get_api_key

        client = anthropic.Anthropic(api_key=get_api_key("anthropic") or None)
        response = _call_with_retries(lambda: client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        ))
        return "".join(
            block.text for block in response.content if block.type == "text"
        )


class OpenAITaskRunner:
    """Single-shot completion via the OpenAI SDK (ChatGPT models)."""

    def __init__(self, model: str, max_tokens: int = 4000):
        self.model = model
        self.max_tokens = max_tokens

    def run(self, system: str, user: str) -> str:
        from openai import OpenAI

        from .app_settings import get_api_key

        client = OpenAI(api_key=get_api_key("openai") or None)
        response = _call_with_retries(lambda: client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        ))
        return response.choices[0].message.content or ""


class GeminiTaskRunner:
    """Single-shot completion via the Google GenAI SDK (Gemini models)."""

    def __init__(self, model: str, max_tokens: int = 4000):
        self.model = model
        self.max_tokens = max_tokens

    def run(self, system: str, user: str) -> str:
        from google import genai
        from google.genai import types

        from .app_settings import get_api_key

        client = genai.Client(api_key=get_api_key("gemini") or None)
        response = _call_with_retries(lambda: client.models.generate_content(
            model=self.model,
            contents=user,
            config=types.GenerateContentConfig(system_instruction=system),
        ))
        return response.text or ""


class StubTaskRunner:
    """
    Offline runner that reports whether the prompt carried graph context. Lets the
    harness and its tests verify wiring without an API call — the with-variant must
    receive context, the without-variant must not.
    """

    MARKER = "CONNECTED CONTEXT"

    def run(self, system: str, user: str) -> str:
        had_context = self.MARKER in user
        return f"[stub answer] had_context={had_context}; prompt_chars={len(user)}"


class EchoTaskRunner:
    """
    Offline runner that echoes the prompt it received. Used by the evaluation
    harness to demonstrate the metric wiring without an API: whatever context was
    in the prompt shows up in the "answer", so the judge can measure the delta
    between the with- and without-context runs. NOT a real model result.
    """

    def run(self, system: str, user: str) -> str:
        return user


class StubLLMClient:
    """Deterministic offline client for tests, demos, and no-key environments."""

    def __init__(self, drafts: list[RootCauseDraft] | None = None):
        self._drafts = drafts

    def draft_root_causes(self, context: DraftContext) -> list[RootCauseDraft]:
        if self._drafts is not None:
            return list(self._drafts)
        # A plausible canned draft derived from the context.
        return [
            RootCauseDraft(
                summary=f"Change to {context.changes[0].identifier[:12]} introduced the fault"
                if context.changes
                else "Unhandled edge case in the affected code path",
                category="logic-error",
                detail="Stub draft (no LLM call). Replace with AnthropicLLMClient in "
                "production to get a real analysis of the incident and diff.",
                confidence=0.5,
            )
        ]
