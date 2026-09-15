"""变体构建器（技术方案 §15.3）。

对数据集中的每道题自动构建四类候选解题过程，并写入私有目录
（公开数据树保持干净，符合"冰山"开源策略）：

  1. correct          — 模型求解并经沙箱验证 AC 的正确过程（process_valid=true）
  2. natural_N        — 模型重复求解的自然变体（不标注，评测时按未标注样本排除统计）
  3. injected_<E?>    — 在指定步骤注入指定类型错误（E1-E7）的候选过程
  4. ac_but_invalid_* — 代码能通过全部测试、但过程存在缺陷的候选（E5 复杂度
                        误述 / E6 题解-代码不一致注入后沙箱判为 AC 者）

所有变体的 exec_status 一律以沙箱真实判题为准，不信任模型自述。

输出布局（private_root 默认 private/bench，与 data/ 完全隔离）：
  <private_root>/<pid>/variants.json   # 全部候选（含内联 label）
  <private_root>/<pid>/labels.json     # {"labels": {...}}（与 dataset.py 兼容）
  <private_root>/build_report.json     # 构建报告

用法（需要真实 Hy3 API，mock 路由不含注入模板）：
  python scripts/build_variants.py \
      --data-root data/processeval-cp --private-root private/bench \
      --problems cc-train-010-0033 --inject-types E1,E2,E4,E5,E7
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# 让脚本可直接运行（不装包也能 import hy3_algoaudit）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hy3_algoaudit.config import SandboxLimits, load_settings  # noqa: E402
from hy3_algoaudit.llm import Budget, Hy3Client  # noqa: E402
from hy3_algoaudit.sandbox import make_sandbox  # noqa: E402
from hy3_algoaudit.sandbox.judge import judge_program  # noqa: E402
from hy3_algoaudit.schemas import CodeBlock, StructuredSolution  # noqa: E402
from hy3_algoaudit.solver import Solver  # noqa: E402
from hy3_algoaudit.utils import LLMOutputError, AuditError, extract_json, write_text  # noqa: E402
from hy3_algoaudit.benchmark.dataset import load_problem  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("build_variants")

# 每类错误默认注入的步骤（与错误类型语义对应）
INJECT_STEP = {
    "E1": "S1", "E2": "S2", "E3": "S3", "E4": "S6",
    "E5": "S5", "E6": "S6", "E7": "S7", "E8": "S2",
}
INJECT_DESC = {
    "E1": "题意理解错误（误读条件/约束/输出要求）",
    "E2": "算法选择错误（选用了不能解决该问题的算法路线）",
    "E3": "推导/逻辑错误（转移方程或论证步骤有误）",
    "E4": "条件或边界遗漏（未处理某些边界情形）",
    "E5": "复杂度描述错误（声称的复杂度与真实实现不符）",
    "E6": "题解-代码不一致（步骤描述与代码实际行为脱节）",
    "E7": "实现错误（代码有 bug，运行结果错误或崩溃）",
    "E8": "无依据推断/幻觉（引用不成立的前提且未证明）",
}

INJECT_SYSTEM = """你是算法题解变体构造专家。给你一道题的正确结构化题解（S1-S7 JSON，含代码）。
请在指定步骤注入指定类型的错误，构造一个"带缺陷的候选过程"。要求：

1. 注入步骤之前的所有步骤保持完全正确；
2. 注入步骤及其后的步骤在错误前提下保持内部自洽（错误自然传播，不要在后续步骤自我纠正）；
3. 同步修改代码，使程序真实体现该错误的行为（除非说明要求代码不变）；
4. steps 必须完整覆盖 S1 到 S7；code 必须是可直接运行的完整程序。

只输出严格 JSON：
{"steps": {"S1": "...", "S2": "...", ..., "S7": "..."},
 "code": "完整修改后的 Python 代码",
 "change_note": "一句话说明注入了什么错误"}"""

INJECT_USER = """## 题目

{problem}

## 正确题解（JSON）

{solution}

## 注入要求

