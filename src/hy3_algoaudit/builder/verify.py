"""参考解实测验证关卡：评测集可信度的核心。

一道题只有在「数据集提供的某条 Python 3 正确解真实跑过全部用例并全部 AC」之后，
才有资格进入 ProcessEval-CP。这是本题集所有下游指标（误报率、首错定位准确率、
AC-but-Invalid 识别率）的地基 —— 参考解不可信，指标就没有意义。

验证策略：
    1. 先用静态规则剔除明显的 Python 2 代码（数据集里 language=1 的解法是 Python 2，
       raw_input / xrange / print 语句在 Python 3 下必然失败），避免无谓的沙箱开销；
    2. 分两阶段执行：先跑少量 public 用例快速失败，再跑完整用例集；
    3. 同题按顺序尝试多条 Py3 解，任一条全 AC 即通过；
    4. 全程记录逐用例证据与 sha256，写入 reference/{pid}.meta.json。

**执行与比对统一走应用侧 sandbox**（而不是自带一套 run_python）：
离线验证用的判题口径必须与 hy3-audit 评测时的口径**逐位一致**，否则同一份
参考解会在数据集里是 AC、在应用里是 WA，评测集的标签立刻失真。因此这里
复用 `sandbox.LocalSandbox`（同一套 `python -I` 隔离与超时/内存限制）和
`sandbox.judge.compare_output`，并把实际使用的 comparison 模式落盘到
题目的 meta 里，供应用侧按题取出使用。
"""

from __future__ import annotations

import hashlib
import random
import re
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any

from ..config import SandboxLimits
from ..sandbox import LocalSandbox
from ..sandbox.judge import DEFAULT_COMPARISON, compare_output
from ..schemas import CodeBlock, TestCase
from ..utils import SandboxError
from . import proto

#: 比对模式（与 sandbox.judge.COMPARISON_MODES 对齐）
#:   token —— 空白分词后逐 token 相等（Codeforces 风格，默认，最严格）
#:   float —— token 结构必须一致，数值 token 允许 1e-6 绝对/相对误差
COMPARISON_FLOAT = "float"
FLOAT_TOLERANCE = 1e-6

#: 旧版构建缓存里的取值 → 沙箱口径。
#:
#: 历史包袱：早期 verify.py 自带一套 judge.judge，用的是
#: `exact`（= 归一化后分词比对，与新的 token **语义完全相同**）和
#: `float_tolerance`。interim/verified.jsonl 里可能还留着这两个值，
#: 于是需要一次显式归一 —— 新代码永远不会产出 `exact`，因此这个映射
#: 不会与「新的 exact 模式（逐行比对）」冲突。
LEGACY_COMPARISON_ALIASES = {
    "exact": DEFAULT_COMPARISON,
    "float_tolerance": COMPARISON_FLOAT,
}


def normalize_comparison(value: str | None) -> str:
    """把历史缓存里的比对模式归一到 sandbox 口径；空值取默认模式。"""
    mode = (value or "").strip()
    if mode in LEGACY_COMPARISON_ALIASES:
        return LEGACY_COMPARISON_ALIASES[mode]
    return mode or DEFAULT_COMPARISON

# 强信号的 Python 2 语法特征
PY2_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("raw_input", re.compile(r"\braw_input\s*\(")),
    ("xrange", re.compile(r"\bxrange\s*\(")),
    ("print_statement", re.compile(r"^\s*print\s+[^(=\s]", re.M)),
    ("print_redirect", re.compile(r"\bprint\s*>>")),
    ("iteritems", re.compile(r"\.\s*iter(items|keys|values)\s*\(")),
    ("has_key", re.compile(r"\.\s*has_key\s*\(")),
    ("basestring", re.compile(r"\bbasestring\b")),
    ("unicode", re.compile(r"\bunicode\s*\(")),
    ("long_literal", re.compile(r"\blong\s*\(")),
    ("old_except", re.compile(r"except\s+[\w.]+\s*,\s*\w+\s*:")),
    ("old_raise", re.compile(r"\braise\s+\w+\s*,\s*")),
    ("backtick_repr", re.compile(r"`[^`\n]+`")),
)


