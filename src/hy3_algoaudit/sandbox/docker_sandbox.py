"""Docker-based sandbox (optional, stronger isolation).

Runs each submission in a disposable container:
  docker run --rm --network=none --memory=<N>g --cpus=1 \
      -v <workdir>:/work -w /work <image> <cmd>

Requires Docker on PATH; use SANDBOX_BACKEND=auto to fall back to local.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from ..config import SandboxLimits
from ..schemas import CodeBlock
from ..utils import SandboxError
from .base import PreparedProgram, RunOutcome, Sandbox, _require_lang

DEFAULT_IMAGE = "python:3.11-slim"


def docker_available() -> bool:
    return shutil.which("docker") is not None


class DockerSandbox(Sandbox):
    """Prepares files in a temp dir and executes them inside a locked-down
    container. Compilation for cpp uses the same container image (g++ must be
    present in the image; use an image like gcc:latest for cpp problems)."""

    def __init__(self, limits: SandboxLimits, image: str | None = None):
        if not docker_available():
            raise SandboxError("Docker not found on PATH")
        self.limits = limits
        self.image = image or DEFAULT_IMAGE
        if self.image == DEFAULT_IMAGE:
            # slim python image cannot compile cpp
            self.cpp_image = "gcc:latest"
        else:
            self.cpp_image = self.image

    def _docker(self, args: list[str], timeout: float, **kw) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["docker", *args], capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace", **kw,
        )

    def prepare(self, code: CodeBlock, workdir: Path | None = None) -> PreparedProgram:
        lang = _require_lang(code)
        wd = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="hy3audit-d-"))
        wd.mkdir(parents=True, exist_ok=True)

        if lang == "python":
            (wd / "solution.py").write_text(code.code, encoding="utf-8")
            cmd = ["python3", "/work/solution.py"]
            return PreparedProgram(workdir=wd, command=cmd, language=lang)

        (wd / "solution.cpp").write_text(code.code, encoding="utf-8")
        mem_g = max(1, self.limits.memory_limit_mb // 1024)
        try:
            cp = self._docker(
                ["run", "--rm", "--network=none", f"--memory={mem_g}g", "--cpus=1",
                 "-v", f"{wd}:/work", "-w", "/work", self.cpp_image,
                 "bash", "-c", "g++ -O2 -std=c++17 solution.cpp -o solution"],
                timeout=self.limits.compile_time_limit_seconds,
            )
        except subprocess.TimeoutExpired:
            return PreparedProgram(workdir=wd, command=[], language=lang,
                                   compile_ok=False, compile_error="compilation timed out")
        if cp.returncode != 0:
            return PreparedProgram(workdir=wd, command=[], language=lang,
                                   compile_ok=False,
                                   compile_error=(cp.stderr or cp.stdout or "compile failed"))
        return PreparedProgram(workdir=wd,
                               command=["/work/solution"], language=lang)

    def run_once(self, program: PreparedProgram, stdin: str,
                 time_limit_seconds: float | None = None,
                 memory_limit_mb: int | None = None) -> RunOutcome:
        if not program.compile_ok:
            raise SandboxError("program has a compilation error; cannot run")
        tl = time_limit_seconds or self.limits.time_limit_seconds
        ml = memory_limit_mb or self.limits.memory_limit_mb
        mem_g = max(1, ml // 1024)

        image = self.cpp_image if program.language == "cpp" else self.image
        outcome = RunOutcome()
        start = time.perf_counter()
        try:
            cp = self._docker(
                ["run", "--rm", "--network=none", f"--memory={mem_g}g", "--cpus=1",
                 "--memory-swap=-1" if mem_g > 0 else "--memory-swap=-1",
                 "-v", f"{program.workdir}:/work", "-w", "/work", image,
                 *program.command],
                timeout=tl + 10,  # container overhead margin
                input=stdin,
            )
            outcome.stdout = cp.stdout
            outcome.stderr = cp.stderr
            outcome.exit_code = cp.returncode
        except subprocess.TimeoutExpired as e:
            outcome.timed_out = True
            outcome.stdout = (e.stdout or b"").decode("utf-8", "replace") \
                if isinstance(e.stdout, bytes) else (e.stdout or "")
            outcome.stderr = (e.stderr or b"").decode("utf-8", "replace") \
                if isinstance(e.stderr, bytes) else (e.stderr or "")
        finally:
            outcome.time_ms = (time.perf_counter() - start) * 1000.0
        # Docker OOM-kill shows up as exit 137
        if outcome.exit_code in (137, 134):
            outcome.mem_exceeded = True
            outcome.runtime_error  # noqa: B018 - status derived in judge
        return outcome

    def cleanup(self, program: PreparedProgram) -> None:
        shutil.rmtree(program.workdir, ignore_errors=True)
