"""判题器：把候选代码跑过全部测试用例，给出汇总判定"""

from __future__ import annotations

import re

from .executor import run_python
from .result import CaseResult,JudgeResult,TestCase,Verdict

# ---------------------------------------------------------------- 输出比对
COMPARISON_EXACT = "exact"
COMPARISON_FLOAT = "float_tolerance"
FLOAT_TOLERANCE = 1e-6

# 能被当作浮点数的 token（整数也匹配，但整数走字符串相等分支即可）
_FLOAT_TOKEN_RE = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?$")

def _normalize(text:str) ->str:
    """规范化输出：去每行行尾空白+去末尾空行
    
    传统OJ约定：行尾多余空格和末尾空行不算错
    不能对整个字符串strip() 会误伤行首空格和中间空行
    """
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and lines[-1] =="":
        lines.pop()
    
    return "\n".join(lines)

def outputs_match(
    actual: str,
    expected: str,
    comparison: str = COMPARISON_EXACT,
    tolerance: float = FLOAT_TOLERANCE,
) -> bool:
    """按 token 比较输出。
    
    为什么需要容差模式：部分题目（如 Codeforces 437D The Child and Zoo）
    的期望输出是固定精度的浮点数（16.6666666667），而数据集自带的正确解
    打印的是 Python 默认精度（16.666666666666668）。Codeforces 上这类题
    由特殊判题器按 1e-6 相对误差判定，精确字符串比对必然把它误判成 WA。
    实测在 11 道抽样题里就出现了 1 例，比例不可忽略。
    
    容差模式只放宽「双方都能解析为数字」的 token，其余 token 仍需精确相等，
    因此不会掩盖真正的答案错误。
    """
    a_tokens = _normalize(actual).split()
    e_tokens = _normalize(expected).split()
    if len(a_tokens) != len(e_tokens):
        return False
    if comparison != COMPARISON_FLOAT:
        return a_tokens == e_tokens
    for got, want in zip(a_tokens, e_tokens):
        if got == want:
            continue
        if not (_FLOAT_TOKEN_RE.match(got) and _FLOAT_TOKEN_RE.match(want)):
            return False
        got_value, want_value = float(got), float(want)
        if abs(got_value - want_value) > tolerance * max(1.0, abs(want_value)):
            return False
    return True

def judge_python(
    code:str,
    cases:list[TestCase],
    timeout:float=5.0,
    comparison:str=COMPARISON_EXACT,
) -> JudgeResult:
    """对一段代码跑全部测试用例"""
    results: list[CaseResult] = []
    
    for case in cases:
        run = run_python(code,case.input_data,timeout=timeout)
        
        if run.timed_out:
            verdict = Verdict.TLE
        elif run.exit_code!=0:
            verdict = Verdict.RE
        elif outputs_match(run.stdout, case.expected, comparison):
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
