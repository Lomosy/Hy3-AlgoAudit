"""过程评估器：结构检查 → 逐步骤审查 → 首错定位。"""
from __future__ import annotations

import re

from ..llm import Hy3Client
from ..solver.schema import STEP_KEYS, STEP_TITLES, Solution
from .prompt import SYSTEM_PROMPT, build_step_prompt
from .schema import ErrorType, EvaluationResult, StepStatus, StepVerdict

STATUS_RE = re.compile(r"STATUS:\s*(\w+)")
TYPE_RE = re.compile(r"ERROR_TYPE:\s*(\w+)")
REASON_RE = re.compile(r"REASON:\s*(.+)")


def _parse(step: str, raw: str) -> StepVerdict:
    """解析模型输出，任何异常都退化为 SUSPICIOUS（宁可存疑，不误判）。"""
    def find(pattern, default=""):
        m = pattern.search(raw)
        return m.group(1).strip() if m else default

    status_raw = find(STATUS_RE, "SUSPICIOUS").upper()
    status = (
        StepStatus(status_raw)
        if status_raw in StepStatus.__members__
        else StepStatus.SUSPICIOUS
    )

    type_raw = find(TYPE_RE, "NONE").upper()
    error_type = (
        ErrorType(type_raw)
        if type_raw in ErrorType.__members__
        else ErrorType.NONE
    )
    return StepVerdict(step, status, error_type, find(REASON_RE))


def evaluate(
    solution: Solution,
    client: Hy3Client | None = None,
    *,
    reasoning_effort: str = "high",
    temperature: float = 0.1,
) -> EvaluationResult:
    """评估一份结构化题解的过程正确性。"""
    client = client or Hy3Client()

    # 1) 结构检查：规则层，不调用模型
    missing = [k for k in STEP_KEYS if not solution.steps.get(k, "").strip()]
    if missing:
        return EvaluationResult(
            process_valid=False,
            notes=f"结构不完整，缺失步骤：{', '.join(missing)}",
        )

    # 2) 逐步骤审查
    verdicts: list[StepVerdict] = []
    prior: list[str] = []
    for key in STEP_KEYS:
        content = solution.steps[key]
        raw = client.chat(
            build_step_prompt(
                solution.problem,
                "\n\n".join(prior),
                f"{key} {STEP_TITLES[key]}",
                content,
            ),
            system=SYSTEM_PROMPT,
            reasoning_effort=reasoning_effort,
            temperature=temperature,
        )
        verdicts.append(_parse(key, raw))
        prior.append(f"## {key} {STEP_TITLES[key]}\n{content}")

    # 3) 首错定位：按顺序取第一个实质错误
    first = next((v for v in verdicts if v.status == StepStatus.ERROR), None)
    return EvaluationResult(
        process_valid=first is None,
        steps=verdicts,
        first_error=first.step if first else None,
        error_type=first.error_type if first else ErrorType.NONE,
    )
