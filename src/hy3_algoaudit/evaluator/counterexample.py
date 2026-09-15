"""Claim-conditioned Counterexample Verification (spec section 11).

Suspicious claim -> minimal counterexample input -> reference solution gives
ground-truth output -> candidate runs in the sandbox -> concrete evidence.
"""
from __future__ import annotations

import logging

from ..config import SandboxLimits
from ..llm import Budget, LLMClient
from ..schemas import (CodeBlock, ErrorType, Evidence, EvidenceKind,
                       StructuredSolution, TestCase)
from ..sandbox.base import Sandbox
from ..sandbox.judge import compare_output
from ..utils import extract_json
from . import prompts

logger = logging.getLogger(__name__)

MAX_CLAIMS = 2  # cap counterexample attempts per evaluation


class CounterexampleSeeker:
    def __init__(self, llm: LLMClient, sandbox: Sandbox | None = None):
        self.llm = llm
        self.sandbox = sandbox

    def seek(self, problem_text: str, solution: StructuredSolution,
             claims: list[tuple[str, str, ErrorType]],  # (step_id, claim, error_type)
             reference_code: CodeBlock | None = None,
             limits: SandboxLimits | None = None) -> list[Evidence]:
        if not claims:
            return []
        evidence: list[Evidence] = []
        for step_id, claim, err in claims[:MAX_CLAIMS]:
            ev = self._verify_claim(problem_text, solution, step_id, claim, err,
                                    reference_code, limits=limits)
            if ev is not None:
                evidence.append(ev)
        return evidence

    # ------------------------------------------------------------ internals

    def _verify_claim(self, problem_text: str, solution: StructuredSolution,
                      step_id: str, claim: str, err: ErrorType,
                      reference_code: CodeBlock | None,
                      limits: SandboxLimits | None = None) -> Evidence | None:
        raw = self.llm.complete(
            prompts.COUNTEREXAMPLE_SYSTEM,
            prompts.COUNTEREXAMPLE_USER.format(
                problem=problem_text, step_id=step_id, claim=claim,
                language=solution.code.language, code=solution.code.code,
            ),
            budget=Budget.HIGH,
        )
        data = extract_json(raw)
        if bool(data.get("claim_holds", True)):
            return None  # model says the claim actually holds — no evidence

        cex_input = str(data.get("input", ""))
        explanation = str(data.get("explanation", ""))
        if not cex_input.strip():
            return None

        # Ground truth: reference solution in sandbox (strongest); otherwise
        # the model's own expected output (weaker, strength reduced).
        expected = ""
        strength = 3
        source = "模型推理"
        ref_prog = None
        if self.sandbox is not None and reference_code is not None \
                and reference_code.code.strip():
            try:
                ref_prog = self.sandbox.prepare(reference_code)
                if ref_prog.compile_ok:
                    tl = limits.time_limit_seconds if limits else None
                    ml = limits.memory_limit_mb if limits else None
                    out = self.sandbox.run_once(ref_prog, cex_input, tl, ml)
                    if not out.runtime_error and not out.timed_out:
                        expected = out.stdout
                        source = "参考解法执行"
                        strength = 5
            except Exception as e:  # sandbox failure must not break evaluation
                logger.warning("reference execution failed: %s", e)
            finally:
                if ref_prog is not None:
                    self.sandbox.cleanup(ref_prog)
        if not expected:
            expected = str(data.get("expected_output", ""))

        # Run the candidate on the counterexample input.
        confirmed = False
        actual = ""
        if self.sandbox is not None and expected:
            try:
                prog = self.sandbox.prepare(solution.code)
                if prog.compile_ok:
                    tl = limits.time_limit_seconds if limits else None
                    ml = limits.memory_limit_mb if limits else None
                    out = self.sandbox.run_once(prog, cex_input, tl, ml)
                    actual = out.stdout
                    confirmed = (not out.timed_out and not out.mem_exceeded
                                 and not out.runtime_error
                                 and not compare_output(actual, expected))
            except Exception as e:
                logger.warning("candidate execution failed: %s", e)
            finally:
                if prog is not None:
                    self.sandbox.cleanup(prog)

        if not confirmed:
            # Even unconfirmed, a model-crafted counterexample is weak evidence.
            return Evidence(
                kind=EvidenceKind.counterexample, strength=2, step_id=step_id,
                error_type=err or ErrorType.E8, indicates_invalid=False,
                claim=claim,
                detail=f"构造了候选反例但未能执行确认（{source}）：{explanation}",
            )

        return Evidence(
            kind=EvidenceKind.counterexample, strength=strength, step_id=step_id,
            error_type=err or ErrorType.E8, indicates_invalid=True,
            claim=claim,
            detail=(f"反例已执行确认（{source}）。输入：{cex_input!r} | "
                    f"期望输出：{expected!r} | 实际输出：{actual!r} | {explanation}"),
        )
