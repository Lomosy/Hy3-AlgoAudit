"""Metrics for solver and evaluator capability (spec sections 16-19).

All functions are pure — trivially unit-testable.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from ..schemas import ErrorType, STEP_ORDER, step_rank


def _prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    p = tp / (tp + fp) if (tp + fp) else 0.0
    r = tp / (tp + fn) if (tp + fn) else 0.0
    f = 2 * p * r / (p + r) if (p + r) else 0.0
    return round(p, 4), round(r, 4), round(f, 4)


# ---------------------------------------------------------------- solver

@dataclass
class SolverMetrics:
    n: int = 0
    passed: int = 0                # AC count
    process_valid: int = 0
    dual_correct: int = 0
    refine_success: Counter = field(default_factory=Counter)  # round -> cumulative

    @property
    def pass_at_1(self) -> float:
        return round(self.passed / self.n, 4) if self.n else 0.0

    @property
    def process_valid_rate(self) -> float:
        return round(self.process_valid / self.n, 4) if self.n else 0.0

    @property
    def dual_correct_rate(self) -> float:
        return round(self.dual_correct / self.n, 4) if self.n else 0.0

    def refine_at(self, k: int) -> float:
        """Fraction of problems solved within <= k repair rounds."""
        done = sum(c for r, c in self.refine_success.items() if r <= k)
        return round(done / self.n, 4) if self.n else 0.0


# ------------------------------------------------------------- evaluator

@dataclass
class EvaluatorMetrics:
    """Metrics over labeled candidate processes (variants)."""
    n: int = 0
    tp: int = 0   # truly invalid, flagged invalid
    fp: int = 0   # truly valid, flagged invalid  (false positives)
    fn: int = 0   # truly invalid, missed
    tn: int = 0
    fe_exact: int = 0
    fe_within1: int = 0
    fe_labeled: int = 0           # variants that carry a first-error label
    type_correct: int = 0
    type_labeled: int = 0
    type_confusion: Counter = field(default_factory=Counter)  # (true, pred)
    # AC-but-Invalid detection
    abi_tp: int = 0
    abi_fp: int = 0
    abi_fn: int = 0

    @property
    def process_p(self) -> float: return _prf(self.tp, self.fp, self.fn)[0]
    @property
    def process_r(self) -> float: return _prf(self.tp, self.fp, self.fn)[1]
    @property
    def process_f1(self) -> float: return _prf(self.tp, self.fp, self.fn)[2]

    @property
    def fpr(self) -> float:
        total_valid = self.fp + self.tn
        return round(self.fp / total_valid, 4) if total_valid else 0.0

    @property
    def first_error_exact_acc(self) -> float:
        return round(self.fe_exact / self.fe_labeled, 4) if self.fe_labeled else 0.0

    @property
    def first_error_pm1_acc(self) -> float:
        return round(self.fe_within1 / self.fe_labeled, 4) if self.fe_labeled else 0.0

    @property
    def error_type_acc(self) -> float:
        return round(self.type_correct / self.type_labeled, 4) if self.type_labeled else 0.0

    def error_type_macro_f1(self) -> float:
        """Macro-F1 over E1-E8 (predicted types counted per true type)."""
        f1s = []
        for et in ErrorType:
            if et is ErrorType.NONE:
                continue
            tp = self.type_confusion.get((et.value, et.value), 0)
            fp = sum(c for (t, p), c in self.type_confusion.items()
                     if p == et.value and t != et.value)
            fn = sum(c for (t, p), c in self.type_confusion.items()
                     if t == et.value and p != et.value)
            if tp + fp + fn == 0:
                continue
            _, _, f1 = _prf(tp, fp, fn)
            f1s.append(f1)
        return round(sum(f1s) / len(f1s), 4) if f1s else 0.0

    @property
    def abi_precision(self) -> float:
        return _prf(self.abi_tp, self.abi_fp, self.abi_fn)[0]

    @property
    def abi_recall(self) -> float:
        return _prf(self.abi_tp, self.abi_fp, self.abi_fn)[1]

    def to_dict(self) -> dict:
        return {
            "n": self.n,
            "process": {"precision": self.process_p, "recall": self.process_r,
                        "f1": self.process_f1, "false_positive_rate": self.fpr},
            "first_error": {"exact_accuracy": self.first_error_exact_acc,
                            "pm1_accuracy": self.first_error_pm1_acc,
                            "labeled": self.fe_labeled},
            "error_type": {"accuracy": self.error_type_acc,
                           "macro_f1": self.error_type_macro_f1(),
                           "confusion": {f"{t}->{p}": c
                                         for (t, p), c in self.type_confusion.items()}},
            "ac_but_invalid": {"precision": self.abi_precision,
                               "recall": self.abi_recall},
        }


def first_error_match(predicted: str | None, labeled: str | None) -> tuple[bool, bool]:
    """Returns (exact_match, within_pm1_steps)."""
    if not labeled:
        return False, False
    if not predicted:
        return False, False
    exact = predicted == labeled
    diff = abs(step_rank(predicted) - step_rank(labeled))
    return exact, diff <= 1
