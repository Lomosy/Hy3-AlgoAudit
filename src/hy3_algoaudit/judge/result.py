"""判定结果的数据结构"""

from __future__ import annotations

from dataclasses import dataclass,field
from enum import Enum

# 枚举类，实例只有6种
class Verdict(str,Enum):
    AC = "AC" # Accepted
    WA = "WA"   # Wrong Answer
    TLE = "TLE" # Time Limit Exceeded
    RE = "RE"   # Runtime Error
    CE = "CE"   # Compilation Error（C++ 扩展时才用到）
    ERR = "ERR" # 判题系统自身故障

@dataclass
class TestCase:
    """测试用例：标准输入+期望输出"""
    input_data : str
    expected   : str
    
@dataclass
class CaseResult:
    """单个用例的判定结果"""
    
    verdict: Verdict
    time_ms: float = 0.0
    stdout : str = ""
    stderr : str = ""
    

@dataclass
class JudgeResult:
    """一次提交的完整判定结果"""
    verdict : Verdict
    results : list[CaseResult] = field(default_factory=list)
    
    # property：函数能够像属性一样调用，使之能够实时计算，对用户透明
    # 该函数本身的变化不会影响外部调用，外部调用稳定，内部实现隐藏且易改
    # 封装成属性
    @property
    def total(self)->int :
        return len(self.results)

    @property
    def passed(self)->int:
        return sum(1 for r in self.results if r.verdict == Verdict.AC)
    
