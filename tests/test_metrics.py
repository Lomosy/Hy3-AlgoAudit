"""Metrics unit tests."""
from hy3_algoaudit.benchmark.metrics import (EvaluatorMetrics, SolverMetrics,
                                             first_error_match)


def test_solver_metrics():
    sm = SolverMetrics(n=4, passed=2, process_valid=3, dual_correct=2)
    sm.refine_success[1] = 1
    sm.refine_success[2] = 1
    assert sm.pass_at_1 == 0.5
    assert sm.process_valid_rate == 0.75
    assert sm.dual_correct_rate == 0.5
    assert sm.refine_at(1) == 0.25
    assert sm.refine_at(2) == 0.5


def test_evaluator_metrics_prf():
    em = EvaluatorMetrics(tp=8, fp=2, fn=2, tn=8)
    assert em.process_p == 0.8
    assert em.process_r == 0.8
    assert em.process_f1 == 0.8
    assert em.fpr == 0.2


def test_macro_f1():
    em = EvaluatorMetrics()
    em.type_confusion.update({("E2", "E2"): 4, ("E3", "E3"): 2,
                              ("E3", "E7"): 2})
    # E2: P=R=F1=1 ; E3: P=1, R=0.5, F1=2/3 ; E7: 2 false positives -> F1=0
    # macro = (1 + 2/3 + 0) / 3 = 0.5556
    assert abs(em.error_type_macro_f1() - 0.5556) < 1e-3


def test_ac_but_invalid_pr():
    em = EvaluatorMetrics(abi_tp=4, abi_fp=1, abi_fn=2)
    assert em.abi_precision == 0.8
    assert em.abi_recall == round(4 / 6, 4)


def test_first_error_match():
    assert first_error_match("S2", "S2") == (True, True)
    assert first_error_match("S3", "S2") == (False, True)   # within +-1
    assert first_error_match("S5", "S2") == (False, False)
    assert first_error_match(None, "S2") == (False, False)
    assert first_error_match("S2", None) == (False, False)