def detect_python2(code: str) -> list[str]:
    """返回命中的 Python 2 特征列表；为空说明代码看起来是 Python 3"""
    return [name for name, pattern in PY2_PATTERNS if pattern.search(code)]


@dataclass
class Verification:
    """一次参考解验证的完整证据"""

    ok: bool
    verdict: str
    language: int
    language_name: str
    tried_index: int
    cases_total: int
    cases_passed: int
    max_ms: float
    total_ms: float
    code_sha256: str
    cases_sha256: str
    comparison: str = DEFAULT_COMPARISON
    reject_reason: str = ""
    py2_features: list[str] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def needs_float_tolerance(result: Verification) -> bool:
    """判断一次失败是否「值得用浮点容差重试」。

    只在失败原因全是 WA（答案不一致）时才重试：若是 TLE / RE / PY2，
    说明问题不在数值表示上，重试纯属浪费。
    另外要求失败样本里**至少有一个**双方都能解析为数字的 token 对，
    否则就是普通的答案错，容差也救不回来。
    """
    if result.ok or result.verdict not in ("WA",):
        return False
    if not result.failures:
        return False
    for failure in result.failures:
        if failure.get("verdict") != "WA":
            return False
        got = (failure.get("stdout_head") or "").split()
        want = (failure.get("expected_head") or "").split()
        if len(got) != len(want):
            return False
    return True


# ---------------------------------------------------------------- 用例选择
def _fits(case: dict[str, str], limit: int) -> bool:
    return len(case["input"].encode("utf-8")) <= limit and len(case["output"].encode("utf-8")) <= limit


def select_cases_with_origins(
    problem: dict[str, Any], cfg: dict[str, Any], key: str
) -> tuple[list[TestCase], list[str], str]:
    """挑选验证用例，并同时返回每用例的来源标签。

    返回 (用例列表, 来源标签列表, 用例集合 sha256)。
    **用例集合参与哈希，任何裁剪参数变化都会改变哈希**，从而让 manifest
    能追溯到「验证的到底是哪一套用例」。

    顺序规则（落盘阶段依赖它保持「公开用例 = 已验证用例的前缀」）：
        全部 public 用例在前，随后是确定性打乱的 private / generated 用例。
    """
    limits = cfg["output_limits"]
    field_limit = int(limits["max_case_field_bytes"])
    verify_limit = int(cfg["verification"]["max_cases"])

    picked: list[tuple[str, dict[str, str]]] = []
    for case in problem["public_tests"]:
        if _fits(case, field_limit):
            picked.append(("public", case))

    rest: list[tuple[str, dict[str, str]]] = []
    for origin in ("private", "generated"):
        for case in problem[f"{origin}_tests"]:
            if _fits(case, field_limit):
                rest.append((origin, case))
    rng = random.Random(key)
    rng.shuffle(rest)

    ordered = list(picked)
    seen: set[tuple[str, str]] = {case["input"] + "\x00" + case["output"] for _, case in picked}
    for origin, case in rest:
        if len(ordered) >= verify_limit:
            break
        fingerprint = case["input"] + "\x00" + case["output"]
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        ordered.append((origin, case))

    ordered = ordered[:verify_limit]
    cases = [TestCase(input=case["input"], expected=case["output"]) for _, case in ordered]
    origins = [origin for origin, _ in ordered]

    digest = hashlib.sha256()
    for case in cases:
        digest.update(case.input.encode("utf-8"))
        digest.update(b"\x1e")
        digest.update(case.expected.encode("utf-8"))
        digest.update(b"\x1d")
    return cases, origins, digest.hexdigest()


def select_cases(problem: dict[str, Any], cfg: dict[str, Any], key: str) -> tuple[list[TestCase], str]:
    """兼容包装：只要 (用例列表, 哈希)"""
    cases, _origins, digest = select_cases_with_origins(problem, cfg, key)
    return cases, digest


# ---------------------------------------------------------------- 单条解验证
def _case_failure(index: int, verdict: str, stdout: str, stderr: str, expected: str) -> dict[str, Any]:
    """失败证据：保留 stdout/expected 头部。

    这两段不是给人看的装饰，而是下游两个判定函数的输入：
    `needs_float_tolerance`（是否值得用浮点容差重试）和
    `suspected_special_judge`（是否疑似答案不唯一）。丢掉它们，两阶段比对
    和漏网多解题的审计线索就都没了。
    """
    return {
        "case_index": index,
        "verdict": verdict,
        "stderr": (stderr or "")[:400],
        "stdout_head": (stdout or "")[:120],
        "expected_head": expected[:120],
    }


