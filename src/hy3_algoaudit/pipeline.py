"""End-to-end pipelines.

solve_mode:    generate -> sandbox judge -> process audit -> first-error-guided
               repair loop -> re-verify  ("generate-evaluate-repair-re-evaluate")
evaluate_mode: judge + audit a user-provided solution, no repair.
"""
from __future__ import annotations

import logging

from .config import SandboxLimits, Settings
from .evaluator import ProcessEvaluator
from .evaluator.fusion import compute_quadrant
from .llm import Budget, LLMClient, decide_budget
from .sandbox.base import Sandbox
from .sandbox.judge import judge_program
from .schemas import (AuditReport, CodeBlock, ExecutionResult,
                      StructuredSolution, TestCase)
from .solver import RepairAgent, Solver
from .utils import now_iso

logger = logging.getLogger(__name__)


class AlgoAuditPipeline:
    def __init__(self, llm: LLMClient, settings: Settings,
                 sandbox: Sandbox | None = None,
                 evaluator: ProcessEvaluator | None = None):
        self.llm = llm
        self.settings = settings
        self.sandbox = sandbox
        self.solver = Solver(llm)
        self.repairer = RepairAgent(llm, self.solver)
        self.evaluator = evaluator or ProcessEvaluator(llm, settings, sandbox)

    # ------------------------------------------------------------------ util

    def _run_judge(self, code: CodeBlock,
                   tests: list[TestCase] | None,
                   comparison: str = "token",
                   limits: SandboxLimits | None = None) -> ExecutionResult | None:
        if not tests or self.sandbox is None:
            return None
        # comparison comes from the dataset's per-problem meta: the very mode the
        # reference solution was verified with offline. Judging float-output
        # problems with "token" would flip genuine ACs to WA.
        return judge_program(self.sandbox, code, tests,
                             limits or self.settings.limits,
                             comparison=comparison)

    def _audit(self, mode: str, problem_id: str, problem_text: str,
               solution, exec_result, difficulty_rating: int | None,
               reference_code: CodeBlock | None,
               limits: SandboxLimits | None = None) -> AuditReport:
        verdict = self.evaluator.evaluate(
            problem_text, solution, exec_result=exec_result,
            reference_code=reference_code, difficulty_rating=difficulty_rating,
            limits=limits,
        )
        qkey, qtext = compute_quadrant(exec_result, verdict)
        return AuditReport(
            problem_id=problem_id,
            mode=mode,
            created_at=now_iso(),
            execution=exec_result,
            process=verdict,
            quadrant_key=qkey,
            quadrant=qtext,
            final_solution=solution,
            tokens_used=self.llm.usage.total_tokens,
        )

    # ------------------------------------------------------------- solve mode

    def solve_mode(
        self,
        problem_text: str,
        problem_id: str = "",
        tests: list[TestCase] | None = None,
        reference_code: CodeBlock | None = None,
        difficulty_rating: int | None = None,
        max_repair: int | None = None,
        comparison: str = "token",
        limits: SandboxLimits | None = None,
    ) -> AuditReport:
        """AI auto-solve with the full repair loop."""
        max_repair = self.settings.max_repair_rounds if max_repair is None else max_repair
        budget = decide_budget(difficulty_rating=difficulty_rating,
                               high_threshold=self.settings.difficulty_high_threshold,
                               phase="solve")
        solution = self.solver.solve(problem_text, budget=budget)
        exec_result = self._run_judge(solution.code, tests, comparison, limits)

        notes: list[str] = []
        rounds = 0
        while rounds < max_repair:
            verdict = self.evaluator.evaluate(
                problem_text, solution, exec_result=exec_result,
                reference_code=reference_code, difficulty_rating=difficulty_rating,
                limits=limits,
            )
            if exec_result is not None and exec_result.accepted and verdict.process_valid:
                break  # dual correct — done
            rounds += 1
            conflict = (exec_result is not None and exec_result.accepted
                        and not verdict.process_valid)
            budget = decide_budget(
                difficulty_rating=difficulty_rating,
                high_threshold=self.settings.difficulty_high_threshold,
                conflict=conflict, failed_repair_rounds=rounds, phase="repair",
            )
            logger.info("repair round %d: first_error=%s type=%s exec=%s",
                        rounds, verdict.first_error_step,
                        verdict.error_type.value,
                        exec_result.status.value if exec_result else "N/A")
            solution = self.repairer.repair(problem_text, solution, verdict,
                                            exec_result, budget=budget)
            exec_result = self._run_judge(solution.code, tests, comparison, limits)
            notes.append(f"第 {rounds} 轮定向修复："
                         f"首错 {verdict.first_error_step or '-'} "
                         f"({verdict.error_type.value}) -> "
                         f"执行 {exec_result.status.value if exec_result else '未执行'}")

        verdict = self.evaluator.evaluate(
            problem_text, solution, exec_result=exec_result,
            reference_code=reference_code, difficulty_rating=difficulty_rating,
            limits=limits,
        )
        qkey, qtext = compute_quadrant(exec_result, verdict)
        return AuditReport(
            problem_id=problem_id,
            mode="solve",
            created_at=now_iso(),
            execution=exec_result,
            process=verdict,
            quadrant_key=qkey,
            quadrant=qtext,
            final_solution=solution,
            repair_rounds=rounds,
            tokens_used=self.llm.usage.total_tokens,
            notes=notes,
        )

    # ----------------------------------------------------------- evaluate mode

    def evaluate_mode(
        self,
        problem_text: str,
        solution,
        problem_id: str = "",
        tests: list[TestCase] | None = None,
        reference_code: CodeBlock | None = None,
        difficulty_rating: int | None = None,
        comparison: str = "token",
        limits: SandboxLimits | None = None,
    ) -> AuditReport:
        """User-solution assessment: result verdict + process audit in one."""
        if not isinstance(solution, StructuredSolution):
            solution = StructuredSolution.model_validate(solution)
        exec_result = self._run_judge(solution.code, tests, comparison, limits)
        return self._audit("evaluate", problem_id, problem_text, solution,
                           exec_result, difficulty_rating, reference_code,
                           limits=limits)
