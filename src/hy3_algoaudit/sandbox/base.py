"""Sandbox primitives and backend interface.

Security model (spec section 7.2):
- no network access (Docker: --network=none; local: trusted-machine fallback)
- CPU time limit per test (subprocess timeout)
- memory limit (best-effort: psutil polling; Docker: --memory)
- isolated temp working directory per submission, destroyed afterwards
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import SandboxLimits
from ..schemas import CodeBlock
from ..utils import SandboxError


@dataclass
class PreparedProgram:
    """A program made executable by the sandbox (compiled or scripted)."""

    workdir: Path
    command: list[str]
    language: str
    compile_ok: bool = True
    compile_error: str = ""


@dataclass
class RunOutcome:
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    mem_exceeded: bool = False
    time_ms: float = 0.0

    @property
    def runtime_error(self) -> bool:
        return (not self.timed_out and not self.mem_exceeded
                and self.exit_code is not None and self.exit_code != 0)


class Sandbox:
    """Backend interface: prepare once -> run many -> cleanup."""

    limits: SandboxLimits

    def prepare(self, code: CodeBlock, workdir: Path | None = None) -> PreparedProgram:
        raise NotImplementedError

    def run_once(
        self,
        program: PreparedProgram,
        stdin: str,
        time_limit_seconds: float | None = None,
        memory_limit_mb: int | None = None,
    ) -> RunOutcome:
        raise NotImplementedError

    def cleanup(self, program: PreparedProgram) -> None:
        raise NotImplementedError


def _require_lang(code: CodeBlock) -> str:
    lang = code.language
    if lang not in ("python", "cpp"):
        raise SandboxError(f"unsupported language {lang!r} (supported: python, cpp)")
    return lang
