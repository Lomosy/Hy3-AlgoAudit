"""Core domain models.

Two orthogonal label systems, per the tech spec:
- ExecStatus: result-track verdicts (AC/WA/TLE/MLE/RE/CE) from the sandbox.
- ErrorType E1-E8: process-track root-cause taxonomy from the evaluator.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------- steps

STEP_ORDER = ["S1", "S2", "S3", "S4", "S5", "S6", "S7"]

STEP_NAMES_ZH = {
    "S1": "题意与约束理解",
    "S2": "关键观察与性质",
    "S3": "算法设计",
    "S4": "正确性推导",
    "S5": "复杂度分析",
    "S6": "边界条件",
    "S7": "最终实现",
}


class StepId(str, Enum):
    S1 = "S1"
    S2 = "S2"
    S3 = "S3"
    S4 = "S4"
    S5 = "S5"
    S6 = "S6"
    S7 = "S7"

    @property
    def index(self) -> int:
        return STEP_ORDER.index(self.value)


def step_rank(step_id: str | None) -> int:
    """Position of a step in S1..S7 order; unknown ids sort last."""
    if step_id and step_id in STEP_ORDER:
        return STEP_ORDER.index(step_id)
    return len(STEP_ORDER)


# ---------------------------------------------------------------- result track

class ExecStatus(str, Enum):
    AC = "AC"
    WA = "WA"
    TLE = "TLE"
    MLE = "MLE"
    RE = "RE"
    CE = "CE"
    SE = "SE"  # system/sandbox error — never the candidate's fault

    @property
    def is_accepted(self) -> bool:
        return self == ExecStatus.AC


class TestCase(BaseModel):
    input: str
    expected: str


class TestResult(BaseModel):
    index: int
    status: ExecStatus
    time_ms: float = 0.0
    detail: str = ""


class ExecutionResult(BaseModel):
    status: ExecStatus
    passed: int = 0
    total: int = 0
    failed_index: Optional[int] = None
    detail: str = ""
    compile_ok: bool = True
    test_results: list[TestResult] = Field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return self.status.is_accepted


# ---------------------------------------------------------------- process track

class ErrorType(str, Enum):
    E1 = "E1"  # 题意理解错误
    E2 = "E2"  # 算法选择错误
    E3 = "E3"  # 推导/逻辑错误
    E4 = "E4"  # 条件或边界遗漏
    E5 = "E5"  # 复杂度错误
    E6 = "E6"  # 题解—代码不一致
    E7 = "E7"  # 实现错误
    E8 = "E8"  # 无依据推断/幻觉
    NONE = "NONE"

    @property
    def label(self) -> str:
        return {
            "E1": "题意理解错误",
            "E2": "算法选择错误",
            "E3": "推导/逻辑错误",
            "E4": "条件或边界遗漏",
            "E5": "复杂度错误",
            "E6": "题解—代码不一致",
            "E7": "实现错误",
            "E8": "无依据推断/幻觉",
            "NONE": "无错误",
        }[self.value]


# Steps are 4-state for quick scan; fine verification collapses to correct/incorrect.
STEP_STATUSES = {"correct", "incorrect", "suspicious", "downstream", "unverifiable"}


class StepContent(BaseModel):
    step_id: str
    title: str = ""
    content: str = ""

    @field_validator("step_id")
    @classmethod
    def _valid_step(cls, v: str) -> str:
        if v not in STEP_ORDER:
            raise ValueError(f"step_id must be one of {STEP_ORDER}, got {v!r}")
        return v


class CodeBlock(BaseModel):
    language: str = "python"
    code: str = ""

    @field_validator("language")
    @classmethod
    def _norm_lang(cls, v: str) -> str:
        v = v.strip().lower()
        alias = {"c++": "cpp", "py": "python", "py3": "python", "python3": "python"}
        return alias.get(v, v)


class StructuredSolution(BaseModel):
    """S1-S7 structured solution. The final implementation lives in `code`
    (step S7 carries implementation notes)."""

    problem_id: Optional[str] = None
    steps: list[StepContent]
    code: CodeBlock

    def step(self, step_id: str) -> StepContent | None:
        for s in self.steps:
            if s.step_id == step_id:
                return s
        return None

    def step_text(self, step_id: str) -> str:
        s = self.step(step_id)
        if not s:
            return ""
        head = f"{s.step_id} {s.title}\n" if s.title else f"{s.step_id}\n"
        return head + s.content

    def reasoning_steps(self) -> list[StepContent]:
        """S1-S6 (semantic reasoning), excluding the implementation step S7."""
        return [s for s in self.steps if s.step_id != "S7"]

    def has_all_steps(self, ids: list[str]) -> bool:
        present = {s.step_id for s in self.steps}
        return all(i in present for i in ids)


class EvidenceKind(str, Enum):
    rule = "rule"                        # 第一层：规则与结构检查
    sandbox = "sandbox"                  # 第二层：沙箱执行验证
    semantic = "semantic"                # 第三层：分步骤语义审查
    consistency = "consistency"          # 题解—代码一致性检查
    counterexample = "counterexample"    # 反例验证
    reference = "reference"              # 参考解法事实


# Evidence priority per spec section 13 (higher = stronger).
EVIDENCE_PRIORITY: dict[EvidenceKind, int] = {
    EvidenceKind.counterexample: 5,
    EvidenceKind.rule: 4,
    EvidenceKind.sandbox: 4,
    EvidenceKind.reference: 3,
    EvidenceKind.consistency: 3,
    EvidenceKind.semantic: 2,
}


class Evidence(BaseModel):
    kind: EvidenceKind
    strength: int = Field(default=3, ge=1, le=5)
    step_id: Optional[str] = None
    error_type: ErrorType = ErrorType.NONE
    indicates_invalid: bool = False  # True => this evidence says the process is broken
    claim: str = ""                  # suspicious claim (counterexample target)
    detail: str = ""


class StepVerdict(BaseModel):
    step_id: str
    status: str = "correct"  # correct | incorrect | suspicious | downstream | unverifiable
    confidence: float = 0.5
    error_type: ErrorType = ErrorType.NONE
    suspicious_claim: str = ""
    reasoning: str = ""

    @field_validator("status")
    @classmethod
    def _valid_status(cls, v: str) -> str:
        if v not in STEP_STATUSES:
            raise ValueError(f"unknown step status {v!r}")
        return v


class ProcessVerdict(BaseModel):
    process_valid: bool
    first_error_step: Optional[str] = None
    error_type: ErrorType = ErrorType.NONE
    confidence: float = 0.5
    step_verdicts: list[StepVerdict] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    summary: str = ""


# ---------------------------------------------------------------- final report

# Outcome x Process quadrant (spec section 3.2)
QUADRANTS = {
    "full_correct": "完整正确解（结果正确 × 过程正确）",
    "ac_but_invalid": "结果正确但过程不成立（AC-but-Invalid）",
    "implementation_error": "主要为实现层错误（过程成立 × 结果错误）",
    "reasoning_error": "推理与实现均存在错误",
}


class AuditReport(BaseModel):
    problem_id: str = ""
    mode: str = "solve"  # solve | evaluate
    created_at: str = ""
    execution: Optional[ExecutionResult] = None
    process: ProcessVerdict
    quadrant: str = ""
    quadrant_key: str = ""
    final_solution: Optional[StructuredSolution] = None
    repair_rounds: int = 0
    tokens_used: int = 0
    notes: list[str] = Field(default_factory=list)
