"""过程评估结果的数据结构"""
from __future__ import annotations

from dataclasses import dataclass,field
from enum import Enum

class StepStatus(str,Enum):
    OK  = "OK"
    ERROR = "ERROR"
    SUSPICIOUS = "SUSPICIOUS"
    SKIPPED = "SKIPPED"
    
class ErrorType(str,Enum):
    """过错根因分类"""
    E1 = "E1"  # 题意理解错误
    E2 = "E2"  # 算法选择错误
    E3 = "E3"  # 推导或逻辑错误
    E4 = "E4"  # 条件或边界遗漏
    E5 = "E5"  # 复杂度错误
    E6 = "E6"  # 题解与代码不一致
    E7 = "E7"  # 实现错误
    E8 = "E8"  # 无依据推断或幻觉
    NONE = "NONE"
    
@dataclass
class StepVerdict:
    """单步判定结果。"""
    step: str
    status: StepStatus
    error_type: ErrorType = ErrorType.NONE
    reason: str = ""

@dataclass
class EvaluationResult:
    """一次过程评估的完整结果。"""
    process_valid: bool
    steps: list[StepVerdict] = field(default_factory=list)
    first_error: str | None = None      # 首个错误步骤，如 "S3"
    error_type: ErrorType = ErrorType.NONE
    notes: str = ""