def _run_cases(
    code: str,
    cases: list[TestCase],
    timeout: float,
    *,
    comparison: str = DEFAULT_COMPARISON,
    tolerance: float = FLOAT_TOLERANCE,
    memory_limit_mb: int = 512,
    abort_on_failure: bool = False,
) -> tuple[int, float, list[dict[str, Any]]]:
    """跑一批用例，返回 (通过数, 最大耗时毫秒, 失败详情)。

    abort_on_failure=True 时遇到第一个非 AC 用例立即停止。这一点对构建耗时
    影响极大：错误解常常是「未优化的朴素写法」，在 Python 下逐条 TLE，
    而 CF 题时限可达 2~4 秒 —— 若把剩余用例全部跑完，单条错误解就能烧掉
    几十秒到几分钟。既然判定目标是「这条解是否全 AC」，一旦出现非 AC
    就没有继续的必要，只需留下失败证据即可。

    执行走 `sandbox.LocalSandbox`：`prepare()` 编译/落盘一次，`run_once()`
    反复喂 stdin，避免每条用例重新写文件、重启解释器。
    """
    sandbox = LocalSandbox(SandboxLimits(
        time_limit_seconds=timeout,
        memory_limit_mb=memory_limit_mb,
    ))
    try:
        program = sandbox.prepare(CodeBlock(language="python", code=code))
    except SandboxError as exc:  # 沙箱/解释器故障，不是代码的错
        return 0, 0.0, [{"case_index": 0, "verdict": "SE", "stderr": str(exc)[:400]}]

    passed = 0
    max_ms = 0.0
    failures: list[dict[str, Any]] = []
    try:
        for index, case in enumerate(cases):
            try:
                outcome = sandbox.run_once(program, case.input, timeout, memory_limit_mb)
            except SandboxError as exc:
                failures.append(
                    {"case_index": index, "verdict": "SE", "stderr": str(exc)[:400]}
                )
                break

            max_ms = max(max_ms, outcome.time_ms)
            if outcome.timed_out:
                verdict = "TLE"
            elif outcome.mem_exceeded:
                verdict = "MLE"
            elif outcome.exit_code not in (0, None):
                verdict = "RE"
            elif compare_output(outcome.stdout, case.expected, comparison,
                                tolerance, tolerance):
                verdict = "AC"
            else:
                verdict = "WA"

            if verdict == "AC":
                passed += 1
                continue
            if len(failures) < 5:
                failures.append(_case_failure(
                    index, verdict, outcome.stdout, outcome.stderr, case.expected))
            if abort_on_failure:
                break
    finally:
        sandbox.cleanup(program)
    return passed, max_ms, failures


