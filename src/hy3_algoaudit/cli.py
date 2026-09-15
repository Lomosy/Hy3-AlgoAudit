"""Hy3-AlgoAudit CLI.

Subcommands:
  solve      AI auto-solve with dual-track evaluation and guided repair
  evaluate   assess a user-provided structured solution
  judge      sandbox-only result judging
  benchmark  run solver/evaluator capability benchmarks on a dataset
  demo       end-to-end demo on a bundled sample (mock or real LLM)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import Settings, load_settings
from .llm import Hy3Client, MockLLMClient
from .pipeline import AlgoAuditPipeline
from .report import render_html, render_markdown
from .sandbox import make_sandbox
from .sandbox.judge import COMPARISON_MODES, DEFAULT_COMPARISON
from .schemas import CodeBlock, StepContent, StructuredSolution, TestCase
from .utils import AuditError, ConfigError, read_text, write_text

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")


# ------------------------------------------------------------------ helpers

def _load_tests(path: str | None) -> list[TestCase]:
    if not path:
        return []
    data = json.loads(read_text(path))
    return [TestCase(input=t.get("input", ""), expected=t.get("expected", ""))
            for t in data]


def _load_reference(path: str | None) -> CodeBlock | None:
    if not path:
        return None
    p = Path(path)
    lang = "python" if p.suffix == ".py" else "cpp"
    return CodeBlock(language=lang, code=read_text(p))


def _load_solution_json(path: str) -> StructuredSolution:
    data = json.loads(read_text(path))
    if "steps" in data:  # full structured solution
        return StructuredSolution.model_validate(data)
    # simple format: {"S1": "...", ..., "code": {"language": ..., "code": ...}}
    steps = [{"step_id": k, "title": "", "content": v}
             for k, v in data.items() if k.startswith("S")]
    code = data.get("code") or {}
    if isinstance(code, str):
        code = {"language": "python", "code": code}
    return StructuredSolution(steps=steps, code=CodeBlock(**code))


def _build_llm(args, settings: Settings):
    if getattr(args, "mock", False):
        from .demo_data import build_mock_routes
        return MockLLMClient(build_mock_routes()), True
    return Hy3Client(settings), False


def _emit_report(report, out: str | None) -> None:
    md = render_markdown(report)
    print(md)
    if out:
        write_text(out, md)
        html_path = str(Path(out).with_suffix(".html"))
        write_text(html_path, render_html(report))
        print(f"\n[报告已写入] {out} 与 {html_path}", file=sys.stderr)


# ------------------------------------------------------------------ commands

def cmd_solve(args) -> int:
    settings = load_settings()
    llm, is_mock = _build_llm(args, settings)
    if not is_mock:
        settings.require_api()
    sandbox = make_sandbox(settings.sandbox_backend, settings.limits)
    pipeline = AlgoAuditPipeline(llm, settings, sandbox)
    report = pipeline.solve_mode(
        problem_text=read_text(args.problem),
        problem_id=args.id or Path(args.problem).stem,
        tests=_load_tests(args.tests),
        reference_code=_load_reference(args.reference),
        max_repair=args.max_repair,
    )
    _emit_report(report, args.out)
    return 0


def cmd_evaluate(args) -> int:
    settings = load_settings()
    llm, is_mock = _build_llm(args, settings)
    if not is_mock:
        settings.require_api()
    sandbox = make_sandbox(settings.sandbox_backend, settings.limits)
    pipeline = AlgoAuditPipeline(llm, settings, sandbox)
    report = pipeline.evaluate_mode(
        problem_text=read_text(args.problem),
        solution=_load_solution_json(args.solution),
        problem_id=args.id or Path(args.problem).stem,
        tests=_load_tests(args.tests),
        reference_code=_load_reference(args.reference),
    )
    _emit_report(report, args.out)
    return 0


def cmd_judge(args) -> int:
    settings = load_settings()
    sandbox = make_sandbox(settings.sandbox_backend, settings.limits)
    from .sandbox.judge import judge_program
    code_path = Path(args.code)
    lang = {"py": "python", "python": "python", "cpp": "cpp", "cc": "cpp",
            "c++": "cpp"}.get(code_path.suffix.lstrip(".").lower(), "python")
    code = CodeBlock(language=lang, code=read_text(code_path))
    result = judge_program(sandbox, code, _load_tests(args.tests), settings.limits,
                           comparison=args.comparison)
    print(json.dumps(json.loads(result.model_dump_json()), ensure_ascii=False, indent=2))
    return 0 if result.accepted else 1


def cmd_benchmark(args) -> int:
    from .benchmark.dataset import load_dataset
    from .benchmark.runner import BenchmarkRunner
    settings = load_settings()
    llm, is_mock = _build_llm(args, settings)
    if not is_mock:
        settings.require_api()
    sandbox = make_sandbox(settings.sandbox_backend, settings.limits)
    cases = load_dataset(args.dataset, private_root=args.private)
    if not cases:
        print(f"数据集为空：{args.dataset}", file=sys.stderr)
        return 1
    runner = BenchmarkRunner(llm, settings, sandbox)
    if args.only in ("solver", "both"):
        sm = runner.run_solver(cases, args.out, max_repair=args.max_repair)
        print(f"[solver] n={sm.n} Pass@1={sm.pass_at_1} "
              f"ProcessValid={sm.process_valid_rate} DualCorrect={sm.dual_correct_rate}")
    if args.only in ("evaluator", "both"):
        em = runner.run_evaluator(cases, args.out)
        print(f"[evaluator] n={em.n} ProcessF1={em.process_f1} FPR={em.fpr} "
              f"FirstErrorExact={em.first_error_exact_acc} "
              f"MacroF1={em.error_type_macro_f1()}")
    return 0


def cmd_demo(args) -> int:
    """End-to-end demo on the bundled max-subarray sample.

    Uses MockLLMClient by default (no API key needed); pass --real to call Hy3.
    """
    root = Path(__file__).resolve().parent.parent.parent / "demo" / "samples" / "max_subarray"
    if not root.exists():
        print("示例数据缺失，请检查 data/samples/max_subarray", file=sys.stderr)
        return 1

    settings = load_settings()
    if args.real:
        llm = Hy3Client(settings)
        settings.require_api()
    else:
        from .demo_data import build_mock_routes
        llm = MockLLMClient(build_mock_routes())
    sandbox = make_sandbox(settings.sandbox_backend, settings.limits)
    pipeline = AlgoAuditPipeline(llm, settings, sandbox)

    from .benchmark.dataset import load_problem
    case = load_problem(root)

    print("=== 场景一：AI 自动解题（含双通道评估与定向修复）===")
    report = pipeline.solve_mode(case.problem_text, problem_id=case.problem_id,
                                 tests=case.tests,
                                 reference_code=case.reference,
                                 difficulty_rating=case.difficulty)
    print(render_markdown(report))

    print("\n=== 场景二：用户解答评估（AC-but-Invalid 样本）===")
    abi = next((v for v in case.variants if v.label_ac_but_invalid), None)
    if abi is not None:
        report2 = pipeline.evaluate_mode(
            case.problem_text, abi.solution, problem_id=f"{case.problem_id}/{abi.name}",
            tests=case.tests, reference_code=case.reference,
            difficulty_rating=case.difficulty)
        print(render_markdown(report2))
    return 0


# ------------------------------------------------------------------ parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hy3-audit",
                                description="Hy3-AlgoAudit: 结果×过程双轨评估与首错定位系统")
    p.add_argument("--mock", action="store_true",
                   help="use scripted mock LLM (offline mode)")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("solve", help="AI 自动解题 + 双轨评估 + 定向修复")
    sp.add_argument("--problem", required=True, help="题目文本文件")
    sp.add_argument("--tests", help="tests.json 路径")
    sp.add_argument("--reference", help="参考解法文件（.py/.cpp）")
    sp.add_argument("--id", help="题目 ID")
    sp.add_argument("--max-repair", type=int, default=None)
    sp.add_argument("--out", help="报告输出路径（.md，同时生成 .html）")
    sp.set_defaults(func=cmd_solve)

    ep = sub.add_parser("evaluate", help="用户解答评估")
    ep.add_argument("--problem", required=True)
    ep.add_argument("--solution", required=True, help="题解 JSON 文件")
    ep.add_argument("--tests")
    ep.add_argument("--reference")
    ep.add_argument("--id")
    ep.add_argument("--out")
    ep.set_defaults(func=cmd_evaluate)

    jp = sub.add_parser("judge", help="仅沙箱判题")
    jp.add_argument("--code", required=True)
    jp.add_argument("--tests", required=True)
    jp.add_argument(
        "--comparison",
        choices=list(COMPARISON_MODES),
        default=DEFAULT_COMPARISON,
        help="输出比对模式，默认 token；数据集里 meta.json 的 comparison 字段取值可直接传入",
    )
    jp.set_defaults(func=cmd_judge)

    bp = sub.add_parser("benchmark", help="数据集评测")
    bp.add_argument("--dataset", required=True, help="数据集根目录（每题一目录）")
    bp.add_argument("--private", default=None,
                    help="私有材料根目录（并入注入候选与 ground truth 标注）")
    bp.add_argument("--out", default="results")
    bp.add_argument("--only", choices=["solver", "evaluator", "both"], default="both")
    bp.add_argument("--max-repair", type=int, default=None)
    bp.set_defaults(func=cmd_benchmark)

    dp = sub.add_parser("demo", help="端到端演示（默认离线 mock）")
    dp.add_argument("--real", action="store_true", help="调用真实 Hy3 API")
    dp.set_defaults(func=cmd_demo)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as e:
        print(f"[配置错误] {e}", file=sys.stderr)
        return 2
    except AuditError as e:
        print(f"[错误] {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
