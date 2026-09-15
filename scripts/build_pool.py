"""第一趟扫描：遍历 CodeContests 全部分片，产出轻量元数据题池。

这是构建评测集的第一步，也是唯一需要完整解码 2.9 GB 原始数据的步骤。
产物是 data/interim/pool.jsonl —— 每行一道题（含未通过门槛的题，便于统计
淘汰原因），第二趟只需按 cc_id 回读入选题目所在的分片。

用法：

    python scripts/build_pool.py                        # 全量扫描
    python scripts/build_pool.py --max-train-shards 3   # 小样本先行验证
    python scripts/build_pool.py --summary-only         # 不扫描，只汇总已有题池
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hy3_algoaudit.builder import pool  # noqa: E402


def print_rejects(stats: dict) -> None:
    """打印淘汰原因直方图（一道题可能命中多个原因）"""
    reasons = stats.get("reject_reasons") or {}
    if not reasons:
        return
    total = sum(reasons.values())
    print("\n淘汰原因（一道题可命中多个）：")
    for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"  {reason:<26} {count:>6}  {count / total:>6.1%}")


def print_summary(pool_path: Path) -> None:
    """对通过门槛的题池做「类别 × 难度箱」供给统计"""
    rows = pool.load_pool(pool_path)
    print(f"\n通过门槛题目：{len(rows)}")

    summary = pool.summarize_pool(rows)
    print("\n按算法类别：")
    for name, count in sorted(summary["by_category"].items(), key=lambda kv: -kv[1]):
        print(f"  {name:<16} {count:>6}")

    print("\n按难度箱：")
    for name, count in sorted(summary["by_rating_bin"].items()):
        print(f"  {name:<16} {count:>6}")

    print("\n单元供给（类别@难度箱）：")
    for cell, count in sorted(summary["cells"].items()):
        flag = "" if count >= 3 else "   <- 供给不足"
        print(f"  {cell:<28} {count:>5}{flag}")

    print("\n按来源：")
    by_source: dict[str, int] = {}
    for row in rows:
        by_source[row["source_name"]] = by_source.get(row["source_name"], 0) + 1
    for name, count in sorted(by_source.items(), key=lambda kv: -kv[1]):
        print(f"  {name:<16} {count:>6}")


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 CodeContests 元数据题池")
    parser.add_argument("--config", default=None, help="配置文件路径，默认 configs/dataset.yaml")
    parser.add_argument("--out", default=None, help="输出路径，默认 {interim_dir}/pool.jsonl")
    parser.add_argument("--max-train-shards", type=int, default=None, help="只扫描前 N 个 train 分片")
    parser.add_argument("--summary-only", action="store_true", help="只汇总已有 pool.jsonl，不重新扫描")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    cfg = pool.load_config(args.config)
    out_path = Path(args.out) if args.out else Path(cfg["paths"]["interim_dir"]) / "pool.jsonl"

    if args.summary_only:
        print_summary(out_path)
        return 0

    max_shards = args.max_train_shards
    if max_shards is None:
        max_shards = cfg["dataset"].get("scan_train_shards")

    shards = pool.discover_shards(
        cfg["paths"]["codecontests_dir"], max_train_shards=max_shards
    )
    print(f"发现分片 {len(shards)} 个，开始扫描 -> {out_path}\n")

    stats = pool.build_pool(shards, cfg, out_path, verbose=not args.quiet)

    print("-" * 72)
    print(f"扫描完成：分片 {stats['shards']}  记录 {stats['records']}  通过 {stats['passed']}")
    print_rejects(stats)

    print("\n通过题目的来源分布：")
    for name, count in sorted(stats["by_source"].items(), key=lambda kv: -kv[1]):
        print(f"  {name:<16} {count:>6}")

    print_summary(out_path)

    manifest = out_path.with_suffix(".stats.json")
    manifest.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n统计摘要已写入 {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
