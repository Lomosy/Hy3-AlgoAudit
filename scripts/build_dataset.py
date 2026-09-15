"""数据与评测集构建主流水线（ProcessEval-CP）。

一条命令走完「扫描 → 关联 → 采样 → 实测验证 → 落盘」五个阶段：

    python scripts/build_dataset.py --stage scan                 # 只体检原始数据
    python scripts/build_dataset.py --stage pool                 # 只构建元数据题池
    python scripts/build_dataset.py --stage sample --n 8         # 只采样并打印分布
    python scripts/build_dataset.py --stage all --n 8            # 小样本端到端
    python scripts/build_dataset.py --stage all --n 180          # 全量端到端

为什么「实测验证」是不可跳过的关卡：本数据集所有下游指标（误报率、首错定位
准确率、AC-but-Invalid 识别率）都建立在「参考解确实正确」这个前提上。
所以本脚本只有在某条 Python 3 解真实跑过全部用例并全 AC 时，才把这道题写进
公开评测集；验证失败就淘汰，并记录淘汰原因。

产物：
    data/interim/pool.jsonl          第一阶段全量题池（含未通过门槛的题）
    data/interim/enriched.jsonl      叠加 TACO 标签与分层后的题池
    data/interim/selected.jsonl      最终入选题目（含配额单元与参考解代码）
    data/interim/verify_report.json  每题验证证据与淘汰明细
    data/processeval-cp/manifest.json          构建清单与逐题索引（公开）
    data/processeval-cp/<pid>/problem.md       题面（公开）
    data/processeval-cp/<pid>/meta.json        元数据（公开，含判题比对模式）
    data/processeval-cp/<pid>/tests.json       测试用例（公开，已验证用例的前缀）
    data/processeval-cp/<pid>/reference.py     参考解（公开，实测 AC）
    private/processeval-cp/<pid>/verification.json  参考解验证证据（私有）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hy3_algoaudit.builder import (  # noqa: E402
    dataset,
    pool,
    sample,
    taco,
    verify,
)


# ---------------------------------------------------------------- 工具
def log(message: str) -> None:
    print(message, flush=True)


def stage_header(index: int, title: str) -> None:
    log("")
    log("=" * 72)
    log(f"阶段 {index}：{title}")
    log("=" * 72)


def dump_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def pids_of(rows: list[dict[str, Any]]) -> list[str]:
    return [row["cc_id"] for row in rows]


# ---------------------------------------------------------------- 阶段 1：题池
def stage_pool(cfg: dict[str, Any], args: argparse.Namespace) -> Path:
    interim = Path(cfg["paths"]["interim_dir"])
    pool_path = interim / "pool.jsonl"

    if args.reuse_pool and pool_path.exists():
        log(f"复用已有题池 {pool_path}（--reuse-pool）")
        return pool_path

    max_shards = args.max_train_shards
    if max_shards is None:
        max_shards = cfg["dataset"].get("scan_train_shards")
    shards = pool.discover_shards(cfg["paths"]["codecontests_dir"], max_train_shards=max_shards)
    log(f"发现分片 {len(shards)} 个，开始扫描…")

    stats = pool.build_pool(shards, cfg, pool_path, verbose=not args.quiet)
    log(f"扫描完成：记录 {stats['records']}  通过 {stats['passed']}")

    log("\n淘汰原因：")
    total_rejects = sum(stats["reject_reasons"].values()) or 1
    for reason, count in sorted(stats["reject_reasons"].items(), key=lambda kv: -kv[1]):
        log(f"  {reason:<26} {count:>6}  {count / total_rejects:>6.1%}")

    (interim / "pool.stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return pool_path


# ---------------------------------------------------------------- 阶段 2：TACO
def stage_enrich(cfg: dict[str, Any], args: argparse.Namespace) -> tuple[Path, dict[str, int]]:
    interim = Path(cfg["paths"]["interim_dir"])
    out_path = interim / "enriched.jsonl"

    if args.reuse_enriched and out_path.exists():
        log(f"复用已有富化题池 {out_path}（--reuse-enriched）")
        rows = load_jsonl(out_path)
        stats = {
            "contest_index": sum(1 for r in rows if r.get("taco_match") == "contest_index"),
            "title": sum(1 for r in rows if r.get("taco_match") == "title"),
            "none": sum(1 for r in rows if r.get("taco_match") == "none"),
        }
        return out_path, stats

    rows = pool.load_pool(interim / "pool.jsonl")
    log(f"读入通过门槛题目 {len(rows)} 道")

    index_path = Path(cfg["paths"]["taco_dir"]) / "taco_index.parquet"
    if args.no_taco or not index_path.exists():
        if not index_path.exists() and not args.no_taco:
            log(f"未找到 TACO 索引 {index_path}，跳过关联（可先运行 scripts/fetch_taco.py）")
        stats = {"contest_index": 0, "title": 0, "none": len(rows)}
        for row in rows:
            row["taco_match"] = "none"
            row["taco_tags"] = []
            row["taco_skill_types"] = []
            row["taco_difficulty"] = None
            row["taco_url"] = None
    else:
        log(f"用 TACO 索引关联：{index_path}")
        stats = taco.enrich(rows, index_path)
        log(
            f"关联结果：比赛题号 {stats['contest_index']}  标题 {stats['title']}  "
            f"未命中 {stats['none']}"
        )

    dump_jsonl(out_path, rows)
    log(f"已写入 {out_path}")

    # 关联前后「难度可分层」与「可归类」的对比，这是 TACO 价值的直接证据
    bins = [name for _, _, name in sample.bins_of(cfg)]
    before_unrated = sum(
        1 for r in rows if pool.rating_bin(r.get("cf_rating"), r) not in bins
    )
    enriched_rows = sample.prepare([dict(r) for r in rows], cfg)
    after_unrated = sum(1 for r in enriched_rows if r["difficulty_bin"] not in bins)
    log("\n难度可分层题目：")
    log(f"  仅用 cf_rating + Difficulty 枚举  {len(rows) - before_unrated} / {len(rows)}")
    log(f"  叠加 TACO difficulty 后           {len(rows) - after_unrated} / {len(rows)}")
    log(f"  → TACO 补齐 {before_unrated - after_unrated} 道，仍有 {after_unrated} 道无法分层")

    by_bin_source: dict[str, int] = {}
    for row in enriched_rows:
        key = row.get("difficulty_bin_source") or "none"
        by_bin_source[key] = by_bin_source.get(key, 0) + 1
    log("  分层来源分布：" + "  ".join(
        f"{k}={v}" for k, v in sorted(by_bin_source.items(), key=lambda kv: -kv[1])
    ))

    categorized = sum(1 for r in enriched_rows if r["primary_category"] != "unclassified")
    log(f"\n可归类题目：{categorized} / {len(enriched_rows)}（{categorized / len(enriched_rows):.1%}）")
    log("归类来源分布：")
    sources: dict[str, int] = {}
    for row in enriched_rows:
        sources[row["category_source"]] = sources.get(row["category_source"], 0) + 1
    for name, count in sorted(sources.items(), key=lambda kv: -kv[1]):
        log(f"  {name:<16} {count:>6}")

    eligible = sum(
        1
        for row in enriched_rows
        if row["primary_category"] in cfg["sampling"]["categories"]
        and row["difficulty_bin"] in bins
    )
    log(f"\n最终可采样池（可归类 且 可分层）：{eligible} 道")
    return out_path, stats


# ---------------------------------------------------------------- 阶段 3：采样
def stage_sample(
    cfg: dict[str, Any], args: argparse.Namespace, taco_stats: dict[str, int]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    interim = Path(cfg["paths"]["interim_dir"])
    rows = load_jsonl(interim / "enriched.jsonl")
    rows = sample.prepare(rows, cfg)

    target = args.n if args.n else int(cfg["dataset"]["n_problems"])
    # 多采一些：验证阶段会淘汰一部分题，超额采样用于回填
    oversample = float(args.oversample)
    candidates_n = int(target * oversample)

    selected, diagnostics = sample.plan_and_sample(rows, cfg, n=candidates_n)
    log(f"目标题数 {target}，超额采样 {candidates_n}（倍率 {oversample}）→ 实得 {len(selected)}")
    log("")
    log(sample.format_matrix(diagnostics, cfg))
    log("")
    log(f"可采样池 {diagnostics['eligible']} 道；单元格 {diagnostics['non_empty_cells']} 个")
    if diagnostics.get("under_min_cells"):
        log(f"供给不足的单元（低于 min_per_cell）：{diagnostics['under_min_cells']}")

    dump_jsonl(interim / "selected.jsonl", selected)
    (interim / "sampling.json").write_text(
        json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return selected, diagnostics


# ---------------------------------------------------------------- 阶段 4：验证
def stage_verify(
    cfg: dict[str, Any],
    args: argparse.Namespace,
    selected: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    interim = Path(cfg["paths"]["interim_dir"])
    log(f"回读题面与测试用例：{len(selected)} 道题")

    problems = pool.load_records(cfg["paths"]["codecontests_dir"], pids_of(selected))
    log(f"取回 {len(problems)} 道题的完整数据")

    workers = args.workers if args.workers is not None else int(cfg["verification"]["workers"])
    log(f"开始实测参考解（进程数 {workers}，每用例一个子进程，这是最耗时的一步）\n")

    started = time.time()
    results = verify.verify_many(problems, cfg, workers=workers, verbose=not args.quiet)
    elapsed = time.time() - started

    passed: list[dict[str, Any]] = []
    rejects: list[dict[str, Any]] = []
    report: dict[str, Any] = {}

    for row in selected:
        pid = row["cc_id"]
        verification, detail = results.get(pid, (None, {"error": "missing"}))
        report[pid] = {
            "passed": verification is not None,
            "verification": verification.to_dict() if verification else None,
            "detail": {
                key: value for key, value in detail.items() if key != "solution_code"
            },
        }
        if verification is not None:
            picked = dict(row)
            picked["pid"] = pid
            picked["reference_code"] = detail["solution_code"]
            picked["reference_sha256"] = verification.code_sha256
            picked["verified_cases"] = verification.cases_total
            picked["verified_cases_sha256"] = verification.cases_sha256
            picked["verified_max_ms"] = round(verification.max_ms, 2)
            picked["comparison"] = verification.comparison
            picked["verify_tried"] = verification.tried_index
            passed.append(picked)
        else:
            rejects.append(
                {
                    "pid": pid,
                    "name": row.get("name"),
                    "attempts": len(detail.get("attempts", [])),
                    "reasons": sorted(
                        {
                            attempt.get("reject_reason") or attempt.get("verdict")
                            for attempt in detail.get("attempts", [])
                        }
                    ),
                    "suspected_special_judge": verify.suspected_special_judge(detail),
                }
            )

    log("")
    log(f"验证完成，耗时 {elapsed / 60:.1f} 分钟：通过 {len(passed)}  淘汰 {len(rejects)}")
    tolerant = [r for r in passed if r.get("comparison") != verify.DEFAULT_COMPARISON]
    if tolerant:
        log(
            f"其中 {len(tolerant)} 道需要浮点容差比对（token 比对会误判 WA）："
            + ", ".join(r["pid"] for r in tolerant)
        )
    attempts_hist: dict[int, int] = {}
    for row in passed:
        attempts_hist[row["verify_tried"]] = attempts_hist.get(row["verify_tried"], 0) + 1
    log("首条解即通过 / 需尝试第 N+1 条解的题目数：" + "  ".join(
        f"{k + 1}条={v}" for k, v in sorted(attempts_hist.items())
    ))
    if rejects:
        log("\n淘汰明细（前 20 条）：")
        for item in rejects[:20]:
            log(f"  {item['pid']}  {str(item['name'])[:32]:<34} 尝试 {item['attempts']:>2} 条  原因 {item['reasons']}")
        reason_hist: dict[str, int] = {}
        for item in rejects:
            for reason in item["reasons"]:
                reason_hist[str(reason)] = reason_hist.get(str(reason), 0) + 1
        log("\n淘汰原因分布：")
        for reason, count in sorted(reason_hist.items(), key=lambda kv: -kv[1]):
            log(f"  {reason:<34} {count:>5}")
        suspected = [item for item in rejects if item.get("suspected_special_judge")]
        if suspected:
            log(
                f"\n其中 {len(suspected)} 道疑似「答案不唯一」漏网（题面未声明、"
                "多条解均以 WA 收场且输出结构一致），建议人工复核："
            )
            for item in suspected[:15]:
                log(f"  {item['pid']}  {str(item['name'])[:40]}")

    # --only 属于调试用法，写到独立文件，避免覆盖全量验证结果
    suffix = ".only" if args.only else ""
    (interim / f"verify_report{suffix}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    passed.sort(key=lambda r: r["pid"])
    dump_jsonl(interim / f"verified{suffix}.jsonl", passed)
    return passed, {"elapsed_seconds": elapsed, "rejects": rejects}


# ---------------------------------------------------------------- 阶段 5：落盘
def stage_write(
    cfg: dict[str, Any],
    verified: list[dict[str, Any]],
    *,
    pool_stats: dict[str, Any],
    taco_stats: dict[str, int],
    diagnostics: dict[str, Any],
    verify_meta: dict[str, Any],
    n_target: int,
    prune: bool = False,
) -> dict[str, Any]:
    # 验证后按正式目标题数重新抽样，配额自动适配「已验证通过」的供给
    selected, final_diag = sample.plan_and_sample(verified, cfg, n=n_target)
    log(f"在已验证题目中按配额重抽：目标 {n_target} → 实得 {len(selected)}")
    log("")
    log(sample.format_matrix(final_diag, cfg))

    if len(selected) < n_target:
        log(f"\n注意：已验证通过的题目不足以填满目标 {n_target}，实得 {len(selected)}。")
        log("可提高 --oversample 或放宽 filters 后重跑。")

    problems_dir = dataset.dataset_root(cfg)
    private_dir = dataset.private_root(cfg)
    for directory in (problems_dir, private_dir):
        directory.mkdir(parents=True, exist_ok=True)

    problems = pool.load_records(cfg["paths"]["codecontests_dir"], pids_of(selected))
    verified_map = {
        row["pid"]: (None, {"timeout_seconds": 0.0, "candidates_total": 0, "attempts": []})
        for row in selected
    }

    # 验证结果与落盘阶段共享同一份证据：从 verify_report 里取回逐次尝试明细
    report_path = Path(cfg["paths"]["interim_dir"]) / "verify_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}

    log("")
    for row in selected:
        pid = row["pid"]
        entry = report.get(pid, {})
        verification_dict = entry.get("verification") or {}
        verification = verify.Verification(
            ok=True,
            verdict=verification_dict.get("verdict", "AC"),
            language=verification_dict.get("language", 3),
            language_name=verification_dict.get("language_name", "PYTHON3"),
            tried_index=verification_dict.get("tried_index", 0),
            cases_total=row["verified_cases"],
            cases_passed=row["verified_cases"],
            max_ms=row["verified_max_ms"],
            total_ms=row["verified_max_ms"],
            code_sha256=row["reference_sha256"],
            cases_sha256=row["verified_cases_sha256"],
            comparison=verify.normalize_comparison(row.get("comparison")),
        )
        detail = entry.get("detail") or {}
        verified_map[pid] = (
            verification,
            {
                "timeout_seconds": detail.get("timeout_seconds", 0.0),
                "memory_limit_mb": detail.get("memory_limit_mb", 512),
                "candidates_total": detail.get("candidates_total", 0),
                "attempts": detail.get("attempts", []),
            },
        )

    dataset_body = dataset.write_dataset(
        selected, problems, verified_map, cfg, verbose=not cfg.get("_quiet", False)
    )

    # 目录与清单必须一致：改过规模/过滤条件后重跑会留下陈旧题目目录
    keep = {row["pid"] for row in selected}
    stale = dataset.prune_stale(cfg, keep, dry_run=True)
    if stale:
        if prune:
            failed = dataset.prune_stale(cfg, keep, dry_run=False)
            removed = len(stale) - len(failed)
            log(f"\n已清理 {removed} 个陈旧题目目录（--prune）")
            if failed:
                log(f"以下 {len(failed)} 个删除失败（可能被环境限制），请手动处理：")
                for item in failed:
                    log(f"  {item}")
        else:
            log(f"\n发现 {len(stale)} 个不属于本次入选题目的陈旧题目目录，"
                f"它们会让目录与 manifest 不一致：")
            for item in stale[:10]:
                log(f"  {item}")
            if len(stale) > 10:
                log(f"  …还有 {len(stale) - 10} 个")
            log("如需清理请加 --prune")
    manifest = dataset.build_manifest(
        cfg,
        pool_stats=pool_stats,
        taco_stats=taco_stats,
        sampling_diagnostics=final_diag,
        dataset=dataset_body,
        verified_total=len(verified),
        verify_rejects=verify_meta.get("rejects", []),
        elapsed_seconds=verify_meta.get("elapsed_seconds", 0.0),
    )
    manifest["stages"]["sampling"]["oversampled"] = diagnostics.get("target_n")
    manifest["stages"]["sampling"]["quota"] = final_diag.get("quota", {})
    # 注意统计口径：这两个都按**最终入选**的题目算，而不是全部通过验证的题目
    manifest["stages"]["taco_tag_supplement"] = _taco_impact(selected)
    manifest["stages"]["comparison_modes"] = _comparison_modes(selected)

    manifest_path = dataset.write_manifest(
        manifest, dataset.dataset_root(cfg) / dataset.MANIFEST_NAME
    )
    log("")
    log(f"清单已写入 {manifest_path}")
    log(f"题目 {dataset_body['totals']['problems']} 道，用例 {dataset_body['totals']['testcases']} 条，"
        f"合计 {dataset_body['totals']['testcases_bytes'] / 1048576:.2f} MiB")
    return manifest


def _comparison_modes(selected: list[dict[str, Any]]) -> dict[str, Any]:
    """统计最终入选题目各用了哪种比对模式。

    这个统计必须按**最终入选**的题目算：comparison 是随题落盘的判题口径，
    应用侧要按它挑模式，所以清单里必须能一眼看出「有多少题不能按最严格的
    token 模式判」以及具体是哪些题。
    """
    from hy3_algoaudit.sandbox.judge import COMPARISON_MODES

    counts = {mode: 0 for mode in COMPARISON_MODES}
    pids: dict[str, list[str]] = {mode: [] for mode in COMPARISON_MODES}
    for row in selected:
        mode = verify.normalize_comparison(row.get("comparison"))
        if mode not in counts:
            mode = verify.DEFAULT_COMPARISON
        counts[mode] += 1
        pids[mode].append(row["pid"])
    return {
        "counts": counts,
        "relaxed_modes": {k: v for k, v in pids.items() if k != verify.DEFAULT_COMPARISON},
    }


def _taco_impact(verified: list[dict[str, Any]]) -> dict[str, int]:
    """统计 TACO 在最终入选题目中的实际贡献"""
    return {
        "with_taco_tags": sum(1 for r in verified if r.get("taco_tags")),
        "with_taco_skill_types": sum(1 for r in verified if r.get("taco_skill_types")),
        "difficulty_from_taco": sum(
            1 for r in verified if r.get("difficulty_bin_source") == "taco"
        ),
        "category_from_taco": sum(
            1 for r in verified if r.get("category_source") == "taco"
        ),
    }


# ---------------------------------------------------------------- 入口
def main() -> int:
    parser = argparse.ArgumentParser(
        description="构建 ProcessEval-CP 数据与评测集",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", default=None)
    parser.add_argument(
        "--stage",
        default="all",
        choices=["scan", "pool", "enrich", "sample", "verify", "write", "all"],
        help=(
            "scan=原始数据体检  pool=题池  enrich=TACO 关联  "
            "sample=采样  verify=实测参考解  write=落盘  all=端到端"
        ),
    )
    parser.add_argument("--n", type=int, default=None, help="目标题数，默认取配置 n_problems")
    parser.add_argument("--oversample", type=float, default=1.4, help="验证前的超额采样倍率")
    parser.add_argument("--max-train-shards", type=int, default=None, help="只扫描前 N 个 train 分片")
    parser.add_argument("--workers", type=int, default=None, help="验证并行进程数")
    parser.add_argument("--only", default=None, help="只验证指定 cc_id（逗号分隔），用于单题调试")
    parser.add_argument("--no-taco", action="store_true", help="跳过 TACO 关联")
    parser.add_argument("--reuse-pool", action="store_true", help="复用已有 pool.jsonl")
    parser.add_argument("--reuse-enriched", action="store_true", help="复用已有 enriched.jsonl")
    parser.add_argument(
        "--prune",
        action="store_true",
        help="落盘时删除不属于本次入选题目的陈旧产物（默认只提示，不删除）",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    cfg = pool.load_config(args.config)
    cfg["_quiet"] = args.quiet
    n_target = args.n if args.n else int(cfg["dataset"]["n_problems"])
    interim = Path(cfg["paths"]["interim_dir"])
    started = time.time()

    # --- 只做单件事的阶段 ---
    if args.stage == "scan":
        return run_scan(cfg, args)

    # 各阶段的依赖链：遇到缺前置产物就报错，避免「静默重算覆盖已有结果」。
    # 早期版本里 verify 会顺带重跑采样，把 selected.jsonl 覆盖成默认规模，
    # 导致「只验证某几题」实际验证 0 题，是一次代价不小的踩坑。
    taco_stats = {"contest_index": 0, "title": 0, "none": 0}
    pool_stats = _load_stats(interim / "pool.stats.json")
    diagnostics: dict[str, Any] = {}
    verify_meta: dict[str, Any] = {}

    if args.stage in ("pool", "enrich", "sample", "all"):
        stage_header(1, "扫描 CodeContests 原始数据，构建元数据题池")
        stage_pool(cfg, args)
        pool_stats = _load_stats(interim / "pool.stats.json")
    elif args.stage in ("verify", "write"):
        require(interim / "pool.jsonl", "题池", "python scripts/build_dataset.py --stage pool")

    if args.stage in ("enrich", "sample", "all"):
        stage_header(2, "关联 TACO 标签与难度分层")
        _path, taco_stats = stage_enrich(cfg, args)
    elif args.stage in ("verify", "write"):
        require(
            interim / "enriched.jsonl",
            "TACO 富化题池",
            "python scripts/build_dataset.py --stage enrich --reuse-pool",
        )
        taco_stats = _taco_stats(cfg)

    if args.stage in ("sample", "all"):
        stage_header(3, "按「算法类别 × 难度箱」分层采样")
        _, diagnostics = stage_sample(cfg, args, taco_stats)
    elif args.stage in ("verify", "write"):
        require(
            interim / "selected.jsonl",
            "采样结果",
            "python scripts/build_dataset.py --stage sample --n <题数> --reuse-pool --reuse-enriched",
        )

    if args.stage in ("verify", "all"):
        stage_header(4, "实测参考解，淘汰不可信题目")
        selected = load_jsonl(interim / "selected.jsonl")
        if args.only:
            keep = {item.strip() for item in args.only.split(",") if item.strip()}
            selected = [row for row in selected if row["cc_id"] in keep]
            log(f"--only 生效，只验证 {len(selected)} 道题")
            if not selected:
                log("错误：--only 指定的题目不在 selected.jsonl 中")
                return 2
        verified, verify_meta = stage_verify(cfg, args, selected)

    if args.stage in ("write", "all"):
        stage_header(5, "落盘题目 / 测试用例 / 参考解 / 清单")
        if args.stage == "write":
            require(
                interim / "verified.jsonl",
                "验证结果",
                "python scripts/build_dataset.py --stage verify",
            )
        verified = load_jsonl(interim / "verified.jsonl")
        if not verify_meta:
            # --stage write 是独立进程，没有内存中的验证元信息，从报告回填
            verify_meta = _verify_meta_from_report(interim)
        if args.n is None and diagnostics.get("target_n"):
            # --stage all 时目标题数发生在本进程内，需要从「超额采样数」反推真实目标
            oversample = max(1.0, float(args.oversample))
            n_target = max(1, int(round(int(diagnostics["target_n"]) / oversample)))
        stage_write(
            cfg,
            verified,
            pool_stats=pool_stats,
            taco_stats=taco_stats,
            diagnostics=diagnostics,
            verify_meta=verify_meta,
            n_target=n_target,
            prune=args.prune,
        )

    log("")
    log(f"全部完成，总耗时 {(time.time() - started) / 60:.1f} 分钟")
    return 0


def require(path: Path, label: str, hint: str) -> None:
    if not path.exists():
        raise SystemExit(f"缺少{label}：{path}\n请先运行：{hint}")


def _verify_meta_from_report(interim: Path) -> dict[str, Any]:
    """从 verify_report.json 还原「淘汰了哪些题」，供独立运行的 write 阶段写清单"""
    path = interim / "verify_report.json"
    if not path.exists():
        return {}
    report = json.loads(path.read_text(encoding="utf-8"))
    rejects = [
        {
            "pid": pid,
            "attempts": len((entry.get("detail") or {}).get("attempts", [])),
            "reasons": sorted(
                {
                    attempt.get("reject_reason") or attempt.get("verdict")
                    for attempt in (entry.get("detail") or {}).get("attempts", [])
                }
            ),
        }
        for pid, entry in report.items()
        if not entry.get("passed")
    ]
    return {"rejects": rejects, "elapsed_seconds": 0.0, "from_report": True}


def run_scan(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    """复用 Riegeli 体检工具，避免重复实现"""
    import subprocess

    cmd = [sys.executable, str(Path(__file__).parent / "check_riegeli.py")]
    if args.config:
        cmd += ["--config", args.config]
    return subprocess.call(cmd)


def _load_stats(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _taco_stats(cfg: dict[str, Any]) -> dict[str, int]:
    path = Path(cfg["paths"]["interim_dir"]) / "enriched.jsonl"
    if not path.exists():
        return {"contest_index": 0, "title": 0, "none": 0}
    stats = {"contest_index": 0, "title": 0, "none": 0}
    for row in load_jsonl(path):
        how = row.get("taco_match") or "none"
        stats[how] = stats.get(how, 0) + 1
    return stats


if __name__ == "__main__":
    raise SystemExit(main())
