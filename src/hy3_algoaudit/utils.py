"""Shared utilities: safe JSON extraction from LLM output, file IO, errors."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


class AuditError(Exception):
    """Base typed error for the whole system."""


class ConfigError(AuditError):
    """Raised when configuration is missing or invalid at startup."""


class LLMOutputError(AuditError):
    """Raised when model output cannot be parsed into the expected schema."""


class SandboxError(AuditError):
    """Raised when the sandbox itself fails (not the candidate program)."""


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def read_text(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def write_text(path: str | Path, content: str) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> dict[str, Any]:
    """Extract the first JSON object from raw model output.

    Tolerates markdown fences, leading prose, and trailing commentary.
    Raises LLMOutputError if nothing parseable is found.
    """
    if not text or not text.strip():
        raise LLMOutputError("empty model output")

    candidates: list[str] = []

    # 1) fenced blocks first (most reliable)
    for m in _FENCE_RE.finditer(text):
        candidates.append(m.group(1).strip())

    # 2) raw text
    candidates.append(text.strip())

    # 3) balanced-brace scan over the raw text
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start : i + 1])
                    break
        start = text.find("{", start + 1)

    for cand in candidates:
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue

    raise LLMOutputError(f"no valid JSON object found in output: {text[:200]!r}")
