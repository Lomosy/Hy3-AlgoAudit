"""First-error-driven targeted repair (spec section 6.3) and the
execution-only repair baseline used for ablation comparisons."""
from __future__ import annotations

from ..llm import Budget, LLMClient
from ..schemas import ExecutionResult, ProcessVerdict, StructuredSolution
from ..utils import LLMOutputError, extract_json
from . import prompts
from .solver import Solver


class RepairAgent:
    def __init__(self, llm: LLMClient, solver: Solver | None = None):
        self.llm = llm
        self._solver = solver or Solver(llm)

    # ---------------------------------------------------------------- shared

    def _parse_via_solver(self, raw: str) -> StructuredSolution:
        # reuse the solver's tolerant parser
        return self._solver.parse(raw)

    def _repair(self, system: str, user: str, budget: Budget,
                max_retries: int = 1) -> StructuredSolution:
        last_err: Exception | None = None
        for _ in range(max_retries + 1):
            raw = self.llm.complete(system, user, budget=budget)
            try:
                return self._parse_via_solver(raw)
            except LLMOutputError as e:
                last_err = e
        raise LLMOutputError(f"repair output unparseable: {last_err}")

    # ------------------------------------------------------ process-guided

    def repair(self, problem_text: str, solution: StructuredSolution,
               verdict: ProcessVerdict, exec_result: ExecutionResult | None,
               budget: Budget = Budget.HIGH) -> StructuredSolution:
        """Targeted repair: keep the verified-correct prefix, rewrite from the
        first error step onward; scope of change follows the error type."""
        evidence_txt = "\n".join(
            f"- [{e.kind.value}] {e.step_id or '-'} {e.error_type.value}: {e.detail or e.claim}"
            for e in verdict.evidence[:8]
        ) or "无"

        exec_detail = ""
        if exec_result is not None:
            if exec_result.failed_index is not None:
                exec_detail = f"- 首个失败测试：#{exec_result.failed_index}（{exec_result.detail}）"
            elif exec_result.detail:
                exec_detail = f"- 详情：{exec_result.detail}"

        user = prompts.REPAIR_GUIDED_USER.format(
            problem=problem_text,
            solution=solution.model_dump_json(indent=2),
            process_valid="是" if verdict.process_valid else "否",
            first_error=verdict.first_error_step or "无",
            error_type=verdict.error_type.value,
            error_label=verdict.error_type.label,
            evidence=evidence_txt,
            exec_status=exec_result.status.value if exec_result else "未执行",
            exec_detail=exec_detail,
        )
        return self._repair(prompts.REPAIR_GUIDED_SYSTEM, user, budget)

    # ------------------------------------------------------ execution-only

    def repair_execution_only(self, problem_text: str, solution: StructuredSolution,
                              exec_result: ExecutionResult,
                              budget: Budget = Budget.LOW) -> StructuredSolution:
        """Baseline: re-given only the WA/TLE/RE feedback, no process info."""
        failed = exec_result.failed_index
        user = prompts.REPAIR_EXEC_ONLY_USER.format(
            problem=problem_text,
            solution=solution.model_dump_json(indent=2),
            exec_status=exec_result.status.value,
            failed_index=f"#{failed}" if failed is not None else "无",
            exec_detail=exec_result.detail or "无",
        )
        return self._repair(prompts.REPAIR_EXEC_ONLY_SYSTEM, user, budget)
