"""执行器：在受限环境中运行一段代码"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT= float(os.getenv("JUDGE_TIMEOUT_SECONDS",'5'))

@dataclass
class RunResult:
    exit_code: int
    stdout   : str
    stderr   : str
    time_ms  : float
    timed_out: bool

def run_python(code: str,stdin:str,timeout:float=DEFAULT_TIMEOUT) ->RunResult:
    """在临时目录中运行代码，返回运行产物
    编码 utf-8，避免Windows中文环境乱码
    """
    
    start = time.perf_counter()
    timed_out = False
    exit_code = -1
    stdout = stderr = ""
    
    try:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "main.py"
            script.write_text(code,encoding="utf-8")
            proc = subprocess.run(
                [sys.executable,str(script)],
                input=stdin,
                capture_output=True,
                text= True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                cwd = tmp
            )
            exit_code,stdout,stderr = proc.returncode,proc.stdout,proc.stderr
    
    except subprocess.TimeoutExpired:
        timed_out = True
        
    elapsed_ms = (time.perf_counter() - start)*1000
    
    return RunResult(exit_code,stdout,stderr,elapsed_ms,timed_out)
    