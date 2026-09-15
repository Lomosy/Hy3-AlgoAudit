"""过程评估器自检：正确题解 vs S3 注入错误。"""
from hy3_algoaudit._legacy.evaluator.evaluator import evaluate
from hy3_algoaudit._legacy.solver.schema import Solution
from hy3_algoaudit._legacy.solver.solver import solve

PROBLEM = """给定一个长度为 n 的整数数组，求其中连续子数组的最大和。
输入：第一行 n（1 <= n <= 100000），第二行 n 个整数（-1000 <= ai <= 1000）。
输出：一个整数，最大子数组和。"""

# 注入到 S3 的错误内容（明显错误的算法选择）
BAD_S3 = (
    "本题可以枚举所有子数组，用双重循环计算每个子数组的和，取最大值即可。"
    "时间复杂度 O(n^2)，对于 n=100000 完全够用，不需要优化。"
)


def main() -> None:
    sol = solve(PROBLEM, reasoning_effort="high")
    print(f"原始题解：{sol}")
    print("=== 1. 评估原始题解 ===")
    r1 = evaluate(sol)
    print(f"过程成立: {r1.process_valid}  首错: {r1.first_error}  类型: {r1.error_type}")
    for v in r1.steps:
        print(f"  {v.step}: {v.status.value}")

    # 复制一份，在 S3 注入错误
    bad = Solution(problem=sol.problem, steps=dict(sol.steps), raw=sol.raw)
    bad.steps["S3"] = BAD_S3
    print("\n=== 2. 评估 S3 注入错误的题解 ===")
    r2 = evaluate(bad)
    print(f"过程成立: {r2.process_valid}  首错: {r2.first_error}  类型: {r2.error_type}")
    for v in r2.steps:
        print(f"  {v.step}: {v.status.value}  {v.reason}")


if __name__ == "__main__":
    main()
