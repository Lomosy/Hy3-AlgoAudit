"""Judge: output comparison + full testcase execution via a Sandbox backend."""
from __future__ import annotations

from ..config import SandboxLimits
from ..schemas import CodeBlock, ExecutionResult, TestCase
from ..utils import SandboxError
from .base import Sandbox

# ---------------------------------------------------------------- comparison

#: Comparison modes understood by :func:`compare_output`.
#:
#: ``exact`` - line-by-line after stripping trailing whitespace (strictest)
#: ``token`` - whitespace-separated token equality (Codeforces style, default)
#: ``float`` - token equality except numeric tokens, which only need to agree
#:             within ``float_abs_tol``
COMPARISON_MODES: tuple[str, ...] = ("exact", "token", "float")
DEFAULT_COMPARISON = "token"


def _norm_lines(text: str) -> list[str]:
    return [ln.rstrip() for ln in text.replace("\r\n", "\n").strip().split("\n")]


def _as_float(token: str) -> float | None:
    """Strictly parse a whole token as a number, else None.

    Used by ``float`` mode: a token must be a complete number to be compared
    with tolerance. Partial matches (``1,2`` / ``abc3``) are deliberately
    rejected so that structure differences are never silently tolerated.
    """
    try:
        return float(token)
    except (TypeError, ValueError):
        return None


def compare_output(actual: str, expected: str, mode: str = DEFAULT_COMPARISON,
                   float_abs_tol: float = 1e-6,
                   float_rel_tol: float = 1e-6) -> bool:
    """Compare program output with expected output.

    modes:
      exact - line-by-line after stripping trailing whitespace
      token - whitespace-separated token comparison (default, Codeforces style)
      float - same token structure as ``token``, but numeric tokens are
              accepted when ``|got - want| <= max(abs_tol, rel_tol * |want|)``

    The float criterion is the Codeforces/special-judge convention
    (absolute error 1e-6 OR relative error 1e-6, whichever is looser), so a
    solution printing ``16.666666666666668`` for an expected ``16.6666666667``
    is accepted while a genuinely different value is not.

    ``float`` deliberately keeps the token structure (count and order of
    non-numeric tokens): comparing extracted numbers alone would let ``YES``
    match ``Impossible`` (both contain no numbers).
    """
    if mode == "exact":
        return _norm_lines(actual) == _norm_lines(expected)
    if mode == "float":
        got = actual.split()
        want = expected.split()
        if len(got) != len(want):
            return False
        for x, y in zip(got, want):
            if x == y:
                continue
            fx, fy = _as_float(x), _as_float(y)
            if fx is None or fy is None:
                return False
            tol = max(float_abs_tol, float_rel_tol * abs(fy))
            if abs(fx - fy) > tol:
                return False
        return True
    if mode != "token":
        raise ValueError(
            f"unknown comparison mode {mode!r}; expected one of {COMPARISON_MODES}"
        )
    return actual.split() == expected.split()


# ---------------------------------------------------------------- full judge


def judge_program(
    sandbox: Sandbox,
    code: CodeBlock,
    tests: list[TestCase],
    limits: SandboxLimits,
    *,
    comparison: str = DEFAULT_COMPARISON,
    float_abs_tol: float = 1e-6,
    float_rel_tol: float = 1e-6,
    abort_on_failure: bool = False,
) -> ExecutionResult:
    """Compile once -> run testcases sequentially -> destroy environment.

    comparison
        Per-problem output comparison mode: ``exact`` | ``token`` | ``float``.
        The dataset stores one mode per problem (the one its reference solution
        was verified with), so callers pass it straight through — the app-side
        judge then uses exactly the same criterion as the offline verification.
    abort_on_failure
        Stop after the first non-AC testcase instead of running the remaining
        ones. The verdict is unchanged, but on error-injected candidates
        (which often TLE on cheap brute force, 2-4s each) this cuts wall-clock
        time by an order of magnitude. Remaining testcases are simply not
        executed — they are absent from ``test_results``.
    """
    if comparison not in COMPARISON_MODES:
        raise ValueError(
            f"unknown comparison mode {comparison!r}; expected one of {COMPARISON_MODES}"
        )

    total = len(tests)
    if total == 0:
        return ExecutionResult(status="SE", detail="no testcases provided", total=0)

    try:
        program = sandbox.prepare(code)
    except SandboxError as e:
        # sandbox environment failure (e.g. missing compiler) — never blame
        # the candidate program
        return ExecutionResult(status="SE", total=total, detail=str(e)[:2000])

    try:
        if not program.compile_ok:
            return ExecutionResult(
                status="CE",
                compile_ok=False,
                total=total,
                detail=program.compile_error[:4000],
            )

        passed = 0
        final_status = "AC"
        failed_index = None
        detail = ""
        test_results = []

        for i, tc in enumerate(tests):
            try:
                outcome = sandbox.run_once(program, tc.input,
                                           limits.time_limit_seconds,
                                           limits.memory_limit_mb)
            except SandboxError as e:
                return ExecutionResult(status="SE", total=total,
                                       detail=f"sandbox failure: {e}")

            if outcome.timed_out:
                status = "TLE"
            elif outcome.mem_exceeded:
                status = "MLE"
            elif outcome.runtime_error:
                status = "RE"
            else:
                status = "AC" if compare_output(
                    outcome.stdout, tc.expected, comparison,
                    float_abs_tol, float_rel_tol,
                ) else "WA"

            entry_detail = ""
            if status == "TLE":
                entry_detail = f"time limit ({limits.time_limit_seconds}s) exceeded"
            elif status == "MLE":
                entry_detail = f"memory limit ({limits.memory_limit_mb}MB) exceeded"
            elif status == "RE":
                entry_detail = (outcome.stderr or f"exit code {outcome.exit_code}")[:2000]
            elif status == "WA":
                entry_detail = f"output mismatch ({comparison} mode)"

            if status != "AC" and failed_index is None:
                failed_index = i
                final_status = status
                detail = entry_detail
            if status == "AC":
                passed += 1
            test_results.append({
                "index": i, "status": status,
                "time_ms": round(outcome.time_ms, 1),
                "detail": entry_detail,
            })
            if abort_on_failure and status != "AC":
                break

        return ExecutionResult(
            status=final_status,
            passed=passed,
            total=total,
            failed_index=failed_index,
            detail=detail,
            compile_ok=True,
            test_results=test_results,
        )
    finally:
        sandbox.cleanup(program)
