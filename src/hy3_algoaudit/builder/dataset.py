"""评测集落盘：把入选题目写成公开可读的「每题一目录」。

目录布局（冰山理论）::

    公开 data/processeval-cp/
        manifest.json              全量清单（配置快照 + 各阶段统计 + 逐题索引）
        <pid>/problem.md           题面
        <pid>/meta.json            元数据（判题所需：难度、标签、时限、比对模式）
        <pid>/tests.json           测试用例 [{"input", "expected"}, ...]
        <pid>/reference.py         参考解（实测全 AC）
        <pid>/variants.json        公开候选（correct + natural，不含任何标注）

    私有 private/processeval-cp/（不入 git）
        manifest.json              标注统计
        <pid>/variants.json        私有候选（injected + ac_invalid，正文即答案）
        <pid>/labels.json          全部候选的 ground truth 标注
        <pid>/verification.json    参考解验证证据（逐用例哈希、耗时、比对模式）

这个布局直接对应 `hy3_algoaudit.benchmark.dataset.load_dataset()` 的读取契约：
一个目录 = 一道题，题面/元数据/用例/参考解/候选各自独立成文件。标注**不放在
variants.json 里**，而是外置到 private/ 下，因此公开仓库可以直接被第三方跑基准，
但拿不到答案。

三条写入铁律：
    1. 公开测试用例是**参考解验证用例集的前缀**，绝不写入未经验证的用例，
       否则公开用例的正确性只能靠数据集自身背书。
    2. 参考解取「实测全 AC」的那一条，并同时写入验证证据（逐用例哈希、
       耗时、用例集哈希），任何下游结论都能回溯到具体代码与具体用例集。
    3. 所有裁剪参数写进 manifest，保证同配置可复现。
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Iterable

from ..schemas import TestCase
from . import verify

MANIFEST_NAME = "manifest.json"

#: 每道题目录下的公开文件（variants.json 由 build_candidates.py 补写）
PROBLEM_FILES = ("problem.md", "meta.json", "tests.json", "reference.py", "variants.json")


# ---------------------------------------------------------------- 路径
def _base(root: str | Path | None) -> Path:
    return Path(root) if root else Path(".")


def dataset_root(cfg: dict[str, Any], root: str | Path | None = None) -> Path:
    """公开评测集根目录（每题一子目录）"""
    return _base(root) / cfg["paths"]["dataset_dir"]


def private_root(cfg: dict[str, Any], root: str | Path | None = None) -> Path:
    """私有材料根目录（标注、注入候选、验证证据）"""
    return _base(root) / cfg["paths"]["private_dir"]


def problem_dir(
    cfg: dict[str, Any], pid: str, *, root: str | Path | None = None, private: bool = False
) -> Path:
    base = private_root(cfg, root) if private else dataset_root(cfg, root)
    return base / pid


# ---------------------------------------------------------------- 哈希工具
def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def cases_digest(cases: Iterable[TestCase]) -> str:
    digest = hashlib.sha256()
    for case in cases:
        digest.update(case.input.encode("utf-8"))
        digest.update(b"\x1e")
        digest.update(case.expected.encode("utf-8"))
        digest.update(b"\x1d")
    return digest.hexdigest()


# ---------------------------------------------------------------- 测试用例裁剪
def truncate_cases(
    cases: list[TestCase],
    origins: list[str],
    cfg: dict[str, Any],
) -> tuple[list[TestCase], list[str]]:
    """从验证用例集**前缀**里取出可公开的用例（数量 + 总体积双重上限）。

    cases 与 origins 一一对应，顺序即验证时的用例顺序，因此结果必然是前缀。
    「前缀」这个性质不是美观问题：公开用例只有作为那次实测 AC 用例集的子前缀，
    其正确性才由实测背书。
    """
    limits = cfg["output_limits"]
    max_cases = int(limits["max_cases_per_problem"])
    max_total = int(limits["max_problem_total_bytes"])

    picked: list[TestCase] = []
    picked_origins: list[str] = []
    total_bytes = 0
    for case, origin in zip(cases, origins):
        if len(picked) >= max_cases:
            break
        size = len(case.input.encode("utf-8")) + len(case.expected.encode("utf-8"))
        if picked and total_bytes + size > max_total:
            break
        if size > max_total:
            break
        picked.append(case)
        picked_origins.append(origin)
        total_bytes += size
    return picked, picked_origins


def case_bytes(cases: Iterable[TestCase]) -> int:
    return sum(
        len(case.input.encode("utf-8")) + len(case.expected.encode("utf-8"))
        for case in cases
    )


def _count_by(origins: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for origin in origins:
        out[origin] = out.get(origin, 0) + 1
    return out


# ---------------------------------------------------------------- 单题落盘
def meta_payload(
    pid: str,
    row: dict[str, Any],
    problem: dict[str, Any],
    cases: list[TestCase],
    origins: list[str],
) -> dict[str, Any]:
    """meta.json（公开）：判题与复现所需的元数据。

    字段名与 `benchmark.dataset.load_problem` 的读取契约对齐
    （`id` / `difficulty` / `tags`），其余为构建期溯源信息。
    """
    return {
        "id": pid,
        "name": problem["name"],
        "cc_id": row["cc_id"],
        "source": problem["source_name"],
        # benchmark 侧按 difficulty（CF rating）做难度分桶，按 tags 做类别统计
        "difficulty": problem["cf_rating"],
        "difficulty_name": problem["difficulty_name"],
        "difficulty_bin": row.get("difficulty_bin"),
        "difficulty_bin_source": row.get("difficulty_bin_source"),
        # --- 算法标签 ---
        "category": row.get("primary_category"),
        "category_source": row.get("category_source"),
        "category_matched": row.get("category_matched") or [],
        "categories_all": row.get("categories_all") or [],
        "tags": problem["cf_tags"],
        "taco_tags": row.get("taco_tags") or [],
        "taco_skill_types": row.get("taco_skill_types") or [],
        "taco_difficulty": row.get("taco_difficulty"),
        "taco_match": row.get("taco_match"),
        "taco_url": row.get("taco_url"),
        # --- 资源限制 ---
        "time_limit_seconds": row["time_limit_seconds"],
        "time_limit_source": row["time_limit_source"],
        "memory_limit_bytes": row["memory_limit_bytes"],
        "io_mode": "stdio",
        # 判题所需的比对模式：token=空白分词逐 token 精确比对；
        # float=token 结构必须一致、数值 token 允许
        # max(1e-6, 1e-6*|expected|) 的误差（Codeforces 上由特殊判题器处理的
        # 浮点输出题）。这一字段是**离线验证与应用判题的共同口径**：应用侧必须
        # 按题取出使用，否则同一份参考解会在数据集里是 AC、在应用里是 WA。
        "comparison": verify.normalize_comparison(row.get("comparison")),
        # --- 规模信息 ---
        "tests": {
            "total": len(cases),
            "by_origin": _count_by(origins),
            "cases_digest": cases_digest(cases),
        },
        "reference": {
            "language": "python",
            "sha256": sha256_text(row["reference_code"]),
            "verified": True,
        },
        "n_solutions": len(problem["solutions"]),
        "n_python3_solutions": row.get("n_python3_solutions"),
        "n_incorrect_solutions": row.get("n_incorrect_solutions"),
    }


def tests_payload(cases: Iterable[TestCase]) -> list[dict[str, str]]:
    """tests.json（公开）：题目用例。

    写成 `[{"input", "expected"}]` 的裸列表 —— 与
    `benchmark.dataset._load_tests` 的读取契约完全一致，应用侧无需适配代码。
    """
    return [{"input": case.input, "expected": case.expected} for case in cases]


def reference_payload(
    pid: str,
    code: str,
    verification: verify.Verification,
    cases: list[TestCase],
    origins: list[str],
    *,
    timeout: float,
    memory_limit_mb: int,
    candidates_total: int,
    attempts: list[dict[str, Any]],
) -> dict[str, Any]:
    """参考解验证证据（私有）：下游任何指标都应能回溯到这条代码与这套用例。

    放私有侧的理由：证据里含逐用例失败样本（stdout/expected 头部），对漏网的
    「答案不唯一」题目，这些样本会暴露期望答案本身。
    """
    return {
        "pid": pid,
        "language": "python",
        "code_sha256": sha256_text(code),
        "verified": True,
        "verdict": verification.verdict,
        # 这条代码是用哪种比对模式验出来的；应用侧按题取用同一模式
        "comparison": verification.comparison,
        "cases_total": verification.cases_total,
        "cases_passed": verification.cases_passed,
        "cases_digest": verification.cases_sha256,
        "max_ms": round(verification.max_ms, 2),
        "timeout_seconds": timeout,
        "memory_limit_mb": memory_limit_mb,
        "tried_index": verification.tried_index,
        "candidates_total": candidates_total,
        "case_origins": _count_by(origins),
        "case_failures": verification.failures[:5],
        "attempts": [
            {key: value for key, value in attempt.items() if key != "failures"}
            for attempt in attempts
        ],
        "verify_command": f"python scripts/build_dataset.py --stage verify --only {pid}",
    }


# ---------------------------------------------------------------- 目录写入
def _dump(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def prune_stale(cfg: dict[str, Any], keep: set[str], *, dry_run: bool = True) -> list[str]:
    """找出（并可选删除）不属于本次入选题目的陈旧**题目目录**。

    为什么要这么做：改过采样规模或过滤条件后重跑，旧题目目录会留在目录里，
    而 manifest 只列本次入选的题 —— 目录内容与清单不一致，下游按目录遍历
    （`load_dataset` 正是按目录遍历）就会读到「不在评测集里」的题。
    默认只提示不删除，避免误伤手工放进来的文件。
    """
    stale: list[str] = []
    for root in (dataset_root(cfg), private_root(cfg)):
        if not root.exists():
            continue
        for child in sorted(root.iterdir()):
            if child.is_dir() and child.name not in keep:
                stale.append(str(child))

    if not dry_run:
        failed: list[str] = []
        for item in stale:
            try:
                shutil.rmtree(item)
            except OSError:
                # 某些受管环境会把删除重定向到回收站，回收站不可用时删除会失败。
                # 这里不中断整体流程，把失败项返回给调用方由人工处置。
                failed.append(item)
        stale = failed
    return stale


def write_dataset(
    selected: list[dict[str, Any]],
    problems: dict[str, dict[str, Any]],
    verified: dict[str, tuple[verify.Verification | None, dict[str, Any]]],
    cfg: dict[str, Any],
    *,
    out_root: str | Path | None = None,
    verbose: bool = True,
) -> dict[str, Any]:
    """写「每题一目录」的公开产物 + 私有验证证据，返回 manifest 主体"""
    paths = cfg["paths"]
    public_root = dataset_root(cfg, out_root)
    priv_root = private_root(cfg, out_root)

    index: list[dict[str, Any]] = []
    total_cases = 0
    total_bytes = 0

    for row in selected:
        pid = row["pid"]
        problem = problems.get(pid)
        if problem is None:
            continue

        cases, origins, _digest = verify.select_cases_with_origins(problem, cfg, pid)
        public, public_origins = truncate_cases(cases, origins, cfg)

        pdir = public_root / pid
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "problem.md").write_text(problem["description"], encoding="utf-8")
        (pdir / "reference.py").write_text(row["reference_code"], encoding="utf-8")
        _dump(pdir / "meta.json", meta_payload(pid, row, problem, public, public_origins))
        _dump(pdir / "tests.json", tests_payload(public))

        verification, detail = verified[pid]
        assert verification is not None, f"{pid} 未通过验证却进入落盘阶段"
        _dump(
            priv_root / pid / "verification.json",
            reference_payload(
                pid,
                row["reference_code"],
                verification,
                cases,
                origins,
                timeout=float(detail["timeout_seconds"]),
                memory_limit_mb=int(detail.get("memory_limit_mb", 512)),
                candidates_total=int(detail["candidates_total"]),
                attempts=detail["attempts"],
            ),
        )

        size = case_bytes(public)
        total_cases += len(public)
        total_bytes += size
        index.append(
            {
                "pid": pid,
                "cc_id": row["cc_id"],
                "name": problem["name"],
                "source": problem["source_name"],
                "category": row.get("primary_category"),
                "difficulty_bin": row.get("difficulty_bin"),
                "cf_contest_id": problem["cf_contest_id"],
                "cf_index": problem["cf_index"],
                "cf_rating": problem["cf_rating"],
                "n_cases": len(public),
                "cases_bytes": size,
                "statement_bytes": len(problem["description"].encode("utf-8")),
                "reference_bytes": len(row["reference_code"].encode("utf-8")),
                "reference_sha256": sha256_text(row["reference_code"]),
                "comparison": row.get("comparison", verify.DEFAULT_COMPARISON),
                "taco_match": row.get("taco_match"),
                "path": f"{paths['dataset_dir']}/{pid}",
            }
        )
        if verbose:
            print(
                f"  写入 {pid}  {problem['name'][:32]:<34} "
                f"用例 {len(public):>3}  {size / 1024:>7.1f} KiB",
                flush=True,
            )

    index.sort(key=lambda item: item["pid"])
    return {
        "problems": index,
        "totals": {
            "problems": len(index),
            "testcases": total_cases,
            "testcases_bytes": total_bytes,
        },
    }


def select_cases_with_origins(
    problem: dict[str, Any], cfg: dict[str, Any], key: str
) -> tuple[list[TestCase], list[str], str]:
    """与 verify.select_cases 等价的带来源版本（保持同一顺序与同一选择规则）"""
    return verify.select_cases_with_origins(problem, cfg, key)


def build_manifest(
    cfg: dict[str, Any],
    *,
    pool_stats: dict[str, Any],
    taco_stats: dict[str, int],
    sampling_diagnostics: dict[str, Any],
    dataset: dict[str, Any],
    verified_total: int,
    verify_rejects: list[dict[str, Any]],
    elapsed_seconds: float,
    riegeli_check: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """全量构建清单：配置快照 + 各阶段统计 + 逐题索引"""
    dataset_cfg = cfg["dataset"]
    paths = cfg["paths"]
    return {
        "name": dataset_cfg["name"],
        "version": dataset_cfg["version"],
        "generated_by": "scripts/build_dataset.py",
        "seed": dataset_cfg["seed"],
        "config": cfg,
        "stages": {
            "riegeli_check": riegeli_check,
            "pool": {
                "shards": pool_stats.get("shards"),
                "records": pool_stats.get("records"),
                "passed": pool_stats.get("passed"),
                "reject_reasons": pool_stats.get("reject_reasons", {}),
                "by_source": pool_stats.get("by_source", {}),
            },
            "taco": taco_stats,
            "sampling": {
                "target_n": sampling_diagnostics.get("target_n"),
                "eligible": sampling_diagnostics.get("eligible"),
                "non_empty_cells": sampling_diagnostics.get("non_empty_cells"),
                "effective_min_per_cell": sampling_diagnostics.get("effective_min_per_cell"),
                "max_per_category": sampling_diagnostics.get("max_per_category"),
                "quota_total": sampling_diagnostics.get("quota_total"),
                "filled_beyond_quota": sampling_diagnostics.get("filled_beyond_quota"),
                "actual_cells": sampling_diagnostics.get("actual_cells", {}),
                "actual_by_category": sampling_diagnostics.get("actual_by_category", {}),
                "under_min_cells": sampling_diagnostics.get("under_min_cells", []),
                "zero_quota_cells": sampling_diagnostics.get("zero_quota_cells", []),
                "supply": sampling_diagnostics.get("supply", {}),
                "supply_primary": sampling_diagnostics.get("supply_primary", {}),
            },
            "verification": {
                "candidates_verified": verified_total,
                "passed": dataset["totals"]["problems"],
                "rejected": len(verify_rejects),
                "rejected_detail": verify_rejects,
            },
        },
        "totals": dataset["totals"],
        "problems": dataset["problems"],
        "elapsed_seconds": round(elapsed_seconds, 1),
        "artifacts": {
            "problem": f"{paths['dataset_dir']}/<pid>/problem.md",
            "meta": f"{paths['dataset_dir']}/<pid>/meta.json",
            "tests": f"{paths['dataset_dir']}/<pid>/tests.json",
            "reference": f"{paths['dataset_dir']}/<pid>/reference.py",
            "public_variants": f"{paths['dataset_dir']}/<pid>/variants.json",
            "reference_evidence": f"{paths['private_dir']}/<pid>/verification.json",
            "private_variants": f"{paths['private_dir']}/<pid>/variants.json",
            "private_labels": f"{paths['private_dir']}/<pid>/labels.json",
        },
        "notes": [
            "一个目录 = 一道题；题面/元数据/用例/参考解/公开候选各自独立成文件。",
            "tests.json 是参考解验证用例集的前缀，未写入任何未经验证的用例。",
            "meta.json 的 comparison 字段是离线验证与应用判题的共同口径，必须按题取用。",
            "首错定位、错误类型标注、注入候选正文均在 private/ 下，不入公开仓库。",
        ],
    }


def write_manifest(manifest: dict[str, Any], path: str | Path) -> Path:
    target = Path(path)
    _dump(target, manifest)
    return target
