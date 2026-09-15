"""Layer 1: deterministic rule & structure checks — no model calls.

Anything reliably decidable by rules never goes to the LLM (spec 7.1).
"""
from __future__ import annotations

import re

from ..schemas import (ErrorType, Evidence, EvidenceKind, StructuredSolution,
                       STEP_ORDER)

# missing step -> most likely root cause at that step
_MISSING_STEP_ERROR = {
    "S1": ErrorType.E1, "S2": ErrorType.E8, "S3": ErrorType.E2,
    "S4": ErrorType.E3, "S5": ErrorType.E5, "S6": ErrorType.E4,
    "S7": ErrorType.E7,
}

_COMPLEXITY_RE = re.compile(r"O\s*\(")
_SUPPORTED_LANGS = {"python", "cpp"}


def structural_check(solution: StructuredSolution) -> list[Evidence]:
    """Return evidence list; `indicates_invalid=True` items are hard failures."""
    ev: list[Evidence] = []
    present = {s.step_id for s in solution.steps}

    # 1) completeness of S1..S7
    for sid in STEP_ORDER:
        step = solution.step(sid)
        if step is None or len(step.content.strip()) < 10:
            err = _MISSING_STEP_ERROR[sid]
            ev.append(Evidence(
                kind=EvidenceKind.rule, strength=4, step_id=sid,
                error_type=err, indicates_invalid=True,
                detail=f"缺少或内容过空的步骤 {sid}（{_step_name(sid)}），"
                       f"对应错误类型 {err.value} {err.label}",
            ))

    # 2) S5 must contain complexity notation
    s5 = solution.step("S5")
    if s5 and s5.content.strip():
        if not _COMPLEXITY_RE.search(s5.content):
            ev.append(Evidence(
                kind=EvidenceKind.rule, strength=4, step_id="S5",
                error_type=ErrorType.E5, indicates_invalid=True,
                detail="S5 复杂度分析未给出 Big-O 记号",
            ))

    # 3) code must exist and language must be supported
    if len(solution.code.code.strip()) < 20:
        ev.append(Evidence(
            kind=EvidenceKind.rule, strength=4, step_id="S7",
            error_type=ErrorType.E7, indicates_invalid=True,
            detail="缺少完整可执行代码（代码过短或为空）",
        ))
    elif solution.code.language not in _SUPPORTED_LANGS:
        ev.append(Evidence(
            kind=EvidenceKind.rule, strength=4, step_id="S7",
            error_type=ErrorType.E7, indicates_invalid=True,
            detail=f"不支持的代码语言：{solution.code.language!r}",
        ))

    # 4) soft check: claims logarithmic complexity but code looks like
    #    nested double loops and S3 never mentions 二分/堆/排序等
    if s5 and _COMPLEXITY_RE.search(s5.content):
        m = re.findall(r"O\s*\(([^)]*)\)", s5.content)
        claimed = " ".join(m)
        code = solution.code.code
        s3 = solution.step("S3")
        s3_text = (s3.content.lower() if s3 else "")
        divide = any(k in s3_text for k in ("二分", "堆", "排序", "分治", "sort", "heap",
                                            "binary", "divide", "log"))
        loop_count = len(re.findall(r"\bfor\s+|while\s+", code))
        if "log" in claimed and loop_count >= 2 and not divide:
            ev.append(Evidence(
                kind=EvidenceKind.rule, strength=2, step_id="S5",
                error_type=ErrorType.E5, indicates_invalid=False,
                claim=f"S5 声称复杂度含 log（{claimed.strip()}），"
                      f"但代码含 {loop_count} 个循环且 S3 未提及对数级算法",
                detail="复杂度声明与代码结构疑似不符，移交一致性/反例模块复核",
            ))

    return ev


def _step_name(sid: str) -> str:
    from ..schemas import STEP_NAMES_ZH
    return STEP_NAMES_ZH.get(sid, sid)
