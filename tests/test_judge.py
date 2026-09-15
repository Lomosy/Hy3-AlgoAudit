"""Sandbox judge unit tests (pure local backend, no LLM)."""
import pytest

from hy3_algoaudit.config import SandboxLimits
from hy3_algoaudit.sandbox import LocalSandbox
from hy3_algoaudit.sandbox.judge import compare_output, judge_program
from hy3_algoaudit.schemas import CodeBlock, TestCase

LIMITS = SandboxLimits(time_limit_seconds=5, memory_limit_mb=256)


def _judge(code: str, tests, **kwargs):
    return judge_program(LocalSandbox(LIMITS), CodeBlock(language="python", code=code),
                         tests, LIMITS, **kwargs)


def test_compare_output_modes():
    assert compare_output("3\n", "3")
    assert compare_output("1 2  3", "3 2 1") is False
    assert compare_output("1 2 3", "3 2 1") is False
    assert compare_output("0.1000001", "0.1", mode="float")
    assert compare_output("3\n", "3\n\n", mode="exact")
    # default mode is token
    assert compare_output("1 2 3", "1\n2\n3") is True


def test_float_mode_keeps_token_structure():
    """float mode must only relax numeric tokens, never the text structure."""
    # a text-only mismatch cannot be rescued by tolerance
    assert compare_output("YES", "Impossible", mode="float") is False
    # token count must still match
    assert compare_output("1 2", "1 2 3", mode="float") is False
    # non-numeric tokens must match positionally
    assert compare_output("Case 1: 0.3333333", "Case 1: 0.33333333", mode="float") is True
    assert compare_output("Case 1: 0.3", "Case 2: 0.3", mode="float") is False
    # partially numeric tokens are not numbers
    assert compare_output("1,2", "1,3", mode="float") is False


def test_float_mode_uses_codeforces_tolerance():
    """|got - want| <= max(abs_tol, rel_tol * |want|) — the CF convention."""
    # 437D-style: fixed-precision expected vs full-precision printed answer
    assert compare_output("16.666666666666668", "16.6666666667", mode="float")
    # near zero the absolute floor 1e-6 dominates
    assert compare_output("0.0000005", "0.0000001", mode="float")
    assert compare_output("0.5", "0.4", mode="float") is False
    # at magnitude 1e6 the relative term dominates: tolerance is ~1.0
    assert compare_output("1000000.5000000001", "1000000.5", mode="float")
    assert compare_output("1000000.9", "1000000.5", mode="float")  # 0.4 <= 1.0
    assert compare_output("1000002.5", "1000000.5", mode="float") is False  # 2.0 > 1.0


def test_unknown_comparison_mode_rejected():
    with pytest.raises(ValueError):
        compare_output("1", "1", mode="tokens")
    with pytest.raises(ValueError):
        _judge("print(1)", [TestCase(input="", expected="1")], comparison="loose")


def test_per_problem_float_comparison():
    """A float-mode problem verifies AC on the app side."""
    code = "print(16.666666666666668)\n"
    tests = [TestCase(input="", expected="16.6666666667\n")]
    assert _judge(code, tests, comparison="exact").status == "WA"
    assert _judge(code, tests, comparison="float").status == "AC"


def test_abort_on_failure_stops_early():
    tests = [TestCase(input="", expected="1\n"), TestCase(input="", expected="2\n")]
    kept = _judge("print(42)\n", tests, abort_on_failure=True)
    assert kept.status == "WA" and kept.failed_index == 0
    assert len(kept.test_results) == 1  # remaining case never executed
    assert kept.total == 2
    full = _judge("print(42)\n", tests)
    assert len(full.test_results) == 2


def test_ac_case():
    code = "import sys\na,b=map(int,sys.stdin.read().split())\nprint(a+b)\n"
    res = _judge(code, [TestCase(input="1 2\n", expected="3\n"),
                        TestCase(input="-5 5\n", expected="0\n")])
    assert res.status == "AC" and res.passed == 2


def test_wa_case():
    code = "print(42)\n"
    res = _judge(code, [TestCase(input="", expected="1\n")])
    assert res.status == "WA" and res.failed_index == 0


def test_re_case():
    code = "import sys\nx = 1 / 0\n"
    res = _judge(code, [TestCase(input="", expected="")])
    assert res.status == "RE"


def test_tle_case():
    res = judge_program(LocalSandbox(LIMITS),
                        CodeBlock(language="python", code="while True:\n    pass\n"),
                        [TestCase(input="", expected="")],
                        SandboxLimits(time_limit_seconds=1, memory_limit_mb=256))
    assert res.status == "TLE"


def test_ce_case_cpp():
    res = judge_program(LocalSandbox(LIMITS),
                        CodeBlock(language="cpp", code="int main() { return 0 }"),
                        [TestCase(input="", expected="")], LIMITS)
    if res.status == "SE":
        pytest.skip("g++ not available on this machine")
    assert res.status == "CE"
