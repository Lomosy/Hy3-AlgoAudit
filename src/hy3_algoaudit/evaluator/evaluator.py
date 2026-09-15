"""ProcessEvaluator orchestrator: multi-layer evaluation pipeline.

Layer 1  rule & structure checks      (no model)
Layer 2  sandbox execution evidence   (injected by the pipeline)
Layer 3  step-level semantic review   (two-phase, adaptive budget)
+        explanation-code consistency check
+        claim-conditioned counterexample verification
=>       evidence fusion -> ProcessVerdict
"""
from __future__ import annotations

import logging

from ..config import SandboxLimits, Settings
from ..llm import Budget, LLMClient, decide_budget
from ..schemas import (CodeBlock, ExecutionResult, Evidence, EvidenceKind,
                       ProcessVerdict, STEP_ORDER, StepVerdict,
                       StructuredSolution)
from ..sandbox.base import Sandbox
from . import prompts  # noqa: F401  (kept for external prompt customization)
from .consistency import ConsistencyChecker
from .counterexample import CounterexampleSeeker
from .fusion import fuse
from .step_verifier import StepVerifier
from .structural import structural_check

logger = logging.getLogger(__name__)


class ProcessEvaluator:
    """Ablation switches (spec section 21):
    enable_structural / enable_fine_verify / enable_consistency /
    enable_counterexample / adaptive budget on-off.
    """

    def __init__(self, llm: LLMClient, settings: Settings,
                 sandbox: Sandbox | None = None,
                 *, enable_structural: bool = True,
                 enable_consistency: bool = True,
                 enable_counterexample: bool = True,
                 enable_fine_verify: bool = True,
                 adaptive_budget: bool = True):
        self.llm = llm
        self.settings = settings
        self.sandbox = sandbox
        self.enable_structural = enable_structural
        self.enable_consistency = enable_consistency
        self.enable_counterexample = enable_counterexample
        self.enable_fine_verify = enable_fine_verify
        self.adaptive_budget = adaptive_budget
        self.verifier = StepVerifier(llm)
        self.consistency = ConsistencyChecker(llm)
        self.cex_seeker = CounterexampleSeeker(llm, sandbox)

    # ------------------------------------------------------------------ API

    def evaluate(
        self,
        problem_text: str,
        solution: StructuredSolution,
        exec_result: ExecutionResult | None = None,
        reference_code: CodeBlock | None = None,
        difficulty_rating: int | None = None,
        limits: SandboxLimits | None = None,
    ) -> ProcessVerdict:
        evidence: list[Evidence] = []

        # -------- Layer 1: rules (deterministic, free) --------
        rule_ev: list[Evidence] = []
        hard_rule_failure = False
        if self.enable_structural:
            rule_ev = structural_check(solution)
            evidence.extend(rule_ev)
            hard_rule_failure = any(e.indicates_invalid for e in rule_ev)

        # -------- Layer 3a: quick scan (low budget) --------
        budget = decide_budget(
            difficulty_rating=difficulty_rating,
            high_threshold=self.settings.difficulty_high_threshold,
            conflict=False, low_confidence=False, phase="verify",
        ) if self.adaptive_budget else Budget.LOW
        try:
            step_verdicts = self.verifier.quick_scan(problem_text, solution,
                                                     budget=budget,
                                                     reference=reference_code)
        except Exception as e:
            # 单层失败不炸整体：按全部 unverifiable 继续，其余层证据照常融合
            logger.warning("quick scan failed: %s", e)
            step_verdicts = [StepVerdict(step_id=sid, status="unverifiable",
                                         confidence=0.0)
                             for sid in STEP_ORDER]
        suspicious = [v for v in step_verdicts if v.status == "suspicious"]

        # -------- Layer 3b: fine verification (high budget, suspicious +-1) --
        if self.enable_fine_verify and suspicious and not hard_rule_failure:
            targets = [v.step_id for v in suspicious]
            try:
                fine = self.verifier.fine_verify(problem_text, solution, targets,
                                                 reference=reference_code)
            except Exception as e:
                logger.warning("fine verify failed: %s", e)
                fine = []
            fine_by_id = {v.step_id: v for v in fine}
            merged = []
            for v in step_verdicts:
                if v.step_id in fine_by_id:
                    fv = fine_by_id[v.step_id]
                    # only escalate on confident incorrect; suspicious stays
                    merged.append(fv if fv.status in ("incorrect", "correct")
                                  and fv.confidence >= 0.6 else v)
                else:
                    merged.append(v)
            step_verdicts = merged

        # -------- explanation-code consistency --------
        if self.enable_consistency and not hard_rule_failure:
            try:
                evidence.append(self.consistency.check(problem_text, solution))
            except Exception as e:
                logger.warning("consistency check failed: %s", e)

        # -------- claim-conditioned counterexamples --------
        if self.enable_counterexample and self.sandbox is not None:
            claims: list[tuple[str, str, "object"]] = []
            for v in step_verdicts:
                if v.status in ("suspicious", "incorrect") and v.suspicious_claim:
                    claims.append((v.step_id, v.suspicious_claim, v.error_type))
            for e in rule_ev:
                if not e.indicates_invalid and e.claim:
                    claims.append((e.step_id or "S5", e.claim, e.error_type))
            if claims:
                try:
                    cex_ev = self.cex_seeker.seek(problem_text, solution, claims,
                                                  reference_code, limits=limits)
                    evidence.extend(cex_ev)
                except Exception as e:
                    logger.warning("counterexample seek failed: %s", e)

        return fuse(exec_result, step_verdicts, evidence)
