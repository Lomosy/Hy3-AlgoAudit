"""修复策略对比实验（技术方案 §17，Refine@k）。

在数据集上对比三种修复策略的"翻盘"能力：

  A. one-shot       — 只求解一次，不修复（基线）
  B. execution-only — 仅凭 WA/TLE/RE 执行反馈盲修（对照基线）
  C. process-guided — 首错定位驱动的定向修复（本系统主策略）

流程：每题先做一次 one-shot 判题；
  - 若已 AC：三种策略均记为第 0 轮解决（修复无从谈起）；
  - 若未 AC：对 B、C 各跑最多 --max-k 轮修复循环，记录首次 AC 的轮次。

指标：Refine@k = 初始失败题中在 ≤k 轮内修到 AC 的比例；
同时汇总整体 AC 率、平均修复轮次与各策略 token 开销。

用法：
  python scripts/run_repair_experiment.py --dataset data/processeval-cp \
      [--private private/bench] [--max-k 3] [--out results/repair_experiment] [--mock]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hy3_algoaudit.benchmark.dataset import load_dataset  # noqa: E402
from hy3_algoaudit.benchmark.runner import _case_limits  # noqa: E402
from hy3_algoaudit.config import Settings, load_settings  # noqa: E402
from hy3_algoaudit.llm import Budget, LLMClient, decide_budget  # noqa: E402
from hy3_algoaudit.pipeline import AlgoAuditPipeline  # noqa: E402
from hy3_algoaudit.solver import RepairAgent, Solver  # noqa: E402
from hy3_algoaudit.utils import LLMOutputError, write_text  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("repair_experiment")

STRATEGIES = ("one_shot", "exec_only", "process_guided")


def _snapshot(llm: LLMClient) -> tuple[int, int]:
    return llm.usage.total_tokens, llm.usage.calls


def _delta(llm: LLMClient, snap: tuple[int, int]) -> dict:
    tokens, calls = _snapshot(llm)
    return {"tokens": tokens - snap[0], "llm_calls": calls - snap[1]}


def _run_exec_only(llm, pipeline: AlgoAuditPipeline, repairer: RepairAgent,
                   case, solution, exec_result, k: int) -> dict:
    """策略 B：只看执行反馈的盲修循环。"""
    snap = _snapshot(llm)
    rounds_used = None
    notes = []
    sol, exec_res = solution, exec_result
    if exec_res is not None and exec_res.accepted:
        rounds_used = 0
    budget = Budget.LOW
    for r in range(1, (k if rounds_used is None else 0) + 1):
        if exec_res is None:
            break
        try:
            sol = repairer.repair_execution_only(case.problem_text, sol,
                                                 exec_res, budget=budget)
        except LLMOutputError as e:
            notes.append(f"round {r}: repair parse failed ({e})")
            continue
        exec_res = pipeline._run_judge(sol.code, case.tests,
                                       case.comparison, _case_limits(case))
        notes.append(f"round {r}: exec={exec_res.status.value}")
        if exec_res.accepted:
            rounds_used = r
            break
    return {"solved_round": rounds_used,
            "final_exec": exec_res.status.value if exec_res else None,
            "notes": notes, "cost": _delta(llm, snap)}


def _run_process_guided(llm, pipeline: AlgoAuditPipeline, repairer: RepairAgent,
                        case, solution, exec_result, k: int,
                        settings: Settings) -> dict:
    """策略 C：首错定位驱动的定向修复循环。"""
    snap = _snapshot(llm)
    rounds_used = None
    dual_round = None
    notes = []
    sol, exec_res = solution, exec_result
    if exec_res is not None and exec_res.accepted:
        rounds_used = 0
    budget = decide_budget(difficulty_rating=case.difficulty,
                           high_threshold=settings.difficulty_high_threshold,
                           phase="solve")
    for r in range(1, (k if rounds_used is None else 0) + 1):
        try:
            verdict = pipeline.evaluator.evaluate(
                case.problem_text, sol, exec_result=exec_res,
                reference_code=case.reference,
                difficulty_rating=case.difficulty,
                limits=_case_limits(case),
            )
        except LLMOutputError as e:
            notes.append(f"round {r}: evaluate failed ({e})")
            continue
        if exec_res is not None and exec_res.accepted and verdict.process_valid:
            dual_round = r - 1 if r == 1 else dual_round
            break
        conflict = (exec_res is not None and exec_res.accepted
                    and not verdict.process_valid)
        budget = decide_budget(
            difficulty_rating=case.difficulty,
            high_threshold=settings.difficulty_high_threshold,
            conflict=conflict, failed_repair_rounds=r, phase="repair")
        try:
            sol = repairer.repair(case.problem_text, sol, verdict,
                                  exec_res, budget=budget)
        except LLMOutputError as e:
            notes.append(f"round {r}: repair parse failed ({e})")
            continue
        exec_res = pipeline._run_judge(sol.code, case.tests,
                                       case.comparison, _case_limits(case))
        notes.append(f"round {r}: first_error={verdict.first_error_step or '-'} "
                     f"({verdict.error_type.value}) -> exec={exec_res.status.value}")
        if exec_res.accepted and rounds_used is None:
            rounds_used = r
            break
    return {"solved_round": rounds_used, "dual_round": dual_round,
            "final_exec": exec_res.status.value if exec_res else None,
            "notes": notes, "cost": _delta(llm, snap)}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="修复策略 Refine@k 对比实验（§17）")
    p.add_argument("--dataset", default="data/processeval-cp")
    p.add_argument("--private", default=None,
                   help="私有材料根目录（并入候选与标注）")
    p.add_argument("--problems", default="", help="逗号分隔题目 ID 子集")
    p.add_argument("--max-k", type=int, default=3)
    p.add_argument("--out", default="results/repair_experiment")
    p.add_argument("--mock", action="store_true", help="离线 mock（演示用）")
    args = p.parse_args(argv)

    settings = load_settings()
    if args.mock:
        from hy3_algoaudit.demo_data import build_mock_routes
        from hy3_algoaudit.llm import MockLLMClient
        llm = MockLLMClient(build_mock_routes())
    else:
        from hy3_algoaudit.llm import Hy3Client
        settings.require_api()
        llm = Hy3Client(settings)

    from hy3_algoaudit.sandbox import make_sandbox
    sandbox = make_sandbox(settings.sandbox_backend, settings.limits)
    pipeline = AlgoAuditPipeline(llm, settings, sandbox)
    solver = Solver(llm)
    repairer = RepairAgent(llm, solver)

    cases = load_dataset(args.dataset, private_root=args.private)
    wanted = {s.strip() for s in args.problems.split(",") if s.strip()}
    if wanted:
        cases = [c for c in cases if c.problem_id in wanted]
    if not cases:
        print("数据集为空", file=sys.stderr)
        return 1

    records = []
    for case in cases:
        logger.info("==== %s ====", case.problem_id)
        limits = _case_limits(case)
        try:
            budget = decide_budget(difficulty_rating=case.difficulty,
                                   high_threshold=settings.difficulty_high_threshold,
                                   phase="solve")
            sol0 = solver.solve(case.problem_text, budget=budget)
            exec0 = pipeline._run_judge(sol0.code, case.tests,
                                        case.comparison, limits)
        except LLMOutputError as e:
            logger.error("[%s] one-shot solve failed: %s", case.problem_id, e)
            records.append({"problem": case.problem_id, "error": str(e)})
            continue

        init_ac = exec0 is not None and exec0.accepted
        rec = {
            "problem": case.problem_id,
            "difficulty": case.difficulty,
            "initial_exec": exec0.status.value if exec0 else "NOT_RUN",
            "strategies": {
                "one_shot": {
                    "solved_round": 0 if init_ac else None,
                    "final_exec": exec0.status.value if exec0 else None,
                    "notes": [], "cost": {"tokens": 0, "llm_calls": 0},
                },
            },
        }
        if not init_ac and exec0 is not None:
            rec["strategies"]["exec_only"] = _run_exec_only(
                llm, pipeline, repairer, case, sol0, exec0, args.max_k)
            rec["strategies"]["process_guided"] = _run_process_guided(
                llm, pipeline, repairer, case, sol0, exec0, args.max_k, settings)
        elif init_ac:
            rec["strategies"]["exec_only"] = {
                "solved_round": 0, "final_exec": "AC", "notes": [],
                "cost": {"tokens": 0, "llm_calls": 0}}
            rec["strategies"]["process_guided"] = {
                "solved_round": 0, "dual_round": 0, "final_exec": "AC",
                "notes": [], "cost": {"tokens": 0, "llm_calls": 0}}
        records.append(rec)
        logger.info("[%s] done: one_shot=%s exec_only=%s process_guided=%s",
                    case.problem_id,
                    rec["strategies"]["one_shot"]["solved_round"],
                    rec["strategies"].get("exec_only", {}).get("solved_round"),
                    rec["strategies"].get("process_guided", {}).get("solved_round"))

    # ------------------------------------------------------------- aggregate
    def agg(strategy: str) -> dict:
        n = sum(1 for r in records if "strategies" in r)
        init_failed = [r for r in records if "strategies" in r
                       and r["strategies"]["one_shot"]["solved_round"] is None]
        solved = [r for r in records if "strategies" in r
                  and r["strategies"].get(strategy, {}).get("solved_round") is not None]
        solved_in_failed = [r for r in init_failed
                            if r["strategies"].get(strategy, {}).get("solved_round") is not None]
        by_k = {k: round(sum(1 for r in solved_in_failed
                             if r["strategies"][strategy]["solved_round"] <= k)
                         / len(init_failed), 4) if init_failed else None
                for k in range(1, args.max_k + 1)}
        rounds = [r["strategies"][strategy]["solved_round"] for r in solved_in_failed
                  if r["strategies"][strategy]["solved_round"] is not None]
        return {
            "n_total": n,
            "n_initial_failed": len(init_failed),
            "n_solved": len(solved),
            "overall_ac_rate": round(len(solved) / n, 4) if n else None,
            "refine_at_k_on_failed": by_k,
            "avg_rounds_when_fixed": round(sum(rounds) / len(rounds), 2) if rounds else None,
            "tokens": sum(r["strategies"].get(strategy, {})
                          .get("cost", {"tokens": 0})["tokens"] for r in records),
        }

    summary = {s: agg(s) for s in STRATEGIES}
    summary["_meta"] = {"dataset": args.dataset, "private": args.private,
                        "max_k": args.max_k, "mock": args.mock,
                        "model": settings.model or "mock"}

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    write_text(out / "summary.json",
               json.dumps({"summary": summary, "records": records},
                          ensure_ascii=False, indent=2))

    lines = ["# 修复策略对比实验（§17 Refine@k）", "",
             f"- 数据集：`{args.dataset}`（{summary['one_shot']['n_total']} 题，"
             f"初始失败 {summary['one_shot']['n_initial_failed']} 题）",
             f"- 最大修复轮数 k = {args.max_k}", "",
             "| 策略 | 整体 AC 率 | " + " | ".join(f"Refine@{k}" for k in range(1, args.max_k + 1))
             + " | 平均修复轮次 | token 开销 |",
             "|---|---|---|---|---|---|"]
    name_zh = {"one_shot": "A. One-shot（不修复）",
               "exec_only": "B. Execution-only（盲修）",
               "process_guided": "C. Process-guided（首错定向）"}
    for s in STRATEGIES:
        a = summary[s]
        lines.append(
            f"| {name_zh[s]} | {a['overall_ac_rate']} | "
            + " | ".join(str(a["refine_at_k_on_failed"][k]) for k in range(1, args.max_k + 1))
            + f" | {a['avg_rounds_when_fixed']} | {a['tokens']} |")
    lines += ["", "> Refine@k 在初始失败的题目子集上计算。"]
    write_text(out / "summary.md", "\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\n结果已写入 {out / 'summary.json'} 与 {out / 'summary.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
