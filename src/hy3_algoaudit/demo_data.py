"""Mock LLM routes for the offline demo (`hy3-audit demo`).

The mock solver produces a correct Kadane solution, so the demo shows the
happy path for scenario 1; scenario 2 evaluates the bundled AC-but-Invalid
variant, exercising the consistency checker against a scripted response.
"""
from __future__ import annotations

import json

KADANE_CODE = """import sys

def main():
    data = sys.stdin.read().split()
    n = int(data[0])
    a = list(map(int, data[1:1 + n]))
    best = a[0]
    cur = a[0]
    for x in a[1:]:
        cur = max(x, cur + x)
        best = max(best, cur)
    print(best)

main()
"""

_SOLVER_JSON = {
    "problem_id": None,
    "steps": [
        {"step_id": "S1", "title": "题意与约束理解",
         "content": "给定长度 n 的整数数组（元素可为负），求连续子数组的最大和，"
                    "子数组不能为空。n 最大 1e5，|a_i| <= 1e9，答案可能达 1e14，"
                    "需使用 64 位整数。"},
        {"step_id": "S2", "title": "关键观察与性质",
         "content": "最大子数组问题具有最优子结构：以位置 i 结尾的最大子数组和 "
                    "f(i) 只依赖 f(i-1)。若 f(i-1) > 0，则将其接上更优；否则从 "
                    "a[i] 重新开始。这是 Kadane 算法的核心不变量。"},
        {"step_id": "S3", "title": "算法设计",
         "content": "采用 Kadane 动态规划：单次线性扫描，维护 cur（以当前元素结尾的"
                    "最大子数组和）与 best（全局最大值）。无需额外数据结构。"},
        {"step_id": "S4", "title": "正确性推导",
         "content": "归纳证明：f(1)=a[1]。对 i>1，任何以 i 结尾的子数组要么只含 "
                    "a[i]，要么是以 i-1 结尾的某个子数组加上 a[i]，故 "
                    "f(i)=max(a[i], f(i-1)+a[i])。转移覆盖所有情形，归纳成立。"},
        {"step_id": "S5", "title": "复杂度分析",
         "content": "时间复杂度 O(n)，空间复杂度 O(1)（数组本身 O(n) 读入）。"
                    "n <= 1e5 时完全满足限制。"},
        {"step_id": "S6", "title": "边界条件",
         "content": "全负数组：cur 初始化为 a[0] 而非 0，保证答案为最大单元素，"
                    "不会错误地输出空子数组和 0。n=1 时直接输出 a[0]。"
                    "使用 Python 整数，无溢出问题。"},
        {"step_id": "S7", "title": "最终实现",
         "content": "从 stdin 一次读入全部数据，线性扫描输出答案。"},
    ],
    "code": {"language": "python", "code": KADANE_CODE},
}


def build_mock_routes() -> list[tuple[str, object]]:
    def solver(_user: str) -> str:
        return json.dumps(_SOLVER_JSON, ensure_ascii=False)

    def step_scan(_user: str) -> str:
        steps = [{"step_id": f"S{i}", "status": "correct", "confidence": 0.9,
                  "error_type": "NONE", "suspicious_claim": "",
                  "reasoning": "mock: 推理成立"} for i in range(1, 8)]
        return json.dumps({"steps": steps}, ensure_ascii=False)

    def fine_verify(_user: str) -> str:
        return json.dumps({"step_id": "S1", "status": "correct",
                           "confidence": 0.95, "error_type": "NONE",
                           "suspicious_claim": "", "reasoning": "mock: 精验通过"},
                          ensure_ascii=False)

    def consistency(_user: str) -> str:
        return json.dumps({"consistent": True, "mismatches": [],
                           "confidence": 0.9}, ensure_ascii=False)

    def counterexample(_user: str) -> str:
        return json.dumps({"claim_holds": True,
                           "explanation": "mock: 命题成立，无需反例"},
                          ensure_ascii=False)

    return [
        ("请解决以下算法竞赛题目", solver),
        ("首个错误步骤", solver),       # process-guided repair prompt
        ("执行反馈", solver),           # execution-only repair prompt
        ("逐步审查", step_scan),
        ("精细验证", fine_verify),
        ("题解与代码的一致性", consistency),
        ("最小反例", counterexample),
    ]
