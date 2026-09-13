"""判题器自检：四道题覆盖AC/WA/TLE/RE四种状态"""

from hy3_algoaudit.judge.judge import judge_python
from hy3_algoaudit.judge.result import TestCase,Verdict


def main() ->None:
    cases = [
        TestCase(input_data="1 2\n",expected="3\n"),
        TestCase(input_data="5 5\n",expected="10\n"),
        TestCase(input_data="1 2\n",expected="-1\n"),
    ]
    
    # AC: ；两数求和
    ac_code = "a,b = map(int,input().split())\nprint(a+b)"
    
    # WA: 多打印一行
    wa_code = "a,b = map(int,input().split())\nprint(a+b)\nprint('Done')"
    
    # TLE:死循环
    tle_code = "while True:\n   pass"
    
    # RE:除0
    re_code = "a,b = map(int,input().split())\nprint(a//(a-b))"
    
    for name,code in [("AC", ac_code), ("WA", wa_code), ("TLE", tle_code), ("RE", re_code)]:
        result = judge_python(code,cases,timeout=2.0)
        print(f"{name}: {result.verdict}  (passed {result.passed}/{result.total}) Error:{result.results}")

        
    
if __name__ == "__main__":
    main()