"""Benchmark runner: solver capability run + evaluator capability run.

Produces results JSON + Markdown analysis (difficulty curves, error-type
distribution, algorithm-tag breakdown) into an output directory.
"""
from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from pathlib import Path

from ..config import SandboxLimits, Settings
from ..evaluator import ProcessEvaluator
from ..evaluator.fusion import compute_quadrant
from ..llm import LLMClient
from ..pipeline import AlgoAuditPipeline
from ..report import render_markdown
from ..schemas import AuditReport
from ..sandbox.base import Sandbox
from ..utils import write_text
from .dataset import ProblemCase, Variant
from .metrics import EvaluatorMetrics, SolverMetrics, first_error_match

logger = logging.getLogger(__name__)


def _case_limits(case) -> SandboxLimits | None:
    """Per-problem judge limits from meta.json (None fields fall back to
    the global settings inside the pipeline)."""
    if case.time_limit_seconds is None and case.memory_limit_mb is None:
        return None
    base = SandboxLimits()
    return SandboxLimits(
        time_limit_seconds=case.time_limit_seconds or base.time_limit_seconds,
        memory_limit_mb=case.memory_limit_mb or base.memory_limit_mb,
        compile_time_limit_seconds=base.compile_time_limit_seconds,
    )


class BenchmarkRunner:
    def __init__(self, llm: LLMClient, settings: Settings,
                 sandbox: Sandbox | None = None):
        self.llm = llm
        self.settings = settings
        self.sandbox = sandbox
        self.pipeline = AlgoAuditPipeline(llm, settings, sandbox)

    # -------------------------------------------------------- solver run

    def run_solver(self, cases: list[ProblemCase], out_dir: str | Path,
                   max_repair: int | None = None) -> SolverMetrics:
        out_dir = Path(out_dir)
        sm = SolverMetrics()
        records = []
        for case in cases:
            logger.info("[solver] %s (difficulty=%s)", case.problem_id, case.difficulty)
            report = self.pipeline.solve_mode(
                case.problem_text, problem_id=case.problem_id, tests=case.tests,
                reference_code=case.reference, difficulty_rating=case.difficulty,
                max_repair=max_repair, comparison=case.comparison,
                limits=_case_limits(case),
            )
            sm.n += 1
            if report.execution is not None and report.execution.accepted:
                sm.passed += 1
            if report.process.process_valid:
                sm.process_valid += 1
            if (report.execution is not None and report.execution.accepted
                    and report.process.process_valid):
                sm.dual_correct += 1
            if report.repair_rounds and report.execution is not None \
                    and report.execution.accepted:
                sm.refine_success[report.repair_rounds] += 1
            self._write_report(out_dir / "solver" / case.problem_id, report)
            records.append(self._case_record(case, report))
        write_text(out_dir / "solver" / "summary.json", json.dumps({
            "pass_at_1": sm.pass_at_1,
            "process_valid_rate": sm.process_valid_rate,
            "dual_correct_rate": sm.dual_correct_rate,
            "refine_at_1": sm.refine_at(1),
            "refine_at_2": sm.refine_at(2),
            "refine_at_3": sm.refine_at(3),
            "by_difficulty": self._difficulty_breakdown(records),
            "by_tag": self._tag_breakdown(records),
            "records": records,
        }, ensure_ascii=False, indent=2))
        return sm

    # ------------------------------------------------------ evaluator run

    def run_evaluator(self, cases: list[ProblemCase],
                      out_dir: str | Path,
                      evaluator: ProcessEvaluator | None = None,
                      write_reports: bool = True) -> EvaluatorMetrics:
        """Evaluate every labeled variant: process-detection P/R/F1, first-error
        accuracy, error-type Macro-F1, FPR, AC-but-Invalid detection.

        `evaluator` allows injecting a pre-configured ProcessEvaluator
        (ablation switches, spec section 21); defaults to the full config.
        """
        out_dir = Path(out_dir)
        em = EvaluatorMetrics()
        evaluator = evaluator or ProcessEvaluator(self.llm, self.settings, self.sandbox)
        records = []
        for case in cases:
            for variant in case.variants:
                verdict = evaluator.evaluate(
                    case.problem_text, variant.solution,
                    exec_result=None,  # exec evidence comes from the variant label world
                    reference_code=case.reference,
                    difficulty_rating=case.difficulty,
                    limits=_case_limits(case),
                )
                truly_invalid = variant.label_process_valid is False
                flagged_invalid = not verdict.process_valid

                if variant.label_process_valid is None:
                    pass  # unlabeled (natural) sample: excluded from detection stats
                else:
                    em.n += 1
                if truly_invalid and flagged_invalid:
                    em.tp += 1
                elif not truly_invalid and flagged_invalid:
                    em.fp += 1
                elif truly_invalid and not flagged_invalid:
                    em.fn += 1
                else:
                    em.tn += 1

                if variant.label_first_error:
                    em.fe_labeled += 1
                    exact, within1 = first_error_match(
                        verdict.first_error_step, variant.label_first_error)
                    em.fe_exact += exact
                    em.fe_within1 += within1

                if variant.label_error_type not in ("", "NONE"):
                    em.type_labeled += 1
                    pred = verdict.error_type.value
                    if not verdict.process_valid and pred != "NONE":
                        em.type_confusion[(variant.label_error_type, pred)] += 1
                        if pred == variant.label_error_type:
                            em.type_correct += 1

                if variant.label_ac_but_invalid:
                    if flagged_invalid:
                        em.abi_tp += 1
                    else:
                        em.abi_fn += 1
                elif (variant.label_process_valid is True and flagged_invalid
                        and variant.label_exec_status == "AC"):
                    em.abi_fp += 1

                records.append({
                    "problem": case.problem_id, "variant": variant.name,
                    "difficulty": case.difficulty,
                    "labels": {
                        "exec_status": variant.label_exec_status,
                        "process_valid": variant.label_process_valid,
                        "first_error": variant.label_first_error,
                        "error_type": variant.label_error_type,
                        "ac_but_invalid": variant.label_ac_but_invalid,
                    },
                    "predicted": {
                        "process_valid": verdict.process_valid,
                        "first_error": verdict.first_error_step,
                        "error_type": verdict.error_type.value,
                        "confidence": verdict.confidence,
                    },
                })
                if write_reports:
                    write_text(
                        out_dir / "evaluator" / f"{case.problem_id}__{variant.name}.md",
                        render_markdown(AuditReport(
                            problem_id=f"{case.problem_id}/{variant.name}",
                            mode="evaluate", process=verdict,
                            quadrant_key="", quadrant="", created_at="",
                        )),
                    )
        write_text(out_dir / "evaluator" / "summary.json",
                   json.dumps(em.to_dict(), ensure_ascii=False, indent=2))
        return em

    # ------------------------------------------------------------- helpers

    def _write_report(self, rdir: Path, report: AuditReport) -> None:
        from ..report import render_html
        write_text(rdir / "report.md", render_markdown(report))
        write_text(rdir / "report.html", render_html(report))

    @staticmethod
    def _case_record(case: ProblemCase, report: AuditReport) -> dict:
        return {
            "problem_id": case.problem_id,
            "difficulty": case.difficulty,
            "tags": case.tags,
            "exec_status": report.execution.status.value if report.execution else None,
            "process_valid": report.process.process_valid,
            "error_type": report.process.error_type.value,
            "first_error": report.process.first_error_step,
            "quadrant": report.quadrant_key,
            "repair_rounds": report.repair_rounds,
        }

    @staticmethod
    def _difficulty_breakdown(records: list[dict]) -> dict:
        """Rating-bucketed capability curves (spec section 18)."""
        buckets: dict[str, list[dict]] = defaultdict(list)
        for r in records:
            d = r.get("difficulty")
            if d is None:
                key = "unknown"
            else:
                key = f"{(d // 400) * 400}-{(d // 400) * 400 + 399}"
            buckets[key].append(r)
        out = {}
        for key, rs in sorted(buckets.items()):
            n = len(rs)
            out[key] = {
                "n": n,
                "pass_rate": round(sum(1 for r in rs if r["exec_status"] == "AC") / n, 4),
                "process_valid_rate": round(
                    sum(1 for r in rs if r["process_valid"]) / n, 4),
                "dual_correct_rate": round(sum(1 for r in rs if r["quadrant"] == "full_correct") / n, 4),
            }
        return out

    @staticmethod
    def _tag_breakdown(records: list[dict]) -> dict:
        """Algorithm-tag x error-type distribution (spec section 19)."""
        tag_stats: dict[str, dict] = defaultdict(lambda: {
            "n": 0, "pass": 0, "error_types": Counter()})
        for r in records:
            for tag in r.get("tags", []):
                s = tag_stats[tag]
                s["n"] += 1
                if r["exec_status"] == "AC":
                    s["pass"] += 1
                if r["error_type"] and r["error_type"] != "NONE":
                    s["error_types"][r["error_type"]] += 1
        return {tag: {"n": s["n"],
                      "pass_rate": round(s["pass"] / s["n"], 4) if s["n"] else 0,
                      "error_types": dict(s["error_types"])}
                for tag, s in sorted(tag_stats.items())}
