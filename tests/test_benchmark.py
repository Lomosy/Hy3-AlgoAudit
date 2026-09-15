"""Dataset loading + benchmark runner dry-run tests (mock LLM)."""
import json

from hy3_algoaudit.benchmark.dataset import load_dataset
from hy3_algoaudit.benchmark.runner import BenchmarkRunner
from hy3_algoaudit.config import SandboxLimits, Settings
from hy3_algoaudit.demo_data import build_mock_routes
from hy3_algoaudit.evaluator import ProcessEvaluator
from hy3_algoaudit.llm import MockLLMClient
from hy3_algoaudit.sandbox import LocalSandbox
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "demo" / "samples"
LIMITS = SandboxLimits(time_limit_seconds=5, memory_limit_mb=256)


def _settings():
    return Settings(api_key="mock", base_url="http://mock", model="mock",
                    limits=LIMITS, max_repair_rounds=1)


def test_dataset_loading():
    cases = load_dataset(ROOT)
    assert len(cases) == 2
    ms = next(c for c in cases if c.problem_id == "max_subarray")
    assert len(ms.tests) == 4
    assert ms.reference is not None
    assert ms.difficulty == 1200
    assert len(ms.variants) == 4
    abi = next(v for v in ms.variants if v.label_ac_but_invalid)
    assert abi.label_first_error == "S5"
    assert abi.label_error_type == "E5"
    assert abi.solution.code.language == "python"


def test_evaluator_run_on_variants(tmp_path):
    llm = MockLLMClient(build_mock_routes())
    runner = BenchmarkRunner(llm, _settings(), LocalSandbox(LIMITS))
    cases = load_dataset(ROOT)
    em = runner.run_evaluator(cases, tmp_path)
    assert em.n == 4
    assert em.fe_labeled == 3  # the "correct" variant carries no first-error label
    assert (tmp_path / "evaluator" / "summary.json").exists()
    summary = json.loads((tmp_path / "evaluator" / "summary.json").read_text("utf-8"))
    assert "process" in summary and "first_error" in summary


def test_solver_run_on_dataset(tmp_path):
    llm = MockLLMClient(build_mock_routes())
    runner = BenchmarkRunner(llm, _settings(), LocalSandbox(LIMITS))
    cases = load_dataset(ROOT)
    sm = runner.run_solver(cases, tmp_path, max_repair=1)
    assert sm.n == 2
    assert sm.passed >= 1  # mock Kadane solves max_subarray; a_plus_b may fail
    assert (tmp_path / "solver" / "summary.json").exists()
