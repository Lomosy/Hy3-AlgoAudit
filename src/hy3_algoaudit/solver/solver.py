"""Structured S1-S7 solving agent."""
from __future__ import annotations

from ..llm import Budget, LLMClient
from ..schemas import CodeBlock, StepContent, StructuredSolution
from ..utils import LLMOutputError, extract_json
from . import prompts


class Solver:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    # ---------------------------------------------------------------- parse

    def parse(self, raw: str) -> StructuredSolution:
        data = extract_json(raw)
        steps_raw = data.get("steps")
        if not isinstance(steps_raw, list) or not steps_raw:
            raise LLMOutputError("solution JSON missing non-empty 'steps' list")
        steps = []
        for s in steps_raw:
            if not isinstance(s, dict):
                continue
            sid = str(s.get("step_id", "")).upper()
            if sid.startswith("S") and sid[1:].isdigit():
                sid = f"S{int(sid[1:])}"
            try:
                steps.append(StepContent(
                    step_id=sid,
                    title=str(s.get("title", "")),
                    content=str(s.get("content", "")),
                ))
            except Exception:
                continue  # drop malformed steps; structural checker will flag
        code_raw = data.get("code") or {}
        if isinstance(code_raw, str):
            code_raw = {"language": "python", "code": code_raw}
        code = CodeBlock(
            language=str(code_raw.get("language", "python")),
            code=str(code_raw.get("code", "")),
        )
        sol = StructuredSolution(
            problem_id=data.get("problem_id"),
            steps=steps,
            code=code,
        )
        # de-duplicate / order by S1..S7
        seen: dict[str, StepContent] = {}
        for s in sol.steps:
            seen[s.step_id] = s
        sol.steps = [seen[k] for k in sorted(seen, key=lambda x: int(x[1:]))]
        return sol

    # ---------------------------------------------------------------- solve

    def solve(self, problem_text: str, budget: Budget = Budget.HIGH,
              max_retries: int = 2) -> StructuredSolution:
        """Generate a structured solution; retry parse failures with a nudge."""
        system = prompts.SOLVER_SYSTEM
        user = prompts.SOLVER_USER.format(problem=problem_text)
        last_err: Exception | None = None
        for attempt in range(max_retries + 1):
            raw = self.llm.complete(system, user, budget=budget)
            try:
                return self.parse(raw)
            except LLMOutputError as e:
                last_err = e
                user = (prompts.SOLVER_USER.format(problem=problem_text)
                        + f"\n\n注意：你上一次的输出无法解析（{e}），"
                          "请严格只输出符合要求的 JSON。")
        raise LLMOutputError(f"solver failed to produce parseable JSON: {last_err}")
