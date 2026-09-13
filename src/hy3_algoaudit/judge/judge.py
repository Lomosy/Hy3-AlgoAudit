"""判题器：把候选代码跑过全部测试用例，给出汇总判定"""

from __future__ import annotations

from .executor import run_python
from .result import CaseResult,JudgeResult,TestCase,Verdict

def _normalize(text:str) ->str:
    """规范化输出：去每行行尾空白+去末尾空行
    
    传统OJ约定：行尾多余空格和末尾空行不算错
    不能对整个字符串strip() 会误伤行首空格和中间空行
    """
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and lines[-1] =="":
        lines.pop()
    
    return "\n".join(lines)

def judge_python(code:str,cases:list[TestCase],timeout:float=5.0) -> JudgeResult:
    """对一段代码跑全部测试用例"""
    results: list[CaseResult] = []
    
    for case in cases:
        run = run_python(code,case.input_data,timeout=timeout)
        
        if run.timed_out:
            verdict = Verdict.TLE
        elif run.exit_code!=0:
            verdict = Verdict.RE
        elif _normalize(run.stdout) == _normalize(case.expected):
            verdict = Verdict.AC
        else:
            verdict = Verdict.WA
        
        results.append(CaseResult(verdict,run.time_ms,run.stderr))
        
        
    if all(r.verdict == Verdict.AC for r in results):
        overall = Verdict.AC
    else:
        # 汇总按严重度取最差：TLE>RE>WA
        overall = next(
            v for v in(Verdict.TLE,Verdict.RE,Verdict.WA)
            if any(r.verdict == v for r in results)
        )
    
    return JudgeResult(verdict=overall,results=results)
