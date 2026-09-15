"""题目池构建：扫描 CodeContests 原始数据，抽取轻量元数据并执行入池过滤。

流程分两趟，避免把 2.9 GB 解码结果驻留在内存：

    第一趟  scan_shard()  只保留元数据（题面长度、标签、测试统计），写 pool.jsonl
    第二趟  load_records() 只对最终入选的题目重新读取分片，取回题面与测试全文

题目 id 采用稳定可溯源的形式 `cc-{split}-{shard:03d}-{record:04d}`，
它编码了「在哪个分片的第几条记录」，因此第二趟可以直接定位分片。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator

import yaml

from . import category, proto, riegeli
from . import PROJECT_ROOT as _PROJECT_ROOT

_SHARD_SUFFIX = re.compile(r"-(\d{5})-of-(\d{5})$")

# 「答案不唯一」的信号句式。
#
# 为什么必须过滤：这类题在 Codeforces 上由特殊判题器（special judge）判定，
# 只要输出是任一合法方案即可 AC，而数据集只保存了**其中一个**参考答案。
# 用精确比对判题时，参考解会输出另一组合法方案而被误判成 WA。
# 实测 11 道抽样题里就撞上 1 例（1141_G Privatization of Roads in Treeland，
# 题面写 "If there are multiple ..."），比例不可忽略。
#
# 两条线索都保留：一是「可以输出任意解」的直白表述，二是「存在多个答案」的条件表述。
# 措辞校准记录（第一版过宽，实测会误杀约 1/3 的命中）：
#   "in any order"   → 误杀："The spells can be used any number of times in any order."
#                      （说的是输入里操作的使用顺序，答案唯一）
#   "does not matter"→ 误杀："the point where Wabbit ends up at does not matter."
#                      （说的是无关紧要的细节，不是「输出不唯一」）
# 因此这两条被移除，其余全部收紧为「必须出现在 print / output 的输出说明语境里」。
# 召回率下降由后续「参考解实测」关卡兜住：漏网的题会因参考解跑不通过而被淘汰。
MULTIPLE_ANSWER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("print_any", re.compile(r"\bprint\s+any\b", re.I)),
    ("output_any", re.compile(r"\boutput\s+any\b", re.I)),
    ("any_of_them", re.compile(r"\b(?:print|output)\s+any\s+of\s+them\b", re.I)),
    ("any_valid_answer", re.compile(
        r"\bany\s+(?:valid|correct|suitable)\s+"
        r"(?:answer|solution|way|sequence|permutation|order|triple|set|arrangement)\b",
        re.I,
    )),
    ("any_permutation", re.compile(
        r"\b(?:print|output)\s+any\s+(?:permutation|sequence|arrangement)\b", re.I
    )),
    ("multiple_answers", re.compile(
        r"\bmultiple\s+(?:answers|solutions|ways|possible\s+answers)\b", re.I
    )),
    ("several_answers", re.compile(
        r"\b(?:if|when)\s+there\s+are\s+(?:several|multiple|many|more\s+than\s+one)\s+"
        r"(?:\w+\s+){0,3}?(?:answers|solutions|ways|options|variants)\b",
        re.I,
    )),
    ("any_order_output", re.compile(
        r"\b(?:print|output)\b[^.\n]{0,100}?\bin\s+any\s+(?:arbitrary\s+)?order\b", re.I
    )),
)


def detect_multiple_answers(description: str) -> list[str]:
    """返回命中的「答案不唯一」句式名；为空说明题面没有这类信号"""
    return [name for name, pattern in MULTIPLE_ANSWER_PATTERNS if pattern.search(description)]


# 「交互题」信号。交互题的输入是**运行时产生的**（程序提问、判题器回答），
# 静态测试用例根本无从表达，因此必须先排除，否则只会白白消耗验证预算，
# 更糟的是可能给出无意义的判定。
#
# 实测教训一：1146_C Tree Diameter 题面**没有任何 "interactive" 字样**，
# 只有 6 处 flush —— 说明「等 interactive 这个词」是不够的。题面要求 flush
# 只可能出现在交互题里（非交互题没有任何东西需要刷新），故 flush 本身足够强。
#
# 实测教训二（抽样 630 条原始记录校准）：最初还写了两条「query 句式」信号
# （`ask/make a query`、`at most N queries`），结果命中的 3 条**全是误杀**：
#     117_D  "You should print the query results modulo mod"   ← 普通离线题
#     673_F  "Your task is to handle q queries of three types"  ← 普通离线题
# 这两条因此被删掉 —— 「query」在离线题里是常见名词（查询操作），不具备判别力。
# 保留的两条信号在同一批样本里 7/7 全部是真交互题。
INTERACTIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("interactive_keyword", re.compile(r"\binteractive\b", re.I)),
    ("flush_output", re.compile(r"\bflush(?:es|ing|ed)?\b", re.I)),
)


def detect_interactive(description: str) -> list[str]:
    """返回命中的「交互题」信号名；为空说明是普通（stdio 一次性输入）题目"""
    text = description or ""
    return [name for name, pattern in INTERACTIVE_PATTERNS if pattern.search(text)]


# ---------------------------------------------------------------- 配置
def load_config(path: str | Path | None = None) -> dict[str, Any]:
    cfg_path = Path(path) if path else _PROJECT_ROOT / "configs" / "dataset.yaml"
    with cfg_path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


# ---------------------------------------------------------------- 分片
@dataclass(frozen=True)
class Shard:
    split: str
    index: int
    path: Path

    @property
    def num_records_hint(self) -> str:
        return self.path.name


def discover_shards(
    cc_dir: str | Path,
    *,
    splits: tuple[str, ...] = ("train", "valid", "test"),
    max_train_shards: int | None = None,
) -> list[Shard]:
    """列出数据集中全部 riegeli 分片；max_train_shards 可用于小样本先行验证"""
    root = Path(cc_dir)
    shards: list[Shard] = []
    for split in splits:
        files = sorted(root.glob(f"code_contests_{split}.riegeli*"))
        if split == "train" and max_train_shards is not None:
            files = files[:max_train_shards]
        for path in files:
            matched = _SHARD_SUFFIX.search(path.name)
            index = int(matched.group(1)) if matched else 0
            shards.append(Shard(split=split, index=index, path=path))
    return shards


def make_cc_id(split: str, shard: int, record_index: int) -> str:
    return f"cc-{split}-{shard:03d}-{record_index:04d}"


def parse_cc_id(cc_id: str) -> tuple[str, int, int]:
    _, split, shard, record = cc_id.split("-")
    return split, int(shard), int(record)


# ---------------------------------------------------------------- 元数据与过滤
def _byte_len(text: str) -> int:
    return len(text.encode("utf-8"))


def build_meta(
    shard: Shard, record_index: int, problem: dict[str, Any], cfg: dict[str, Any]
) -> dict[str, Any]:
    """把一条解析后的题目压缩成轻量元数据，并判定是否通过入池门槛"""
    filters = cfg["filters"]

    time_limit = problem["time_limit_seconds"]
    time_limit_source = "dataset" if time_limit else "default"
    if not time_limit:
        time_limit = float(filters["default_time_limit_seconds"])

    memory_limit = problem["memory_limit_bytes"] or filters["default_memory_limit_bytes"]

    cap = filters["max_test_field_bytes"]
    retainable: Counter[str] = Counter()
    for origin in ("public", "private", "generated"):
        for case in problem[f"{origin}_tests"]:
            if _byte_len(case["input"]) <= cap and _byte_len(case["output"]) <= cap:
                retainable[origin] += 1
    total_retainable = sum(retainable.values())

    py3_solutions = [
        s for s in problem["solutions"] if s["language"] == proto.LANGUAGE_PYTHON3
    ]
    heuristic = category.guess_from_text(problem["description"])
    multiple_answer_hits = detect_multiple_answers(problem["description"])
    interactive_hits = detect_interactive(problem["description"])

    reasons: list[str] = []
    if problem["source"] in filters["exclude_sources"]:
        reasons.append("source_excluded")
    if filters["require_python3_solution"] and not py3_solutions:
        reasons.append("no_python3_solution")
    if filters["require_stdio"] and (problem["input_file"] or problem["output_file"]):
        reasons.append("non_stdio")
    if filters.get("exclude_multiple_answers") and multiple_answer_hits:
        reasons.append("multiple_answers")
    if filters.get("exclude_interactive") and interactive_hits:
        reasons.append("interactive")
    if time_limit > float(filters["max_time_limit_seconds"]):
        reasons.append("time_limit_too_large")
    if total_retainable < int(filters["min_total_tests"]):
        reasons.append("too_few_tests")
    if sum(
        len(problem[f"{o}_tests"]) for o in ("public", "private", "generated")
    ) > int(filters["max_total_tests"]):
        reasons.append("too_many_tests")
    if _byte_len(problem["description"]) > int(filters["max_description_bytes"]):
        reasons.append("description_too_large")

    return {
        "cc_id": make_cc_id(shard.split, shard.index, record_index),
        "split": shard.split,
        "shard": shard.index,
        "record_index": record_index,
        "name": problem["name"],
        "source": problem["source"],
        "source_name": problem["source_name"],
        "difficulty": problem["difficulty"],
        "difficulty_name": problem["difficulty_name"],
        "cf_contest_id": problem["cf_contest_id"],
        "cf_index": problem["cf_index"],
        "cf_rating": problem["cf_rating"],
        "cf_tags": problem["cf_tags"],
        "time_limit_seconds": time_limit,
        "time_limit_source": time_limit_source,
        "memory_limit_bytes": memory_limit,
        "memory_limit_source": "dataset" if problem["memory_limit_bytes"] else "default",
        "n_public": len(problem["public_tests"]),
        "n_private": len(problem["private_tests"]),
        "n_generated": len(problem["generated_tests"]),
        "n_retainable_public": retainable["public"],
        "n_retainable_private": retainable["private"],
        "n_retainable_generated": retainable["generated"],
        "n_solutions": len(problem["solutions"]),
        "n_python3_solutions": len(py3_solutions),
        "n_incorrect_solutions": problem["n_incorrect_solutions"],
        "description_bytes": _byte_len(problem["description"]),
        "heuristic_category": heuristic.category if heuristic else None,
        "heuristic_source": heuristic.source if heuristic else None,
        "heuristic_matched": list(heuristic.matched) if heuristic else [],
        "multiple_answer_hits": multiple_answer_hits,
        "interactive_hits": interactive_hits,
        "passed": not reasons,
        "reject_reasons": reasons,
    }


def scan_shard(
    shard: Shard, cfg: dict[str, Any]
) -> Iterator[dict[str, Any]]:
    """扫描一个分片，产出通过/未通过门槛的元数据（未通过的也保留，用于统计）"""
    for record_index, raw in enumerate(riegeli.iter_records(shard.path)):
        try:
            problem = proto.parse_contest_problem(raw)
        except proto.ProtoError:
            yield {
                "cc_id": make_cc_id(shard.split, shard.index, record_index),
                "split": shard.split,
                "shard": shard.index,
                "record_index": record_index,
                "passed": False,
                "reject_reasons": ["proto_error"],
            }
            continue
        yield build_meta(shard, record_index, problem, cfg)


def build_pool(
    shards: list[Shard],
    cfg: dict[str, Any],
    out_path: str | Path,
    *,
    verbose: bool = True,
) -> dict[str, Any]:
    """扫描全部分片，把元数据写入 pool.jsonl，并返回统计摘要"""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    stats: dict[str, Any] = {
        "shards": len(shards),
        "records": 0,
        "passed": 0,
        "reject_reasons": Counter(),
        "by_source": Counter(),
        "by_split": Counter(),
    }

    with out.open("w", encoding="utf-8") as handle:
        for shard in shards:
            count = 0
            kept = 0
            for meta in scan_shard(shard, cfg):
                count += 1
                if meta.get("passed"):
                    kept += 1
                    stats["passed"] += 1
                    stats["by_source"][meta["source_name"]] += 1
                    stats["by_split"][meta["split"]] += 1
                else:
                    stats["reject_reasons"].update(meta.get("reject_reasons", []))
                handle.write(json.dumps(meta, ensure_ascii=False) + "\n")
            stats["records"] += count
            if verbose:
                print(
                    f"  {shard.path.name:<46} 记录 {count:>5}  通过 {kept:>5}",
                    flush=True,
                )

    stats["reject_reasons"] = dict(stats["reject_reasons"])
    stats["by_source"] = dict(stats["by_source"])
    stats["by_split"] = dict(stats["by_split"])
    return stats


def load_pool(path: str | Path, *, only_passed: bool = True) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if only_passed and not row.get("passed"):
                continue
            rows.append(row)
    return rows


def load_records(
    cc_dir: str | Path,
    cc_ids: list[str],
    *,
    splits: tuple[str, ...] = ("train", "valid", "test"),
) -> dict[str, dict[str, Any]]:
    """第二趟：只对指定题目重新扫描所需分片，取回题面与测试全文"""
    wanted: dict[tuple[str, int], set[int]] = {}
    for cc_id in cc_ids:
        split, shard, record = parse_cc_id(cc_id)
        wanted.setdefault((split, shard), set()).add(record)

    targets = set(wanted)
    found: dict[str, dict[str, Any]] = {}
    for shard in discover_shards(cc_dir, splits=splits):
        key = (shard.split, shard.index)
        if key not in targets:
            continue
        needed = set(wanted[key])
        for record_index, raw in enumerate(riegeli.iter_records(shard.path)):
            if record_index not in needed:
                continue
            problem = proto.parse_contest_problem(raw, keep_incorrect_solutions=False)
            found[make_cc_id(shard.split, shard.index, record_index)] = problem
            needed.discard(record_index)
            if not needed:
                break
    missing = [cc_id for cc_id in cc_ids if cc_id not in found]
    if missing:
        raise RuntimeError(f"以下题目未能从原始数据中取回：{missing[:5]}（共 {len(missing)} 条）")
    return found


def summarize_pool(pool: list[dict[str, Any]]) -> dict[str, Any]:
    """对通过门槛的题目池做分布统计，用于判断采样是否可行"""
    by_category = Counter()
    by_bin = Counter()
    cells = Counter()
    for row in pool:
        assignment = category.classify(
            cf_tags=row.get("cf_tags"),
            heuristic=_saved_heuristic(row),
        )
        bin_name = rating_bin(row.get("cf_rating"), row)
        by_category[assignment.category] += 1
        by_bin[bin_name] += 1
        cells[(assignment.category, bin_name)] += 1
    return {
        "total": len(pool),
        "by_category": dict(by_category),
        "by_rating_bin": dict(by_bin),
        "cells": {f"{c}@{b}": n for (c, b), n in sorted(cells.items())},
    }


def _saved_heuristic(row: dict[str, Any]) -> category.CategoryAssignment | None:
    if not row.get("heuristic_category"):
        return None
    return category.CategoryAssignment(
        row["heuristic_category"],
        row.get("heuristic_source") or "heuristic",
        tuple(row.get("heuristic_matched") or ()),
    )


def rating_bin(rating: int | None, row: dict[str, Any], cfg: dict[str, Any] | None = None) -> str:
    """把题目映射到难度箱；非 CF 题目用 Difficulty 枚举回落到近似箱"""
    bins = (
        cfg["sampling"]["rating_bins"]
        if cfg
        else [
            [0, 1200, "<1200"],
            [1200, 1600, "1200-1599"],
            [1600, 2000, "1600-1999"],
            [2000, 2400, "2000-2399"],
            [2400, 100000, ">=2400"],
        ]
    )
    if rating:
        for low, high, name in bins:
            if low <= rating < high:
                return name
        return bins[-1][2]
    # 无 cf_rating：用 Difficulty 字母/枚举粗略映射
    difficulty = row.get("difficulty", 0)
    if 1 <= difficulty <= 2:
        return bins[0][2]
    if difficulty in (3, 4, 5, 6):
        return bins[2][2]
    if 7 <= difficulty <= 11:  # A..E
        return bins[1][2] if difficulty <= 9 else bins[2][2]
    if difficulty >= 12:  # F 及以上
        return bins[3][2]
    return "unknown"


def meta_asdict(meta: dict[str, Any]) -> dict[str, Any]:
    return asdict(meta) if hasattr(meta, "__dataclass_fields__") else meta
