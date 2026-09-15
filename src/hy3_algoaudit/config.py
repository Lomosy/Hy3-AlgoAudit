"""Central configuration.

All settings come from environment variables (optionally via a .env file) and
are validated once at load time — fail fast, never hardcode secrets.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

from .utils import ConfigError

load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name, str(default))
    try:
        return int(raw)
    except ValueError as e:
        raise ConfigError(f"environment variable {name} must be an integer, got {raw!r}") from e


def _env_float(name: str, default: float) -> float:
    raw = _env(name, str(default))
    try:
        return float(raw)
    except ValueError as e:
        raise ConfigError(f"environment variable {name} must be a number, got {raw!r}") from e


@dataclass(frozen=True)
class SandboxLimits:
    time_limit_seconds: float = 5.0
    memory_limit_mb: int = 512
    compile_time_limit_seconds: float = 30.0


@dataclass(frozen=True)
class Settings:
    # Hy3 API (OpenAI-compatible)
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    request_timeout: float = 120.0
    extra_body: dict = field(default_factory=dict)

    # Adaptive reasoning budget
    low_budget_max_tokens: int = 4096
    high_budget_max_tokens: int = 16384
    reasoning_effort_low: str = "low"
    reasoning_effort_high: str = "high"

    # Sandbox
    sandbox_backend: str = "auto"  # auto | local | docker
    limits: SandboxLimits = field(default_factory=SandboxLimits)

    # Pipeline
    max_repair_rounds: int = 3
    difficulty_high_threshold: int = 2000  # Codeforces rating threshold

    def require_api(self) -> None:
        """Fail fast when a real model call is needed but config is missing."""
        missing = [k for k, v in (("HY3_API_KEY", self.api_key),
                                  ("HY3_BASE_URL", self.base_url),
                                  ("HY3_MODEL", self.model)) if not v]
        if missing:
            raise ConfigError(
                "missing required environment variables: " + ", ".join(missing)
                + " — copy .env.example to .env and fill in real values"
            )


def load_settings() -> Settings:
    extra_raw = _env("HY3_EXTRA_BODY")
    extra_body: dict = {}
    if extra_raw:
        try:
            parsed = json.loads(extra_raw)
            if isinstance(parsed, dict):
                extra_body = parsed
            else:
                raise ConfigError("HY3_EXTRA_BODY must be a JSON object")
        except json.JSONDecodeError as e:
            raise ConfigError(f"HY3_EXTRA_BODY is not valid JSON: {e}") from e

    backend = _env("SANDBOX_BACKEND", "auto")
    if backend not in ("auto", "local", "docker"):
        raise ConfigError("SANDBOX_BACKEND must be one of: auto, local, docker")

    return Settings(
        # 新命名 HY3_MODEL 优先，兼容旧 .env 的 HY3_MODEL_NAME
        api_key=_env("HY3_API_KEY"),
        base_url=_env("HY3_BASE_URL"),
        model=_env("HY3_MODEL") or _env("HY3_MODEL_NAME"),
        request_timeout=(_env_float("HY3_REQUEST_TIMEOUT", 0)
                         or _env_float("HY3_TIMEOUT_SECONDS", 120.0)),
        extra_body=extra_body,
        low_budget_max_tokens=_env_int("LOW_BUDGET_MAX_TOKENS", 4096),
        high_budget_max_tokens=_env_int("HIGH_BUDGET_MAX_TOKENS", 16384),
        reasoning_effort_low=_env("REASONING_EFFORT_LOW")
        or _env("HY3_REASONING_EFFORT") or "low",
        reasoning_effort_high=_env("REASONING_EFFORT_HIGH")
        or _env("HY3_REASONING_EFFORT") or "high",
        sandbox_backend=backend,
        limits=SandboxLimits(
            time_limit_seconds=_env_float("SANDBOX_TIME_LIMIT_SECONDS", 5.0),
            memory_limit_mb=_env_int("SANDBOX_MEMORY_LIMIT_MB", 512),
            compile_time_limit_seconds=_env_float("SANDBOX_COMPILE_TIME_LIMIT_SECONDS", 30.0),
        ),
        max_repair_rounds=_env_int("MAX_REPAIR_ROUNDS", 3),
        difficulty_high_threshold=_env_int("DIFFICULTY_HIGH_THRESHOLD", 2000),
    )
