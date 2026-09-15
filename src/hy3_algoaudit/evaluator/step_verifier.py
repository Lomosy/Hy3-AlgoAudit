"""Layer 3: step-level semantic review with two-phase first-error
localization (spec section 8.2):

Phase 1 - low-budget scan of all steps (correct / suspicious / downstream).
Phase 2 - high-budget fine verification of suspicious steps and neighbors.
"""
from __future__ import annotations

from ..llm import Budget, LLMClient
from ..schemas import (CodeBlock, ErrorType, STEP_NAMES_ZH, STEP_ORDER,
                       StepVerdict, StructuredSolution)
from ..utils import extract_json
from . import prompts


def _parse_error_type(v) -> ErrorType:
    try:
        t = ErrorType(str(v).upper().strip())
        return t
    except Exception:
        return ErrorType.NONE


def _format_reference(reference: CodeBlock | None) -> str:
    """Render the official reference code as an optional prompt section."""
    if reference is None or not (reference.code or "").strip():
        return ""
    lang = reference.language or "text"
    return (f"\n## 官方参考代码（仅用于核对该步推理的算法路线，"
            f"禁止直接照抄其内容作为判定依据）\n\n```{lang}\n"
            f"{reference.code.strip()}\n```\n")


class StepVerifier:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    # ------------------------------------------------------------ phase 1

    def quick_scan(self, problem_text: str, solution: StructuredSolution,
                   budget: Budget = Budget.LOW,
                   reference: CodeBlock | None = None) -> list[StepVerdict]:
        ref_block = _format_reference(reference)
        user = prompts.STEP_SCAN_USER.format(problem=problem_text,
                                             solution=solution.model_dump_json(indent=2),
                                             reference=ref_block)
        try:
            raw = self.llm.complete(prompts.STEP_SCAN_SYSTEM, user, budget=budget)
            data = extract_json(raw)
        except Exception:
            if budget is Budget.HIGH:
                raise
            # 低预算下推理模型可能把 token 花在推理上导致 JSON 截断，
            # 按自适应预算思想升级到 HIGH 重试一次。
            raw = self.llm.complete(prompts.STEP_SCAN_SYSTEM, user,
                                    budget=Budget.HIGH)
            data = extract_json(raw)
        by_id: dict[str, StepVerdict] = {}
        for s in data.get("steps", []):
            if not isinstance(s, dict):
                continue
            sid = str(s.get("step_id", "")).upper()
            if sid.startswith("S") and sid[1:].isdigit():
                sid = f"S{int(sid[1:])}"
            if sid not in STEP_ORDER:
                continue
            status = str(s.get("status", "unverifiable")).lower()
            if status not in ("correct", "incorrect", "suspicious",
                              "downstream", "unverifiable"):
                status = "unverifiable"
            try:
                conf = float(s.get("confidence", 0.5))
            except (TypeError, ValueError):
                conf = 0.5
            by_id[sid] = StepVerdict(
                step_id=sid, status=status, confidence=min(max(conf, 0.0), 1.0),
                error_type=_parse_error_type(s.get("error_type")),
                suspicious_claim=str(s.get("suspicious_claim", "")),
                reasoning=str(s.get("reasoning", "")),
            )
        # steps the model skipped are treated as unverifiable, not incorrect
        return [by_id.get(sid, StepVerdict(step_id=sid, status="unverifiable",
                                             confidence=0.0))
                for sid in STEP_ORDER]

    # ------------------------------------------------------------ phase 2

    def fine_verify(self, problem_text: str, solution: StructuredSolution,
                    targets: list[str], budget: Budget = Budget.HIGH,
                    reference: CodeBlock | None = None) -> list[StepVerdict]:
        """High-intensity verification of suspicious steps and neighbors."""
        ref_block = _format_reference(reference)
        target_set = set()
        for sid in targets:
            idx = STEP_ORDER.index(sid)
            for j in (idx - 1, idx, idx + 1):
                if 0 <= j < len(STEP_ORDER):
                    target_set.add(STEP_ORDER[j])
        out: list[StepVerdict] = []
        for sid in sorted(target_set):
            raw = self.llm.complete(
                prompts.STEP_FINE_VERIFY_SYSTEM.format(step_id=sid),
                prompts.STEP_FINE_VERIFY_USER.format(
                    problem=problem_text,
                    solution=solution.model_dump_json(indent=2),
                    reference=ref_block,
                    step_id=sid, step_name=STEP_NAMES_ZH[sid],
                ),
                budget=budget,
            )
            data = extract_json(raw)
            status = str(data.get("status", "unverifiable")).lower()
            if status not in ("correct", "incorrect", "suspicious", "unverifiable"):
                status = "unverifiable"
            try:
                conf = float(data.get("confidence", 0.5))
            except (TypeError, ValueError):
                conf = 0.5
            out.append(StepVerdict(
                step_id=sid, status=status, confidence=min(max(conf, 0.0), 1.0),
                error_type=_parse_error_type(data.get("error_type")),
                suspicious_claim=str(data.get("suspicious_claim", "")),
                reasoning=str(data.get("reasoning", "")),
            ))
        return out
