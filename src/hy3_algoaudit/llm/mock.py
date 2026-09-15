"""Scripted mock LLM client.

Enables end-to-end pipeline, benchmark dry-runs, and unit tests without any
network access or API key. Routes are matched by keyword against the user
prompt; first match wins.
"""
from __future__ import annotations

from typing import Callable

from .client import Budget, TokenUsage

Route = tuple[str, Callable[[str], str]]


class MockLLMClient:
    """routes: ordered list of (keyword, responder). responder gets the full
    user prompt and returns the raw completion text."""

    def __init__(self, routes: list[Route]):
        if not routes:
            raise ValueError("MockLLMClient needs at least one route")
        self.routes = routes
        self.usage = TokenUsage()

    def complete(
        self,
        system: str,
        user: str,
        budget: Budget = Budget.LOW,
        json_mode: bool = True,
    ) -> str:
        for keyword, responder in self.routes:
            if keyword in user:
                self.usage.calls += 1
                self.usage.prompt_tokens += len(system) + len(user)
                out = responder(user)
                self.usage.completion_tokens += len(out)
                return out
        raise ValueError(
            "MockLLMClient: no route matched prompt starting with "
            f"{user[:80]!r} — add a (keyword, responder) route"
        )
