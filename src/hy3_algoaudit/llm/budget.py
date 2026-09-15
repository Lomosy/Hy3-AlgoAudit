"""Evidence-triggered adaptive reasoning budget (spec sections 12 / 20.4).

Strategy:
    rule-solvable            -> no model call (handled upstream)
    ordinary semantic checks -> LOW budget
    hard / low-confidence / conflicting evidence / repair retry -> HIGH budget
"""
from __future__ import annotations

from .client import Budget


def decide_budget(
    *,
    difficulty_rating: int | None = None,
    high_threshold: int = 2000,
    conflict: bool = False,
    low_confidence: bool = False,
    failed_repair_rounds: int = 0,
    phase: str = "verify",
) -> Budget:
    """Decide the reasoning budget for one model call.

    phase: "solve" | "verify" | "repair"
    conflict: sandbox result and process judgment disagree, or critics disagree.
    """
    if failed_repair_rounds > 0:
        return Budget.HIGH
    if conflict or low_confidence:
        return Budget.HIGH
    if difficulty_rating is not None and difficulty_rating >= high_threshold:
        return Budget.HIGH
    # Phase-based default: first-pass generation leans higher, verification low.
    return Budget.HIGH if phase == "solve" else Budget.LOW
