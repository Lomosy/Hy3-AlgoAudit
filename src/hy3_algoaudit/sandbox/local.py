"""Local subprocess sandbox (default backend).

- python: executed with `python -I` (isolated mode: no user site, no env vars)
- cpp: compiled with g++ -O2 -std=c++17 if available
- per-test wall-clock timeout; best-effort memory monitoring via psutil
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
import threading
from pathlib import Path

from ..config import SandboxLimits
from ..schemas import CodeBlock
from ..utils import SandboxError
from .base import PreparedProgram, RunOutcome, Sandbox, _require_lang

try:
    import psutil  # optional
    _HAS_PSUTIL = True
except ImportError:  # pragma: no cover
    _HAS_PSUTIL = False


def _memory_probe(proc: subprocess.Popen, limit_mb: int, stop: threading.Event,
                  flag: list[bool]) -> None:
    """Poll the process tree's RSS; set flag[0] and kill on exceed."""
    try:
        ps = psutil.Process(proc.pid)
    except psutil.Error:
        return
    while not stop.is_set():
        try:
            total = ps.memory_info().rss
            for child in ps.children(recursive=True):
                total += child.memory_info().rss
            if total > limit_mb * 1024 * 1024:
                flag[0] = True
                for p in [ps, *ps.children(recursive=True)]:
                    try:
                        p.kill()
                    except psutil.Error:
                        pass
                return
        except psutil.Error:
            return
        stop.wait(0.05)


class LocalSandbox(Sandbox):
    def __init__(self, limits: SandboxLimits):
        self.limits = limits

    # ---- prepare -------------------------------------------------------

    def prepare(self, code: CodeBlock, workdir: Path | None = None) -> PreparedProgram:
        lang = _require_lang(code)
        wd = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="hy3audit-"))
        wd.mkdir(parents=True, exist_ok=True)

        if lang == "python":
            src = wd / "solution.py"
            src.write_text(code.code, encoding="utf-8")
            cmd = [sys.executable, "-I", str(src)]
            return PreparedProgram(workdir=wd, command=cmd, language=lang)

        # cpp
        gxx = shutil.which("g++") or shutil.which("g++.exe")
        if not gxx:
            raise SandboxError("g++ not found on PATH; cannot compile cpp submissions")
        src = wd / "solution.cpp"
        src.write_text(code.code, encoding="utf-8")
        exe = wd / "solution.exe" if sys.platform == "win32" else wd / "solution"
        try:
            cp = subprocess.run(
                [gxx, "-O2", "-std=c++17", str(src), "-o", str(exe)],
                capture_output=True, text=True, timeout=self.limits.compile_time_limit_seconds,
                cwd=str(wd), encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired:
            return PreparedProgram(workdir=wd, command=[], language=lang,
                                   compile_ok=False, compile_error="compilation timed out")
        if cp.returncode != 0:
            return PreparedProgram(workdir=wd, command=[], language=lang,
                                   compile_ok=False,
                                   compile_error=(cp.stderr or cp.stdout or "compile failed"))
        return PreparedProgram(workdir=wd, command=[str(exe)], language=lang)

    # ---- run -----------------------------------------------------------

    def run_once(self, program: PreparedProgram, stdin: str,
                 time_limit_seconds: float | None = None,
                 memory_limit_mb: int | None = None) -> RunOutcome:
        if not program.compile_ok:
            raise SandboxError("program has a compilation error; cannot run")
        tl = time_limit_seconds or self.limits.time_limit_seconds
        ml = memory_limit_mb or self.limits.memory_limit_mb

        outcome = RunOutcome()
        start = time.perf_counter()
        try:
            proc = subprocess.Popen(
                program.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(program.workdir),
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as e:
            raise SandboxError(f"failed to launch program: {e}") from e

        stop = threading.Event()
        mem_flag = [False]
        monitor = None
        if _HAS_PSUTIL and ml > 0:
            monitor = threading.Thread(
                target=_memory_probe, args=(proc, ml, stop, mem_flag), daemon=True)
            monitor.start()

        try:
            out, err = proc.communicate(input=stdin, timeout=tl)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                out, err = proc.communicate(timeout=2)
            except (subprocess.TimeoutExpired, ValueError):
                out, err = "", ""
            outcome.timed_out = True
        finally:
            stop.set()
            if monitor is not None:
                monitor.join(timeout=1)

        outcome.stdout = out or ""
        outcome.stderr = err or ""
        outcome.exit_code = proc.returncode
        outcome.time_ms = (time.perf_counter() - start) * 1000.0
        outcome.mem_exceeded = mem_flag[0]
        return outcome

    # ---- cleanup ---------------------------------------------------------

    def cleanup(self, program: PreparedProgram) -> None:
        shutil.rmtree(program.workdir, ignore_errors=True)
