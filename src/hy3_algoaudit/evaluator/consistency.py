"""Explanation-Code Consistency Checker (spec section 10).

Independently verifies that the natural-language explanation matches the
actual program — the key module for detecting "AC but invalid" cases.
"""
from __future__ import annotations

from ..llm import Budget, LLMClient
from ..schemas import (ErrorType, Evidence, EvidenceKind, StructuredSolution)
from ..utils import extract_json
from . import prompts


class ConsistencyChecker:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    def check(self, problem_text: str, solution: StructuredSolution,
              budget: Budget = Budget.LOW) -> Evidence:
        raw = self.llm.complete(
            prompts.CONSISTENCY_SYSTEM,
            prompts.CONSISTENCY_USER.format(
                problem=problem_text,
                solution=solution.model_dump_json(indent=2),
                language=solution.code.language,
                code=solution.code.code,
            ),
            budget=budget,
        )
        data = extract_json(raw)
        consistent = bool(data.get("consistent", True))
        try:
            conf = float(data.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        mismatches = data.get("mismatches", []) or []
        detail = "; ".join(
            f"[{m.get('aspect', '?')}] {m.get('detail', '')}" for m in mismatches
            if isinstance(m, dict)
        )
        err = ErrorType.E6
        for m in mismatches:
            if isinstance(m, dict) and str(m.get("error_type", "")).upper() in (
                    "E5", "E6"):
                err = ErrorType(str(m["error_type"]).upper())
                break
        return Evidence(
            kind=EvidenceKind.consistency,
            strength=3 if not consistent else 2,
            step_id="S7",
            error_type=err if not consistent else ErrorType.NONE,
            indicates_invalid=not consistent,
            claim=detail,
            detail=(f"题解与代码{'不一致' if not consistent else '一致'}"
                    f"（置信度 {conf:.2f}）: {detail}" if not consistent else "题解与代码一致"),
        )