def verify_solution(
    code: str,
    cases: list[TestCase],
    *,
    timeout: float,
    trial: int = 0,
    language: int = proto.LANGUAGE_PYTHON3,
    cases_sha256: str = "",
    comparison: str = DEFAULT_COMPARISON,
    tolerance: float = FLOAT_TOLERANCE,
    memory_limit_mb: int = 512,
) -> Verification:
    """验证单条参考解：先少量用例快速失败，再跑完整用例集"""
    digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
    features = detect_python2(code)
    if features:
        return Verification(
            ok=False,
            verdict="PY2",
            language=language,
            language_name=proto.LANGUAGE_NAMES.get(language, "?"),
            tried_index=trial,
            cases_total=len(cases),
            cases_passed=0,
            max_ms=0.0,
            total_ms=0.0,
            code_sha256=digest,
            cases_sha256=cases_sha256,
            comparison=comparison,
            reject_reason="python2_syntax",
            py2_features=features,
        )

    if not cases:
        return Verification(
            ok=False, verdict="NO_CASES", language=language,
            language_name=proto.LANGUAGE_NAMES.get(language, "?"), tried_index=trial,
            cases_total=0, cases_passed=0, max_ms=0.0, total_ms=0.0,
            code_sha256=digest, cases_sha256=cases_sha256, comparison=comparison,
            reject_reason="no_cases",
        )

    # 阶段一：public 用例快速失败
    head = cases[: min(3, len(cases))]
    passed_head, max_ms, failures = _run_cases(
        code, head, timeout, comparison=comparison, tolerance=tolerance,
        memory_limit_mb=memory_limit_mb,
    )
    if passed_head != len(head):
        return Verification(
            ok=False, verdict=failures[0]["verdict"] if failures else "WA",
            language=language, language_name=proto.LANGUAGE_NAMES.get(language, "?"),
            tried_index=trial, cases_total=len(cases), cases_passed=passed_head,
            max_ms=max_ms, total_ms=max_ms, code_sha256=digest,
            cases_sha256=cases_sha256, comparison=comparison,
            reject_reason="failed_public_cases", failures=failures,
        )

    # 阶段二：完整用例集（遇到第一个非 AC 立即停止）
    rest = cases[len(head):]
    passed_rest, max_ms_rest, failures_rest = _run_cases(
        code, rest, timeout, comparison=comparison, tolerance=tolerance,
        memory_limit_mb=memory_limit_mb, abort_on_failure=True,
    )
    total_passed = passed_head + passed_rest
    max_ms = max(max_ms, max_ms_rest)
    ok = total_passed == len(cases)
    verdict = "AC" if ok else (failures_rest[0]["verdict"] if failures_rest else "WA")
    if not ok:
        # 提前中止时 cases_passed 只是「中止前通过数」，需标明以正确解读证据
        failed_verdicts = sum(1 for f in failures_rest if "verdict" in f)
        failures_rest.append(
            {
                "note": "aborted_early",
                "cases_skipped": len(rest) - passed_rest - failed_verdicts,
            }
        )
    return Verification(
        ok=ok, verdict=verdict, language=language,
        language_name=proto.LANGUAGE_NAMES.get(language, "?"), tried_index=trial,
        cases_total=len(cases), cases_passed=total_passed, max_ms=max_ms,
        total_ms=max_ms, code_sha256=digest, cases_sha256=cases_sha256,
        comparison=comparison,
        reject_reason="" if ok else "failed_full_case_set", failures=failures_rest,
    )


def suspected_special_judge(detail: dict[str, Any]) -> bool:
    """在没有通过验证的题目里，识别「疑似答案不唯一」的题面外信号。

    题面句式过滤（pool.detect_multiple_answers）只能拦下题面写了
    "print any / multiple answers" 的题。仍有漏网者：题面没写，但答案确实
    不唯一。这类题的表现是——多条解全部以 WA 收场，且输出与期望的 token
    **个数相同、位置对应**（说明结构对了、只是具体方案不同），而第一条
    非空行（通常是「答案个数/颜色数」这类唯一标量）往往是一致的。

    命中该信号不等于一定有问题，只是**值得人工复核**，所以只作为审计线索
    记录，不改变淘汰判定。
    """
    attempts = [a for a in detail.get("attempts", []) if a.get("verdict")]
    if not attempts or any(a.get("verdict") != "WA" for a in attempts):
        return False
    for attempt in attempts:
        for failure in attempt.get("failures", []):
            if "verdict" not in failure or failure["verdict"] != "WA":
                continue
            got = (failure.get("stdout_head") or "").split()
            want = (failure.get("expected_head") or "").split()
            if got and len(got) == len(want):
                return True
    return False


