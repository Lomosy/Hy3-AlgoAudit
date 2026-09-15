"""Dataset layout tests: public tree vs private labels tree."""
import json
from pathlib import Path

from hy3_algoaudit.benchmark.dataset import load_dataset

PUBLIC_META = {
    "id": "p1",
    "name": "Floating Point Sum",
    "difficulty": 1700,
    "tags": ["math"],
    "comparison": "float",
    "time_limit_seconds": 1.0,
}

CORRECT_STEPS = {f"S{i}": f"step {i} reasoning" for i in range(1, 8)}
INJECTED_STEPS = dict(CORRECT_STEPS, S3="wrong algorithm here", S4="follows the wrong idea")


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    path.write_text(text, encoding="utf-8")


def _build(tmp_path: Path) -> tuple[Path, Path]:
    pub = tmp_path / "data" / "processeval-cp"
    priv = tmp_path / "private" / "processeval-cp"

    _write(pub / "p1" / "problem.md", "Sum n floats.\n")
    _write(pub / "p1" / "meta.json", PUBLIC_META)
    _write(pub / "p1" / "tests.json", [{"input": "2\n0.5\n0.25\n", "expected": "0.75"}])
    _write(pub / "p1" / "reference.py", "print(0.75)\n")
    # public variants: no labels at all
    _write(pub / "p1" / "variants.json", [
        {"name": "correct", "language": "python", "code": "print(0.75)",
         "steps": CORRECT_STEPS},
    ])
    # private variants: names leak the answer, so they live outside the public tree
    _write(priv / "p1" / "variants.json", [
        {"name": "injected-E2-S3", "language": "python",
         "code": "print(0.5)", "steps": INJECTED_STEPS},
    ])
    _write(priv / "p1" / "labels.json", {
        "pid": "p1",
        "labels": {
            "correct": {"kind": "correct", "exec_status": "AC", "process_valid": True,
                        "first_error": None, "error_type": "NONE",
                        "ac_but_invalid": False},
            "injected-E2-S3": {"kind": "injected", "exec_status": "WA",
                               "process_valid": False, "first_error": "S3",
                               "error_type": "E2", "ac_but_invalid": False},
        },
    })
    (pub / "manifest.json").write_text("{}", encoding="utf-8")
    return pub, priv


def test_public_only_has_no_labels(tmp_path):
    pub, _priv = _build(tmp_path)
    cases = load_dataset(pub)
    assert len(cases) == 1  # manifest.json must not be mistaken for a problem
    case = cases[0]
    assert case.problem_id == "p1"
    assert case.difficulty == 1700
    assert case.tags == ["math"]
    assert case.comparison == "float"
    assert case.reference is not None
    assert len(case.tests) == 1
    # only the public variant, and it carries no ground truth
    assert [v.name for v in case.variants] == ["correct"]
    assert case.variants[0].label_process_valid is None
    assert not case.variants[0].labeled


def test_private_root_merges_variants_and_labels(tmp_path):
    pub, priv = _build(tmp_path)
    cases = load_dataset(pub, private_root=priv)
    variants = {v.name: v for v in cases[0].variants}
    assert set(variants) == {"correct", "injected-E2-S3"}

    injected = variants["injected-E2-S3"]
    assert injected.kind == "injected"
    assert injected.labeled
    assert injected.label_exec_status == "WA"
    assert injected.label_process_valid is False
    assert injected.label_first_error == "S3"
    assert injected.label_error_type == "E2"
    assert injected.label_ac_but_invalid is False
    # steps survive the mapping -> StructuredSolution conversion
    assert injected.solution.step("S3").content == "wrong algorithm here"
    assert injected.solution.step("S1").content == "step 1 reasoning"
    assert injected.solution.code.code == "print(0.5)"

    correct = variants["correct"]
    assert correct.label_process_valid is True
    assert not correct.label_ac_but_invalid


def test_missing_private_dir_is_tolerated(tmp_path):
    pub, _priv = _build(tmp_path)
    cases = load_dataset(pub, private_root=tmp_path / "does-not-exist")
    assert len(cases[0].variants) == 1  # public variant only, no crash
