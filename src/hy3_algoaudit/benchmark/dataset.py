"""ProcessEval-CP dataset loading.

Expected layout per problem directory:
    <problem>/
      problem.md        - full statement
      meta.json         - {"id": ..., "difficulty": <codeforces rating>,
                           "tags": [...], "comparison": "token"|"float"}
      tests.json        - [{"input": "...", "expected": "..."}, ...]
      reference.py|.cpp - reference solution (optional but recommended)
      variants.json     - candidate processes (optional, **no labels**)

Labels live outside the public tree, under the private root:
    <private>/<problem>/
      variants.json     - candidates whose *names* would leak the answer
                          (error-injection / AC-but-invalid)
      labels.json       - {"labels": {"<variant name>": {...}}, ...}

variants.json entry format:
    {"name": "correct", "language": "python", "code": "...",
     "steps": {"S1": "...", ...}}          # optional

labels.json entry format:
    {"exec_status": "AC", "process_valid": true,
     "first_error": null, "error_type": "NONE", "ac_but_invalid": false}

`load_dataset(root)` reads only the public tree — anyone can run the benchmark,
but nobody can grade it. `load_dataset(root, private_root=...)` merges the private
variants and the ground-truth labels, which is what offline evaluation needs.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..schemas import CodeBlock, StepContent, StructuredSolution, TestCase
from ..utils import read_text


@dataclass
class Variant:
    name: str
    solution: StructuredSolution
    kind: str = ""
    label_exec_status: str = ""
    label_process_valid: bool | None = None
    label_first_error: str | None = None
    label_error_type: str = "NONE"
    label_ac_but_invalid: bool = False

    @property
    def labeled(self) -> bool:
        """Whether this variant carries a ground-truth process label."""
        return self.label_process_valid is not None


@dataclass
class ProblemCase:
    problem_id: str
    problem_text: str
    tests: list[TestCase]
    reference: CodeBlock | None = None
    difficulty: int | None = None
    tags: list[str] = field(default_factory=list)
    #: Per-problem output comparison mode ("token" | "float"). This is the mode the
    #: reference solution was verified with offline; judging with anything else can
    #: flip a genuinely AC solution to WA (float-printing problems).
    comparison: str = "token"
    #: Per-problem judge limits from meta.json (None -> global settings).
    time_limit_seconds: float | None = None
    memory_limit_mb: int | None = None
    variants: list[Variant] = field(default_factory=list)


def _load_tests(pdir: Path) -> list[TestCase]:
    tests_file = pdir / "tests.json"
    tests: list[TestCase] = []
    if tests_file.exists():
        data = json.loads(read_text(tests_file))
        for t in data:
            tests.append(TestCase(input=t.get("input", ""),
                                  expected=t.get("expected", "")))
        return tests
    # fallback: tests/ directory with input1.txt / expected1.txt ...
    tdir = pdir / "tests"
    if tdir.is_dir():
        i = 1
        while (tdir / f"input{i}.txt").exists():
            tests.append(TestCase(
                input=read_text(tdir / f"input{i}.txt"),
                expected=read_text(tdir / f"expected{i}.txt"),
            ))
            i += 1
    return tests


def _load_reference(pdir: Path) -> CodeBlock | None:
    for name, lang in (("reference.py", "python"), ("reference.cpp", "cpp")):
        f = pdir / name
        if f.exists():
            return CodeBlock(language=lang, code=read_text(f))
    return None


def _load_variants(pdir: Path, private_pdir: Path | None = None) -> list[Variant]:
    """Load public variants, and (when a private dir is given) private ones + labels."""
    sources = [pdir / "variants.json"]
    if private_pdir is not None:
        sources.append(private_pdir / "variants.json")

    labels = _load_labels(private_pdir)
    out: list[Variant] = []
    for path in sources:
        if not path.exists():
            continue
        for v in json.loads(read_text(path)):
            name = v.get("name", "unnamed")
            steps = [{"step_id": sid, "title": "", "content": content}
                     for sid, content in (v.get("steps") or {}).items()]
            # inline label (demo files) first, external ground truth wins
            label = {**(v.get("label") or {}), **(labels.get(name) or {})}
            out.append(Variant(
                name=name,
                solution=StructuredSolution(
                    problem_id=name,
                    steps=steps,
                    code=CodeBlock(language=v.get("language", "python"),
                                   code=v.get("code", "")),
                ),
                kind=label.get("kind", ""),
                label_exec_status=label.get("exec_status", ""),
                label_process_valid=label.get("process_valid"),
                label_first_error=label.get("first_error"),
                label_error_type=label.get("error_type", "NONE"),
                label_ac_but_invalid=bool(label.get("ac_but_invalid", False)),
            ))
    return out


def _load_labels(private_pdir: Path | None) -> dict[str, dict]:
    if private_pdir is None:
        return {}
    f = private_pdir / "labels.json"
    if not f.exists():
        return {}
    return json.loads(read_text(f)).get("labels") or {}


def load_problem(pdir: str | Path, private_pdir: str | Path | None = None) -> ProblemCase:
    pdir = Path(pdir)
    priv = Path(private_pdir) if private_pdir is not None else None
    meta = {}
    meta_file = pdir / "meta.json"
    if meta_file.exists():
        meta = json.loads(read_text(meta_file))
    return ProblemCase(
        problem_id=meta.get("id", pdir.name),
        problem_text=read_text(pdir / "problem.md"),
        tests=_load_tests(pdir),
        reference=_load_reference(pdir),
        difficulty=meta.get("difficulty"),
        tags=meta.get("tags", []),
        comparison=meta.get("comparison", "token"),
        time_limit_seconds=meta.get("time_limit_seconds"),
        memory_limit_mb=(round(meta["memory_limit_bytes"] / (1024 * 1024))
                         if meta.get("memory_limit_bytes") else None),
        variants=_load_variants(pdir, priv),
    )


def load_dataset(root: str | Path, private_root: str | Path | None = None) -> list[ProblemCase]:
    """Load every problem under `root` (one directory per problem).

    private_root: optional private tree mirroring the same <pid>/ layout. When
    given, its variants.json and labels.json are merged in — that is the only
    way to obtain ground-truth process labels.
    """
    root = Path(root)
    priv = Path(private_root) if private_root is not None else None

    def _merge(child: Path) -> ProblemCase:
        return load_problem(child, (priv / child.name) if priv is not None else None)

    if (root / "problem.md").exists():
        return [_merge(root)]
    cases: list[ProblemCase] = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / "problem.md").exists():
            cases.append(_merge(child))
    return cases
