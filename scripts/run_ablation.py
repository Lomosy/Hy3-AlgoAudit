"""过程评估器消融实验（技术方案 §21）。

在带标注的候选过程（variants + labels）上，对 ProcessEvaluator 的
六种配置做对比评测，量化每个组件的贡献：

  1. full               — 全部组件开启（完整系统）
  2. no_structural      — 去掉 Layer1 规则/结构检查
  3. no_consistency     — 去掉题解-代码一致性检查
  4. no_counterexample  — 去掉反例验证
  5. no_fine_verify     — 去掉二阶段精细验证（只保留低预算快扫）
  6. fixed_low_budget   — 关闭自适应推理预算（固定低预算）

指标：过程判定 P/R/F1、误报率 FPR、首错定位 Exact/±1、
错误类型 Macro-F1、AC-but-Invalid 检出率。

用法：
  python scripts/run_ablation.py --dataset data/processeval-cp \
      --private private/bench [--out results/ablation] [--mock]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hy3_algoaudit.benchmark.dataset import load_dataset  # noqa: E402
from hy3_algoaudit.benchmark.runner import BenchmarkRunner  # noqa: E402
from hy3_algoaudit.config import Settings, load_settings  # noqa: E402
from hy3_algoaudit.evaluator import ProcessEvaluator  # noqa: E402
from hy3_algoaudit.utils import write_text  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ablation")

#: §21 六种消融配置（name -> ProcessEvaluator 开关 kwargs）
ABLATION_CONFIGS: list[tuple[str, str, dict]] = [
    ("full", "完整系统（全部组件）", {}),
    ("no_structural", "去掉 Layer1 规则/结构检查",
     {"enable_structural": False}),
    ("no_consistency", "去掉题解-代码一致性检查",
     {"enable_consistency": False}),
    ("no_counterexample", "去掉反例验证",
     {"enable_counterexample": False}),
    ("no_fine_verify", "去掉二阶段精细验证",
     {"enable_fine_verify": False}),
    ("fixed_low_budget", "关闭自适应推理预算（固定低预算）",
     {"adaptive_budget": False}),
]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="过程评估器消融实验（§21）")
    p.add_argument("--dataset", default="data/processeval-cp")
    p.add_argument("--private", default=None,
                   help="私有材料根目录（并入候选与标注；无标注样本自动跳过统计）")
    p.add_argument("--problems", default="", help="逗号分隔题目 ID 子集")
    p.add_argument("--out", default="results/ablation")
    p.add_argument("--mock", action="store_true", help="离线 mock（演示用）")
    args = p.parse_args(argv)

    settings: Settings = load_settings()
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

    cases = load_dataset(args.dataset, private_root=args.private)
    wanted = {s.strip() for s in args.problems.split(",") if s.strip()}
    if wanted:
        cases = [c for c in cases if c.problem_id in wanted]
    n_labeled = sum(1 for c in cases for v in c.variants if v.labeled)
    if not n_labeled:
        print("数据集中没有带标注的候选过程（请先用 build_variants.py 构建并"
              "通过 --private 挂载），消融实验无从计算。", file=sys.stderr)
        return 1
    logger.info("加载 %d 题，共 %d 个带标注候选", len(cases), n_labeled)

    runner = BenchmarkRunner(llm, settings, sandbox)
    results: dict[str, dict] = {}
    for name, desc, kwargs in ABLATION_CONFIGS:
        logger.info("---- 配置 %s：%s ----", name, desc)
        evaluator = ProcessEvaluator(llm, settings, sandbox, **kwargs)
        em = runner.run_evaluator(cases, Path(args.out) / name,
                                  evaluator=evaluator, write_reports=False)
        results[name] = {"desc": desc, "metrics": em.to_dict()}
        logger.info("[%s] n=%d F1=%s FPR=%s FirstErrorExact=%s MacroF1=%s",
                    name, em.n, em.process_f1, em.fpr,
                    em.first_error_exact_acc, em.error_type_macro_f1())

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    write_text(out / "ablation.json",
               json.dumps(results, ensure_ascii=False, indent=2))

    # ------------------------------------------------------- markdown table
    def row(name: str) -> str:
        m = results[name]["metrics"]
        pr = m["process"]
        fe = m["first_error"]
        et = m["error_type"]
        abi = m["ac_but_invalid"]
        return (f"| {name} | {results[name]['desc']} | {m['n']} "
                f"| {pr['precision']} | {pr['recall']} | {pr['f1']} "
                f"| {pr['false_positive_rate']} "
                f"| {fe['exact_accuracy']} | {fe['pm1_accuracy']} "
                f"| {et['macro_f1']} | {abi['recall']} |")

    header = ("| 配置 | 说明 | n | 过程P | 过程R | 过程F1 | FPR "
              "| 首错Exact | 首错±1 | 类型Macro-F1 | ABI召回 |")
    sep = "|---|---|---|---|---|---|---|---|---|---|---|"
    lines = ["# 过程评估器消融实验（§21）", "", header, sep]
    lines.append(row("full"))
    for name, _, _ in ABLATION_CONFIGS[1:]:
        lines.append(row(name))
    lines += ["", f"- 数据集：`{args.dataset}`，带标注候选 {n_labeled} 个。",
              "- 各配置相对 full 的差异即该组件的净贡献。"]
    write_text(out / "ablation.md", "\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\n结果已写入 {out / 'ablation.json'} 与 {out / 'ablation.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