def verify_problem(
    pid: str, problem: dict[str, Any], cfg: dict[str, Any]
) -> tuple[Verification | None, dict[str, Any]]:
    """在一道题的所有 Python 3 正确解中依次尝试，返回 (中标验证, 尝试记录)

    比对策略是两段式：先用**分词精确比对**（token，最严格），失败且失败
    原因符合「数值表示差异」特征时，再用**浮点容差比对**（float，1e-6
    绝对/相对误差）重试一次。这样绝大多数题目仍按最严格标准验证，只有
    确实需要容差的题才放宽，且放宽到哪一档会记录在 evidence 里
    （comparison 字段）—— 应用侧据此按题选用同一口径判题。
    """
    cases, cases_sha256 = select_cases(problem, cfg, pid)
    verification_cfg = cfg["verification"]
    tolerance = float(verification_cfg.get("float_tolerance", FLOAT_TOLERANCE))
    memory_limit_mb = int(verification_cfg.get("memory_limit_mb", 512))
    timeout = min(
        float(problem.get("time_limit_seconds") or cfg["filters"]["default_time_limit_seconds"]),
        float(verification_cfg["timeout_cap_seconds"]),
    )
    max_try = int(verification_cfg["max_solutions_tried"])

    candidates = [s for s in problem["solutions"] if s["language"] == proto.LANGUAGE_PYTHON3]
    # 优先尝试较短的解法（通常更快、更少依赖）——确定性排序，避免随机性
    candidates.sort(key=lambda s: (len(s["solution"]),))

    def _detail(solution_code: str) -> dict[str, Any]:
        return {
            "cases_selected": len(cases),
            "cases_sha256": cases_sha256,
            "timeout_seconds": timeout,
            "memory_limit_mb": memory_limit_mb,
            "candidates_total": len(candidates),
            "attempts": attempts,
            "solution_code": solution_code,
        }

    attempts: list[dict[str, Any]] = []
    for trial, sol in enumerate(candidates[:max_try]):
        result = verify_solution(
            sol["solution"], cases, timeout=timeout, trial=trial,
            cases_sha256=cases_sha256, comparison=DEFAULT_COMPARISON,
            tolerance=tolerance, memory_limit_mb=memory_limit_mb,
        )
        attempts.append(result.to_dict())
        if result.ok:
            return result, _detail(sol["solution"])

        # 分词比对失败，且失败形态符合「数值表示差异」→ 用容差再试一次
        if needs_float_tolerance(result):
            tolerant = verify_solution(
                sol["solution"], cases, timeout=timeout, trial=trial,
                cases_sha256=cases_sha256, comparison=COMPARISON_FLOAT,
                tolerance=tolerance, memory_limit_mb=memory_limit_mb,
            )
            attempts.append(tolerant.to_dict())
            if tolerant.ok:
                return tolerant, _detail(sol["solution"])

    return None, _detail("")


# ---------------------------------------------------------------- 并行入口
def _worker(payload: tuple[str, dict[str, Any], dict[str, Any]]) -> tuple[str, Any, dict[str, Any]]:
    pid, problem, cfg = payload
    try:
        verification, detail = verify_problem(pid, problem, cfg)
    except Exception as exc:  # 单题异常不应中断整批
        return pid, None, {"error": f"{type(exc).__name__}: {exc}"}
    return pid, verification, detail


def verify_many(
    problems: dict[str, dict[str, Any]], cfg: dict[str, Any], *, workers: int = 1, verbose: bool = True
) -> dict[str, tuple[Verification | None, dict[str, Any]]]:
    """批量验证；workers>1 时用进程池并行（每题仍串行尝试其候选解）"""
    items = list(problems.items())
    results: dict[str, tuple[Verification | None, dict[str, Any]]] = {}

    if workers <= 1:
        for index, (pid, problem) in enumerate(items, 1):
            _, verification, detail = _worker((pid, problem, cfg))
            results[pid] = (verification, detail)
            if verbose:
                _report(pid, problem, verification, detail, index, len(items))
        return results

    with ProcessPoolExecutor(max_workers=workers) as pool:
        for index, (pid, verification, detail) in enumerate(
            pool.map(_worker, [(pid, problem, cfg) for pid, problem in items]), 1
        ):
            results[pid] = (verification, detail)
            if verbose:
                _report(pid, problems[pid], verification, detail, index, len(items))
    return results


def _report(
    pid: str,
    problem: dict[str, Any],
    verification: Verification | None,
    detail: dict[str, Any],
    index: int,
    total: int,
) -> None:
    name = problem.get("name", "")[:34]
    if verification is not None:
        print(
            f"  [{index:>4}/{total}] {pid} 通过  {name:<34} "
            f"用例 {verification.cases_passed}/{verification.cases_total}  {verification.max_ms:.0f}ms",
            flush=True,
        )
    else:
        tried = len(detail.get("attempts", []))
        print(
            f"  [{index:>4}/{total}] {pid} 淘汰  {name:<34} 尝试 {tried} 条解均未全 AC",
            flush=True,
        )
