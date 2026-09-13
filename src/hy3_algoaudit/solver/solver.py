"""解题器：调用Hy3 生成结构化题解"""

from __future__ import annotations

import re

from ..llm import Hy3Client
from .prompt import SYSTEM_PROMPT,build_prompt
from .schema import Solution


# 按 "## S数字 标题" 切分，捕获到下一节标题或文末为止
SECTION_RE = re.compile(
    r"^##\s+(S[1-7])[^\n]*\n(.*?)(?=^##\s+S[1-7]|\Z)",
    re.DOTALL | re.MULTILINE
)

def parse_solution(problem:str,raw:str) -> Solution:
    """把模型原始输出解析为结构化Solution"""
    steps = {m.group(1): m.group(2).strip() for m in SECTION_RE.finditer(raw)}
    return Solution(problem=problem,steps=steps,raw=raw)

def solve(problem:str,client:Hy3Client | None = None,**kwargs)->Solution:
    """生成结构化题解。kwargs透传给chat() （如reasoning_effort"""
    client = client or Hy3Client()
    raw = client.chat(build_prompt(problem),system=SYSTEM_PROMPT,**kwargs)
    return parse_solution(problem,raw)
    