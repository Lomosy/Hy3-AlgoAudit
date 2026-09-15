"""Structural checker + evidence fusion unit tests."""
from hy3_algoaudit.evaluator.fusion import compute_quadrant, fuse
from hy3_algoaudit.evaluator.structural import structural_check
from hy3_algoaudit.schemas import (CodeBlock, ErrorType, Evidence,
                                   EvidenceKind, ExecutionResult, StepContent,
                                   StepVerdict, StructuredSolution)

GOOD_STEPS = [
    StepContent(step_id="S1", title="题意", content="求最大子数组和，元素可为负。" * 2),
    StepContent(step_id="S2", title="性质", content="f(i)=max(a[i], f(i-1)+a[i])，最优子结构。" * 2),
    StepContent(step_id="S3", title="算法", content="Kadane 线性扫描动态规划。" * 2),
    StepContent(step_id="S4", title="推导", content="归纳证明转移覆盖所有情形。" * 2),
    StepContent(step_id="S5", title="复杂度", content="时间复杂度 O(n)，空间 O(1)。"),
    StepContent(step_id="S6", title="边界", content="全负数组时初始化 a[0]，n=1 正确。" * 2),
    StepContent(step_id="S7", title="实现", content="单循环 Kadane 实现，从 stdin 读入并输出 best。"),
]
CODE = CodeBlock(language="python", code="print(sum([1]))" + " # pad pad pad")
SOL = StructuredSolution(steps=GOOD_STEPS, code=CODE)


def test_structural_clean():
    ev = structural_check(SOL)
    assert not any(e.indicates_invalid for e in ev)


def test_structural_missing_steps():
    broken = SOL.model_copy(update={"steps": GOOD_STEPS[:4]})
    ev = structural_check(broken)
    invalid = [e for e in ev if e.indicates_invalid]
    assert any(e.step_id == "S5" and e.error_type == ErrorType.E5 for e in invalid)
    assert any(e.step_id in ("S6", "S7") for e in invalid)


def test_structural_missing_complexity():
    s5_bad = GOOD_STEPS[4].model_copy(update={"content": "很快，肯定能过。" * 3})
    steps = GOOD_STEPS[:4] + [s5_bad] + GOOD_STEPS[5:]
    ev = structural_check(StructuredSolution(steps=steps, code=CODE))
    assert any(e.error_type == ErrorType.E5 and e.indicates_invalid for e in ev)


def test_fusion_all_correct_with_ac():
    exec_res = ExecutionResult(status="AC", passed=4, total=4)
    verdicts = [StepVerdict(step_id=f"S{i}", status="correct") for i in range(1, 8)]
    v = fuse(exec_res, verdicts, evidence=[])
    assert v.process_valid
    assert v.first_error_step is None


def test_fusion_semantic_first_error():
    exec_res = ExecutionResult(status="WA", passed=2, total=4)
    verdicts = [StepVerdict(step_id="S1", status="correct"),
                StepVerdict(step_id="S2", status="incorrect",
                            error_type=ErrorType.E2),
                StepVerdict(step_id="S3", status="downstream"),
                StepVerdict(step_id="S4", status="downstream"),
                StepVerdict(step_id="S5", status="correct"),
                StepVerdict(step_id="S6", status="correct"),
                StepVerdict(step_id="S7", status="correct")]
    v = fuse(exec_res, verdicts, evidence=[])
    assert not v.process_valid
    assert v.first_error_step == "S2"          # causal first error, not S7
    assert v.error_type == ErrorType.E2


def test_fusion_counterexample_beats_voting():
    cex = Evidence(kind=EvidenceKind.counterexample, strength=5, step_id="S6",
                   error_type=ErrorType.E4, indicates_invalid=True,
                   detail="反例执行确认")
    exec_res = ExecutionResult(status="AC", passed=4, total=4)
    verdicts = [StepVerdict(step_id=f"S{i}", status="correct") for i in range(1, 8)]
    v = fuse(exec_res, verdicts, evidence=[cex])
    assert not v.process_valid
    assert v.first_error_step == "S6"
    assert v.error_type == ErrorType.E4


def test_quadrant_labels():
    exec_ac = ExecutionResult(status="AC", passed=1, total=1)
    verdicts_ok = [StepVerdict(step_id=f"S{i}", status="correct") for i in range(1, 8)]
    v_ok = fuse(exec_ac, verdicts_ok, [])
    assert compute_quadrant(exec_ac, v_ok)[0] == "full_correct"

    abi = Evidence(kind=EvidenceKind.consistency, strength=3, step_id="S7",
                   error_type=ErrorType.E5, indicates_invalid=True)
    v_abi = fuse(exec_ac, verdicts_ok, [abi])
    assert compute_quadrant(exec_ac, v_abi)[0] == "ac_but_invalid"

    exec_wa = ExecutionResult(status="WA", passed=0, total=1)
    v_wa = fuse(exec_wa, verdicts_ok, [])
    assert compute_quadrant(exec_wa, v_wa)[0] == "implementation_error"
    assert v_wa.first_error_step == "S7"
    assert v_wa.error_type == ErrorType.E7

    verdicts_bad = [StepVerdict(step_id="S1", status="correct"),
                    StepVerdict(step_id="S2", status="incorrect",
                                error_type=ErrorType.E8)]
    v_bad = fuse(exec_wa, verdicts_bad, [])
    assert compute_quadrant(exec_wa, v_bad)[0] == "reasoning_error"


def test_tle_maps_to_complexity():
    exec_tle = ExecutionResult(status="TLE", passed=0, total=1, detail="tl")
    verdicts_ok = [StepVerdict(step_id=f"S{i}", status="correct") for i in range(1, 8)]
    v = fuse(exec_tle, verdicts_ok, [])
    assert v.error_type == ErrorType.E5
    assert v.first_error_step == "S7"
