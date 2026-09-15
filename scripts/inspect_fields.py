"""字段号核对工具：扫描真实记录并输出字段直方图，用于与官方 proto 对照。

用法（项目根目录下）：

    python scripts/inspect_fields.py --shard data/codecontests_data/dm-code_contests/code_contests_train.riegeli-00000-of-00128 --limit 105

输出：
    1. 文件体检（chunk 类型直方图、记录数、签名校验）
    2. 每个字段号的出现次数、wire type、样例值
    3. 按来源分组，便于确认 Codeforces 专属字段（cf_renting / cf_tags 等）的真实字段号

设计意图：在把任何推断字段号写进 builder/proto.py 并投入使用之前，
必须先跑一遍本工具，逐项确认。核对不过的字段一律不采信。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hy3_algoaudit.builder import proto, riegeli  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Riegeli/ContestProblem 字段号核对")
    parser.add_argument("--shard", required=True, help="riegeli 文件路径")
    parser.add_argument("--limit", type=int, default=105, help="最多扫描多少条记录")
    parser.add_argument("--source", type=int, default=None, help="只看某个 source（2=CODEFORCES）")
    parser.add_argument("--json-out", default=None, help="把结果另存为 JSON")
    parser.add_argument("--show", type=int, default=0, help="额外打印 N 条完整解析结果")
    args = parser.parse_args()

    print("=" * 72)
    info = riegeli.describe(args.shard)
    print(f"文件          : {info['path']}")
    print(f"大小          : {info['file_size']:,} 字节")
    print(f"签名校验      : {'通过' if info['signature_ok'] else '不通过'}")
    print(f"chunk 总数    : {info['chunks']}  类型分布: {info['chunk_types']}")
    print(f"记录数        : {info['records']}")
    print(f"解码总字节    : {info['decoded_bytes']:,}")
    if info["chunk_types"].get("transposed"):
        print("!! 检测到 Transposed chunk，本读取器会跳过这些记录")
    print("=" * 72)

    records: list[bytes] = []
    sources: Counter[int] = Counter()
    for raw in riegeli.iter_records(args.shard):
        try:
            parsed = proto.parse_contest_problem(raw)
        except proto.ProtoError as exc:  # 记录损坏时跳过而不是整体失败
            print(f"  跳过一条无法解析的记录：{exc}")
            continue
        sources[parsed["source"]] += 1
        if args.source is not None and parsed["source"] != args.source:
            continue
        records.append(raw)
        if len(records) >= args.limit:
            break

    print("来源分布（全部扫描到的记录）:")
    for code, count in sorted(sources.items()):
        print(f"  {code:>2} {proto.SOURCE_NAMES.get(code, '?'):<16} {count}")
    print(f"参与字段统计的记录数: {len(records)}")
    print("=" * 72)

    report = proto.field_report(records, max_samples=3)
    declared = {v: k for k, v in proto.FIELD_NUMBER.items()}

    print(f"{'字段号':<6}{'出现次数':<10}{'wire':<8}{'当前映射':<26}样例")
    print("-" * 72)
    for number in sorted(report):
        entry = report[number]
        mapping = declared.get(number, "—（未声明）")
        wires = ",".join(str(w) for w in entry["wire_types"])
        samples = " | ".join(
            f"[w{w}] {str(s)[:48]!r}" for w, s in entry["samples"][:2]
        )
        print(f"{number:<6}{entry['count']:<10}{wires:<8}{mapping:<26}{samples}")

    undeclared = sorted(set(report) - set(declared))
    if undeclared:
        print("-" * 72)
        print(f"未声明字段号（可能对应 cf_points / 翻译标记等）: {undeclared}")

    if args.show:
        print("=" * 72)
        print(f"完整解析示例（前 {args.show} 条）:")
        for raw in records[: args.show]:
            p = proto.parse_contest_problem(raw)
            print(
                f"  name={p['name']!r} source={p['source_name']} "
                f"difficulty={p['difficulty_name']} rating={p['cf_rating']} "
                f"cf=({p['cf_contest_id']},{p['cf_index']}) tags={p['cf_tags']} "
                f"time_limit={p['time_limit_seconds']}s mem={p['memory_limit_bytes']} "
                f"public={len(p['public_tests'])} private={len(p['private_tests'])} "
                f"generated={len(p['generated_tests'])} "
                f"solutions={len(p['solutions'])} incorrect={len(p['incorrect_solutions'])} "
                f"py3={sum(1 for s in p['solutions'] if s['language'] == proto.LANGUAGE_PYTHON3)}"
            )

    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "shard": args.shard,
            "describe": info,
            "sources": dict(sources),
            "fields": {
                str(k): {
                    "count": v["count"],
                    "wire_types": v["wire_types"],
                    "samples": [[w, str(s)] for w, s in v["samples"]],
                    "mapped_to": declared.get(k),
                }
                for k, v in sorted(report.items())
            },
        }
        Path(args.json_out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"结果已写入 {args.json_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
