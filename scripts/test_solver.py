"""解题器自检：结构化输出+解析+判题闭环"""
from hy3_algoaudit.judge.judge import judge_python
from hy3_algoaudit.judge.result import TestCase
from hy3_algoaudit.solver.solver import solve


PROBLEM = """输入两个整数 a 和 b，输出它们的和。
输入：一行，两个整数 a、b（0 <= a, b <= 1000）。
输出：一个整数，表示 a + b。"""

def main() :
    sol = solve(PROBLEM,reasoning_effort = "high")
    
    for key in ("S1","S3","S5"):
        print(f"---{key}---")
        print(sol.steps.get(key,"(缺失)")[:200])
        
    print("--- 提取到的代码 ---")
    print(sol.code or "(未提取到代码块)")
    print("七步齐全：",sol.complete)
    
    # 用第二课的判题器验证生成的代码
    
    result = judge_python(
        sol.code,
        [TestCase("1 2\n","3\n"),TestCase("5 7\n","11\n")],
    )
    
    print(f"判题结果：{result.verdict} ({result.passed}/{result.total})")
    
if __name__ == "__main__":
    main()
