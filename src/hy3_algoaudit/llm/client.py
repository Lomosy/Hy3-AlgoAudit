"""Hy3 client over an OpenAI-compatible HTTPS endpoint.

Token budgets are part of the adaptive reasoning scheduling (spec section 12):
`Budget.LOW` for routine semantic judgments, `Budget.HIGH` for hard / low
confidence / conflicting cases.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from ..config import Settings
from ..utils import AuditError


class Budget(str, Enum):
    LOW = "low"
    HIGH = "high"


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def add(self, other: "TokenUsage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.calls += other.calls


class LLMClient(Protocol):
    usage: TokenUsage

    def complete(
        self,
        system: str,
        user: str,
        budget: Budget = Budget.LOW,
        json_mode: bool = True,
    ) -> str: ...


class Hy3Client:
    """OpenAI-compatible chat client for Hy3."""

    def __init__(self, settings: Settings):
        settings.require_api()
        try:
            from openai import OpenAI  # imported lazily so the lib works without it
        except ImportError as e:  # pragma: no cover
            raise AuditError(
                "the 'openai' package is required for Hy3Client: pip install openai>=1.30"
            ) from e
        self.settings = settings
        self.usage = TokenUsage()
        self._client = OpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=settings.request_timeout,
        )

    def complete(
        self,
        system: str,
        user: str,
        budget: Budget = Budget.LOW,
        json_mode: bool = True,
    ) -> str:
        if budget is Budget.HIGH:
            max_tokens = self.settings.high_budget_max_tokens
            effort = self.settings.reasoning_effort_high
        else:
            max_tokens = self.settings.low_budget_max_tokens
            effort = self.settings.reasoning_effort_low

        extra: dict = dict(self.settings.extra_body)
        if effort:
            extra.setdefault("reasoning_effort", effort)

        params: dict = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
        if extra:
            params["extra_body"] = extra
        if json_mode:
            # Best effort: not all OpenAI-compatible backends support it.
            params["response_format"] = {"type": "json_object"}

        try:
            resp = self._client.chat.completions.create(**params)
        except TypeError:
            # Backend rejected response_format — retry without it.
            params.pop("response_format", None)
            resp = self._client.chat.completions.create(**params)

        if resp.usage:
            self.usage.prompt_tokens += resp.usage.prompt_tokens or 0
            self.usage.completion_tokens += resp.usage.completion_tokens or 0
        self.usage.calls += 1

        content = ""
        reasoning = ""
        if resp.choices:
            msg = resp.choices[0].message
            content = msg.content or ""
            # Some reasoning models put the answer in the reasoning field.
            reasoning = getattr(msg, "reasoning_content", None) or ""

        def _has_json(t: str) -> bool:
            return "{" in t or "[" in t

        # Reasoning models sometimes burn the whole budget on reasoning and
        # emit truncated prose in content. Prefer whichever field actually
        # carries a JSON payload; otherwise return content as-is so the
        # caller's parser raises a precise error.
        if _has_json(reasoning) and not _has_json(content):
            content = reasoning
        if not content.strip():
            raise AuditError("model returned an empty completion")
        return content
