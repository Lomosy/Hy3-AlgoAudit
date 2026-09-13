"""结构化题解的数据模型"""
from __future__ import annotations

import re
from dataclasses import dataclass,field

STEP_KEYS = ("S1","S2","S3","S4","S5","S6","S7")

STEP_TITLES = {
    "S1": "题意与约束理解",
    "S2": "关键观察与性质",
    "S3": "算法设计",
    "S4": "正确性推导",
    "S5": "复杂度分析",
    "S6": "边界条件",
    "S7": "最终实现",
}

CODE_BLOCK_RE = re.compile(r"```(?:python|py|cpp|c\+\+)?\s*\n(.*?)```", re.DOTALL)

def extract_code(text: str ) -> str:
    """从MD文本中提取第一个代码块内容"""
    m = CODE_BLOCK_RE.search(text)
    return m.group(1).strip() if m else ""

@dataclass
class Solution:
    """一道题的结构化题解"""
    problem : str
    steps: dict[str,str] = field(default_factory=dict)
    raw: str = "" # 模型原始输出，解析出问题时用于排查
    
    @property
    def code(self) -> str:
        """从 S7提取可执行代码"""
        return extract_code(self.steps.get("S7",""))
    
    @property
    def complete(self) -> bool:
        """起步是否齐全：过程评估的前置条件"""
        return all(self.steps.get(k,"").strip() for k in STEP_KEYS)

    