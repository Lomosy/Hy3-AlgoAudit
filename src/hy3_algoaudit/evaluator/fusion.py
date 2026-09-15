"""Evidence fusion (spec section 13).

Priority: executable counterexample > deterministic rule/sandbox > reference
facts > high-intensity semantic judgment > plain multi-model voting.

Core principle: evidence over vote counts.
"""
from __future__ import annotations

from ..schemas import (EVIDENCE_PRIORITY, ErrorType, Evidence, EvidenceKind,
                       ExecStatus, ExecutionResult, ProcessVerdict,
                       StepVerdict, step_rank)


def fuse(exec_result: ExecutionResult | None,
         step_verdicts: list[StepVerdict],
         evidence: list[Evidence]) -> ProcessVerdict:
    """Fuse execution, rule, semantic, consistency and counterexample evidence
    into a final ProcessVerdict with first causal error + root cause."""

    # ---- collect per-step incorrect verdicts (semantic layer) -------------
    incorrect_steps = {v.step_id: v for v in step_verdicts
                       if v.status == "incorrect"}

    # ---- sandbox evidence: execution failure localizes to S7 --------------
    # (unless an earlier reasoning step is already wrong — then the sandbox
    #  failure is merely downstream)
    sandbox_ev: list[Evidence] = []
    if exec_result is not None and not exec_result.accepted:
        if exec_result.status in (ExecStatus.WA, ExecStatus.RE):
            err = ErrorType.E7
            detail = f"执行失败（{exec_result.status.value}）：{exec_result.detail}"
        elif exec_result.status == ExecStatus.TLE:
            # implementation meets the spec but is too slow -> complexity issue
            err = ErrorType.E5
            detail = f"超时（TLE）：{exec_result.detail}"
        elif exec_result.status == ExecStatus.MLE:
            err = ErrorType.E5
            detail = f"超内存（MLE）：{exec_result.detail}"
        else:  # CE / SE handled separately
            err = ErrorType.E7
            detail = exec_result.detail
        sandbox_ev.append(Evidence(
            kind=EvidenceKind.sandbox, strength=4, step_id="S7",
            error_type=err, indicates_invalid=True, detail=detail,
        ))

    all_evidence = list(evidence) + sandbox_ev
    invalid_ev = [e for e in all_evidence if e.indicates_invalid]

    # ---- first error step: earliest confirmed-broken step ------------------
    first_error = None
    first_error_source: Evidence | None = None

    # a) strongest evidence wins if it pins a step earlier than semantic ones
    for e in sorted(invalid_ev,
                    key=lambda x: (-EVIDENCE_PRIORITY.get(x.kind, 0), -x.strength)):
        if first_error is None or (
                e.step_id and step_rank(e.step_id) < step_rank(first_error)):
            if e.step_id:
                first_error = e.step_id
                first_error_source = e

    # b) semantic incorrect verdicts can only pull the first error earlier
    for sid, v in incorrect_steps.items():
        if first_error is None or step_rank(sid) < step_rank(first_error):
            first_error = sid
            first_error_source = None  # resolved below from verdict

    # ---- decide error type --------------------------------------------------
    if first_error_source is not None:
        error_type = first_error_source.error_type
    elif first_error in incorrect_steps:
        error_type = incorrect_steps[first_error].error_type
    else:
        error_type = ErrorType.NONE
    if error_type is ErrorType.NONE and first_error is not None:
        # weakest fallback: implementation error at S7
        error_type = ErrorType.E7 if first_error == "S7" else ErrorType.E8

    process_valid = not invalid_ev and not incorrect_steps

    # CE alone: reasoning may be perfectly fine, the code simply doesn't
    # compile — root cause is still E7 at S7.
    if (exec_result is not None and exec_result.status == ExecStatus.CE
            and not incorrect_steps and not invalid_ev):
        process_valid = False
        first_error = "S7"
        error_type = ErrorType.E7

    # ---- confidence: strongest evidence strength, softened by disagree ----
    if invalid_ev:
        strength = max(e.strength for e in invalid_ev)
        confidence = 0.5 + 0.1 * strength  # 0.6..1.0
    else:
        confirmed_ok = [v for v in step_verdicts if v.status == "correct"]
        confidence = (sum(v.confidence for v in confirmed_ok) / len(confirmed_ok)
                      if confirmed_ok else 0.5)

    summary = _summarize(process_valid, first_error, error_type,
                         exec_result, invalid_ev)

    return ProcessVerdict(
        process_valid=process_valid,
        first_error_step=first_error,
        error_type=error_type if not process_valid else ErrorType.NONE,
        confidence=round(min(confidence, 1.0), 2),
        step_verdicts=step_verdicts,
        evidence=all_evidence,
        summary=summary,
    )


def _summarize(process_valid: bool, first_error: str | None,
               error_type: ErrorType, exec_result: ExecutionResult | None,
               invalid_ev: list[Evidence]) -> str:
    exec_txt = exec_result.status.value if exec_result else "未执行"
    if process_valid:
        if exec_result is not None and exec_result.accepted:
            return f"执行 {exec_txt}，过程审查未发现问题：完整正确解。"
        return f"执行 {exec_txt}，推理过程成立；问题主要出在实现层。"
    key = (invalid_ev[0].detail[:120] if invalid_ev else "")
    return (f"执行 {exec_txt}，首个实质性错误位于 {first_error}，"
            f"错误类型 {error_type.value}（{error_type.label}）。依据：{key}")


def compute_quadrant(exec_result: ExecutionResult | None,
                     verdict: ProcessVerdict) -> tuple[str, str]:
    """Outcome x Process quadrant (spec section 3.2)."""
    from ..schemas import QUADRANTS
    exec_ok = bool(exec_result and exec_result.accepted)
    # reasoning validity = no incorrect step before S7
    reasoning_ok = all(v.status != "incorrect"
                       for v in verdict.step_verdicts if v.step_id != "S7")
    if exec_ok and verdict.process_valid:
        key = "full_correct"
    elif exec_ok and not verdict.process_valid:
        key = "ac_but_invalid"
    elif not exec_ok and reasoning_ok:
        key = "implementation_error"
    else:
        key = "reasoning_error"
    return key, QUADRANTS[key]