- 注入步骤：{step}
- 错误类型：{etype}（{desc}）
{extra}"""

# E5 / E6 是"过程缺陷但代码可 AC"的来源：代码不变或保持正确
EXTRA_BY_TYPE = {
    "E5": "- 特别要求：代码完全不变，仅把 S5 的复杂度论述改成错误版本"
          "（例如把 O(n log n) 说成 O(n)，或虚构错误的复杂度推导）。",
    "E6": "- 特别要求：代码保持正确（判题仍会 AC），但把某步骤的文字描述"
          "改成与代码实际行为不一致（例如声称用了线段树，代码却是暴力枚举）。",
}


def _case_limits(case) -> SandboxLimits | None:
    if case.time_limit_seconds is None and case.memory_limit_mb is None:
        return None
    base = SandboxLimits()
    return SandboxLimits(
        time_limit_seconds=case.time_limit_seconds or base.time_limit_seconds,
        memory_limit_mb=case.memory_limit_mb or base.memory_limit_mb,
        compile_time_limit_seconds=base.compile_time_limit_seconds,
    )


def _judge(sandbox, case, code: CodeBlock):
    return judge_program(sandbox, code, case.tests, _case_limits(case),
                         comparison=case.comparison)


def _steps_dict(solution: StructuredSolution) -> dict:
    return {s.step_id: s.content for s in solution.steps}


def _llm_json(llm, system: str, user: str, budget: Budget = Budget.HIGH,
              retries: int = 2) -> dict:
    last: Exception | None = None
    for _ in range(retries + 1):
        raw = llm.complete(system, user, budget=budget)
        try:
            data = extract_json(raw)
            if isinstance(data, dict):
                return data
        except Exception as e:  # noqa: BLE001
            last = e
    raise LLMOutputError(f"model JSON unparseable: {last}")


def _to_solution(data: dict, fallback_name: str) -> StructuredSolution | None:
    steps = data.get("steps")
    code = data.get("code")
    if not isinstance(steps, dict) or not isinstance(code, str) or not code.strip():
        return None
    items = [{"step_id": sid, "title": "", "content": str(content)}
             for sid, content in steps.items()
             if str(sid).upper().startswith("S")]
    if len(items) < 5:  # 退化变体没有评测价值
        return None
    return StructuredSolution(
        problem_id=fallback_name,
        steps=items,
        code=CodeBlock(language="python", code=code),
    )


def build_correct(llm, solver: Solver, case, sandbox,
                  attempts: int = 3) -> StructuredSolution | None:
    """求解并沙箱验证，直到拿到一个真实 AC 的解。"""
    budget = Budget.HIGH if (case.difficulty or 0) >= 2000 else Budget.LOW
    for i in range(attempts):
        try:
            sol = solver.solve(case.problem_text, budget=budget)
        except LLMOutputError as e:
            logger.warning("[correct] solve parse failed (try %d): %s", i + 1, e)
            continue
        res = _judge(sandbox, case, sol.code)
        if res.accepted:
            return sol
        logger.info("[correct] try %d not AC: %s %s", i + 1, res.status.value,
                    (res.detail or "")[:120])
    return None


def build_natural(llm, solver: Solver, case, sandbox, idx: int):
    """自然变体：重复求解一次并真实判题；不标注过程有效性。"""
    try:
        sol = solver.solve(case.problem_text, budget=Budget.LOW)
    except LLMOutputError as e:
        logger.warning("[natural] solve parse failed: %s", e)
        return None
    res = _judge(sandbox, case, sol.code)
    return {
        "name": f"natural_{idx}",
        "language": "python",
        "steps": _steps_dict(sol),
        "code": sol.code.code,
        "label": {"exec_status": res.status.value, "process_valid": None,
                  "first_error": None, "error_type": "NONE",
                  "ac_but_invalid": False},
        "sandbox_detail": (res.detail or "")[:200],
    }


def build_injected(llm, case, sandbox, correct: StructuredSolution,
                   etype: str):
    step = INJECT_STEP.get(etype, "S3")
    user = INJECT_USER.format(
        problem=case.problem_text,
        solution=correct.model_dump_json(indent=2),
        step=step, etype=etype, desc=INJECT_DESC.get(etype, etype),
        extra=EXTRA_BY_TYPE.get(etype, ""),
    )
    data = _llm_json(llm, INJECT_SYSTEM, user)
    sol = _to_solution(data, f"injected_{etype.lower()}")
    if sol is None:
        logger.warning("[inject %s] bad model output, skipped", etype)
        return None
    res = _judge(sandbox, case, sol.code)
    exec_status = res.status.value
    # ac_but_invalid：代码真的 AC、但过程被注入了缺陷（E5/E6 的设计目标）
    abi = res.accepted
    return {
        "name": f"injected_{etype.lower()}",
        "language": "python",
        "steps": _steps_dict(sol),
        "code": sol.code.code,
        "label": {"exec_status": exec_status, "process_valid": False,
                  "first_error": step, "error_type": etype,
                  "ac_but_invalid": abi},
        "change_note": str(data.get("change_note", "")),
        "sandbox_detail": (res.detail or "")[:200],
    }


def process_problem(llm, case, sandbox, args) -> dict:
    pid = case.problem_id
    logger.info("==== %s (difficulty=%s, tests=%d) ====",
                pid, case.difficulty, len(case.tests))
    solver = Solver(llm)

    correct = build_correct(llm, solver, case, sandbox, attempts=args.correct_attempts)
    if correct is None:
        logger.error("[%s] 无法得到 AC 的正确解，跳过该题", pid)
        return {"problem": pid, "ok": False, "reason": "no_ac_solution"}

    variants: list[dict] = [{
        "name": "correct",
        "language": "python",
        "steps": _steps_dict(correct),
        "code": correct.code.code,
        "label": {"exec_status": "AC", "process_valid": True,
                  "first_error": None, "error_type": "NONE",
                  "ac_but_invalid": False},
    }]

    for i in range(args.max_natural):
        v = build_natural(llm, solver, case, sandbox, i)
        if v:
            variants.append(v)

    for etype in args.inject_types:
        v = build_injected(llm, case, sandbox, correct, etype)
        if v:
            variants.append(v)
            if v["label"]["ac_but_invalid"] and etype not in ("E5", "E6"):
                logger.warning("[%s] 注入 %s 后代码仍然 AC（意外，已如实标注）",
                               pid, etype)

    out_dir = Path(args.private_root) / pid
    out_dir.mkdir(parents=True, exist_ok=True)
    write_text(out_dir / "variants.json",
               json.dumps(variants, ensure_ascii=False, indent=2))
    write_text(out_dir / "labels.json", json.dumps(
        {"labels": {v["name"]: v["label"] for v in variants}},
        ensure_ascii=False, indent=2))

    n_abi = sum(1 for v in variants if v["label"].get("ac_but_invalid"))
    logger.info("[%s] 完成：variants=%d（correct=1, natural=%d, injected=%d, abi=%d）",
                pid, len(variants), args.max_natural,
                len(args.inject_types), n_abi)
    return {"problem": pid, "ok": True, "variants": len(variants),
            "abi_variants": n_abi}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="构建四类候选过程变体（§15.3）")
    p.add_argument("--data-root", default="data/processeval-cp")
    p.add_argument("--private-root", default="private/bench",
                   help="变体与标注输出根目录（不得位于公开数据树内）")
    p.add_argument("--problems", default="",
                   help="逗号分隔的题目 ID 子集，默认全部")
    p.add_argument("--inject-types", default="E1,E2,E3,E4,E5,E6,E7")
    p.add_argument("--max-natural", type=int, default=1)
    p.add_argument("--correct-attempts", type=int, default=3)
    args = p.parse_args(argv)

    if args.max_natural < 0 or not args.inject_types:
        p.error("--inject-types 不能为空")
    args.inject_types = [t.strip().upper() for t in
                         args.inject_types.split(",") if t.strip()]
    unknown = [t for t in args.inject_types if t not in INJECT_STEP]
    if unknown:
        p.error(f"未知错误类型: {unknown}（支持 E1-E8）")

    wanted = [s.strip() for s in args.problems.split(",") if s.strip()]

    settings = load_settings()
    settings.require_api()  # 注入模板没有 mock 路由，必须真实调用
    llm = Hy3Client(settings)
    sandbox = make_sandbox(settings.sandbox_backend, settings.limits)

    root = Path(args.data_root)
    dirs = sorted(d for d in root.iterdir()
                  if d.is_dir() and (d / "problem.md").exists()) if root.exists() else []
    if wanted:
        dirs = [d for d in dirs if d.name in wanted or
                (d / "meta.json").exists() and
                json.loads((d / "meta.json").read_text(encoding="utf-8")).get("id") in wanted]

    if not dirs:
        print(f"未找到题目目录：{root}（或 --problems 过滤后为空）", file=sys.stderr)
        return 1

    report = []
    for d in dirs:
        case = load_problem(d)
        try:
            report.append(process_problem(llm, case, sandbox, args))
        except (AuditError, LLMOutputError) as e:
            logger.error("[%s] 构建失败: %s", case.problem_id, e)
            report.append({"problem": case.problem_id, "ok": False,
                           "reason": str(e)})

    out = Path(args.private_root) / "build_report.json"
    write_text(out, json.dumps(report, ensure_ascii=False, indent=2))
    ok = sum(1 for r in report if r["ok"])
    print(f"\n构建完成：{ok}/{len(report)} 题成功，报告见 {out}")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
