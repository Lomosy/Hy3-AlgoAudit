"""分层采样：在「算法类别 × 难度箱」矩阵上按配额抽取题目。

设计要点：
    1. 一道题可能同时命中多个算法标签（实测 199 题中有 72 题跨类别），
       如果只用「首要类别」会把稀缺单元（如 BinarySearch）饿死。
       因此先按首要类别统计供给，再在**缺口填充阶段**放宽到所有命中类别。
    2. 难度优先用 cf_rating（精确），无 rating 时回落到 TACO difficulty、
       再回落到 CodeContests 的 Difficulty 枚举字母，并记录来源以便追溯。
    3. 全部随机性来自单个 random.Random(seed)，单元按排序后顺序遍历，
       保证同 seed + 同输入必得同结果。
"""

from __future__ import annotations

import random
from collections import Counter, defaultdict
from typing import Any

from . import category


# ---------------------------------------------------------------- 难度分层
TACO_DIFFICULTY_BIN = {
    "EASY": 0,
    "MEDIUM": 1,
    "MEDIUM_HARD": 2,
    "HARD": 2,
    "VERY_HARD": 4,
    "HARDER": 3,
    "HARDEST": 3,
}

# CodeContests Difficulty 字母 → 难度箱下标（粗略映射，仅作兜底）
CC_ENUM_BIN_RANGE = ((7, 9, 1), (10, 12, 2), (13, 16, 3), (17, 28, 4))


def bins_of(cfg: dict[str, Any]) -> list[tuple[int, int, str]]:
    return [(int(low), int(high), name) for low, high, name in cfg["sampling"]["rating_bins"]]


def assign_difficulty_bin(row: dict[str, Any], cfg: dict[str, Any]) -> tuple[str, str]:
    """返回 (难度箱名, 来源)。unrated 表示无法可靠分层，不参与配额矩阵。"""
    bins = bins_of(cfg)

    rating = row.get("cf_rating")
    if rating:
        for low, high, name in bins:
            if low <= rating < high:
                return name, "cf_rating"

    taco_difficulty = (row.get("taco_difficulty") or "").strip().upper()
    if taco_difficulty in TACO_DIFFICULTY_BIN:
        return bins[TACO_DIFFICULTY_BIN[taco_difficulty]][2], "taco"

    difficulty = row.get("difficulty", 0)
    if 1 <= difficulty <= 2:  # EASY / MEDIUM
        return bins[0][2], "cc_enum"
    if 3 <= difficulty <= 6:  # HARD / HARDER / HARDEST / EXTERNAL
        return bins[2][2], "cc_enum"
    for low, high, index in CC_ENUM_BIN_RANGE:
        if low <= difficulty <= high:
            return bins[index][2], "cc_enum"

    return "unrated", "none"


# ---------------------------------------------------------------- 归类
def classify_row(row: dict[str, Any]) -> tuple[category.CategoryAssignment, list[str]]:
    """返回 (首要类别归属, 全部命中类别)"""
    heuristic = None
    if row.get("heuristic_category"):
        heuristic = category.CategoryAssignment(
            row["heuristic_category"],
            row.get("heuristic_source") or "heuristic",
            tuple(row.get("heuristic_matched") or ()),
        )
    taco_tags = list(row.get("taco_tags") or []) + list(row.get("taco_skill_types") or [])
    assignment = category.classify(
        cf_tags=row.get("cf_tags"),
        taco_tags=taco_tags,
        heuristic=heuristic,
    )
    matched = category.all_matched_categories(row.get("cf_tags"), taco_tags)
    if assignment.category != "unclassified" and assignment.category not in matched:
        matched.insert(0, assignment.category)
    return assignment, matched


def prepare(rows: list[dict[str, Any]], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """给每道题补上类别、难度箱等采样所需字段（就地修改并返回）"""
    for row in rows:
        assignment, matched = classify_row(row)
        bin_name, bin_source = assign_difficulty_bin(row, cfg)
        row["primary_category"] = assignment.category
        row["category_source"] = assignment.source
        row["category_matched"] = list(assignment.matched)
        row["categories_all"] = matched
        row["difficulty_bin"] = bin_name
        row["difficulty_bin_source"] = bin_source
    return rows


# ---------------------------------------------------------------- 配额与抽样
def allocate_quota(
    non_empty: list[tuple[str, str]],
    supply: dict[tuple[str, str], int],
    target_n: int,
    *,
    min_per_cell: int,
    max_per_category: int,
) -> dict[tuple[str, str], int]:
    """在「类别 × 难度箱」单元上分配配额。

    分配规则（全部确定性，不依赖字典遍历顺序）：
        1. 若预算充足（target_n ≥ 单元数 × min_per_cell），先给每个非空单元
           min_per_cell 的保底配额，保证稀缺类别（如 BinarySearch）不被饿死；
           小样本场景下自动下调保底值（effective_min），避免保底本身就超额。
        2. 剩余预算按「各单元剩余供给量」用最大余额法比例分配，
           余数按（小数部分降序、单元名升序）确定性地补齐。
        3. 分配全程受 max_per_category 上限约束；触顶类别让出的份额
           回流给其它类别，反复直到无人在意。
    """
    quota: dict[tuple[str, str], int] = {cell: 0 for cell in non_empty}
    if not non_empty or target_n <= 0:
        return quota

    effective_min = max(1, min(min_per_cell, target_n // len(non_empty)))

    def category_total(cat: str) -> int:
        return sum(count for (c, _), count in quota.items() if c == cat)

    def headroom(cell: tuple[str, str]) -> int:
        if category_total(cell[0]) >= max_per_category:
            return 0
        return max(0, supply[cell] - quota[cell])

    # --- 第一步：保底配额（同样受类别上限约束）---
    if target_n >= len(non_empty) * effective_min:
        for cell in non_empty:
            if category_total(cell[0]) >= max_per_category:
                continue
            quota[cell] = min(effective_min, supply[cell], max_per_category)

    # --- 第一步补：类别保底 —— 每个有货的类别至少 1 题 ---
    # 没有这一层时，按供给比例分配会把稀缺类别（如 BinarySearch，占比约 6%）
    # 在小样本上四舍五入成 0，导致「类别覆盖」这项设计要求在样本上落空。
    present = sorted({cell[0] for cell in non_empty})
    if target_n >= len(present):
        for cat in present:
            if category_total(cat) > 0:
                continue
            # 选该类别下剩余供给最多的单元，并遵守总量上限
            best = max(
                (cell for cell in non_empty if cell[0] == cat),
                key=lambda c: (supply[c] - quota[c], c),
            )
            room = min(supply[best] - quota[best], max_per_category - category_total(cat))
            if room > 0 and sum(quota.values()) < target_n:
                quota[best] += 1

    # --- 第二步：按剩余供给比例分配（最大余额法）+ 类别上限 ---
    remaining = target_n - sum(quota.values())
    guard = 0
    while remaining > 0 and guard < 1000:
        guard += 1
        flexible = [cell for cell in non_empty if headroom(cell) > 0]
        if not flexible:
            break
        total_headroom = sum(headroom(cell) for cell in flexible)
        seats = min(remaining, total_headroom)

        # 理想配额 = seats × 该单元剩余供给 / 总剩余供给
        ideal = {cell: seats * headroom(cell) / total_headroom for cell in flexible}
        gains = {cell: int(ideal[cell]) for cell in flexible}
        left = seats - sum(gains.values())
        if left > 0:
            order = sorted(flexible, key=lambda c: (-(ideal[c] - gains[c]), c))
            for cell in order[:left]:
                gains[cell] += 1

        # 本轮一次性分配可能让某个类别整体越过上限，需按类别裁剪，
        # 被裁掉的部分留到下一轮重新分配（remaining 未归零，while 会继续）。
        # 注意：quota 是**增量更新**的，category_total() 读到的已是本轮最新值，
        # 因此这里不能再额外减去「本轮已给该类别多少」——早期版本两处都减，
        # 导致同类别的额度被扣两次，可分配总量凭空缩水（实测 8 题只选出 6 题）。
        progressed = False
        for cell in non_empty:
            gain = gains.get(cell, 0)
            if gain <= 0:
                continue
            room = max_per_category - category_total(cell[0])
            gain = min(gain, max(0, room))
            if gain <= 0:
                continue
            quota[cell] += gain
            remaining -= gain
            progressed = True
        if not progressed:
            break

    return quota


def plan_and_sample(
    rows: list[dict[str, Any]], cfg: dict[str, Any], *, n: int | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """按「类别 × 难度箱」配额抽样，返回 (入选题目, 诊断信息)"""
    sampling = cfg["sampling"]
    target_n = int(n if n is not None else cfg["dataset"]["n_problems"])
    categories = list(sampling["categories"])
    bin_names = [name for _, _, name in bins_of(cfg)]
    min_per_cell = int(sampling["min_per_cell"])
    rng = random.Random(cfg["dataset"]["seed"])

    eligible = [
        row
        for row in rows
        if row["primary_category"] in categories and row["difficulty_bin"] in bin_names
    ]
    # 供给要按「放宽后的候选池」统计，而不是只看首要类别。
    # 原因：一道题常同时命中多个类别（实测约 1/3），若配额只看首要类别，
    # Graph@1600-1999 这类没有「首要供给」的单元就永远分不到配额，
    # 而放宽后可选的题其实存在 —— 表现为「配额总数低于目标题数」。
    supply_primary = Counter(
        (row["primary_category"], row["difficulty_bin"]) for row in eligible
    )
    supply_wide: Counter[tuple[str, str]] = Counter()
    for row in eligible:
        for cat in row["categories_all"]:
            if cat in categories:
                supply_wide[(cat, row["difficulty_bin"])] += 1
    supply = supply_wide
    non_empty = sorted(cell for cell, count in supply.items() if count > 0)

    diagnostics: dict[str, Any] = {
        "target_n": target_n,
        "eligible": len(eligible),
        "excluded_unclassified": sum(1 for r in rows if r["primary_category"] not in categories),
        "excluded_unrated": sum(1 for r in rows if r["difficulty_bin"] not in bin_names),
        "non_empty_cells": len(non_empty),
        "supply": {f"{c}@{b}": supply[(c, b)] for c, b in non_empty},
        "supply_primary": {
            f"{c}@{b}": supply_primary[(c, b)]
            for c, b in sorted(cell for cell in supply_primary if supply_primary[cell] > 0)
        },
    }

    if not non_empty:
        return [], diagnostics

    max_per_category = max(
        1, int(target_n * float(sampling.get("max_per_category_share", 1.0)))
    )
    quota = allocate_quota(
        non_empty,
        supply,
        target_n,
        min_per_cell=min_per_cell,
        max_per_category=max_per_category,
    )

    effective_min = max(1, min(min_per_cell, target_n // len(non_empty)))
    diagnostics["effective_min_per_cell"] = effective_min
    diagnostics["max_per_category"] = max_per_category
    diagnostics["quota"] = {f"{c}@{b}": quota[(c, b)] for c, b in non_empty}
    diagnostics["total_quota"] = sum(quota.values())
    diagnostics["under_min_cells"] = [
        f"{c}@{b}" for c, b in non_empty if 0 < quota[(c, b)] < effective_min
    ]
    diagnostics["zero_quota_cells"] = [
        f"{c}@{b}" for c, b in non_empty if quota[(c, b)] == 0
    ]

    # 按单元确定性抽取；候选放宽到「所有命中该类别」的题目，避免稀缺单元饿死
    selected: list[dict[str, Any]] = []
    used: set[str] = set()
    cell_actual: Counter[tuple[str, str]] = Counter()

    for cell in non_empty:
        want = quota[cell]
        if want <= 0:
            continue
        cat, bin_name = cell
        candidates = [
            row
            for row in eligible
            if row["difficulty_bin"] == bin_name
            and cat in row["categories_all"]
            and row["cc_id"] not in used
        ]
        candidates.sort(key=lambda r: r["cc_id"])
        rng.shuffle(candidates)
        for row in candidates[:want]:
            used.add(row["cc_id"])
            picked = dict(row)
            picked["quota_cell"] = f"{cat}@{bin_name}"
            picked["quota_cell_primary"] = row["primary_category"] == cat
            selected.append(picked)
            cell_actual[cell] += 1

    # 兜底填充：放宽供给统计后，配额是「期望值」而非硬约束（一道题可能被多个
    # 单元同时计入供给）。第一轮抽完若有缺口，就用剩余未选题按「首要类别所属
    # 单元」补上，直到达到目标或题池耗尽。补进来的题不占配额单元的统计，
    # 因此在 diagnostics 里单独记为 filled_beyond_quota，避免混淆两者的口径。
    quota_total = sum(quota.values())
    filled_beyond_quota = 0
    if len(selected) < target_n:
        leftovers = [row for row in eligible if row["cc_id"] not in used]
        leftovers.sort(key=lambda r: r["cc_id"])
        rng.shuffle(leftovers)
        for row in leftovers:
            if len(selected) >= target_n:
                break
            used.add(row["cc_id"])
            picked = dict(row)
            picked["quota_cell"] = (
                f"{row['primary_category']}@{row['difficulty_bin']}"
            )
            picked["quota_cell_primary"] = True
            picked["filled_beyond_quota"] = True
            selected.append(picked)
            cell_actual[(row["primary_category"], row["difficulty_bin"])] += 1
            filled_beyond_quota += 1

    selected.sort(key=lambda r: r["cc_id"])
    diagnostics["selected"] = len(selected)
    diagnostics["quota_total"] = quota_total
    diagnostics["filled_beyond_quota"] = filled_beyond_quota
    diagnostics["actual_cells"] = {
        f"{c}@{b}": cell_actual[(c, b)] for c, b in sorted(cell_actual)
    }
    diagnostics["actual_by_category"] = dict(
        Counter(r["quota_cell"].split("@")[0] for r in selected)
    )
    return selected, diagnostics


def format_matrix(diagnostics: dict[str, Any], cfg: dict[str, Any]) -> str:
    """把「类别 × 难度箱」实得计数渲染成表格文本，便于写进报告"""
    categories = list(cfg["sampling"]["categories"])
    bin_names = [name for _, _, name in bins_of(cfg)]
    actual = diagnostics.get("actual_cells", {})
    lines = [
        f"{'类别':<14}" + "".join(f"{b:>12}" for b in bin_names) + f"{'合计':>8}",
        "-" * (14 + 12 * len(bin_names) + 8),
    ]
    for cat in categories:
        counts = [int(actual.get(f"{cat}@{b}", 0)) for b in bin_names]
        lines.append(f"{cat:<14}" + "".join(f"{c:>12}" for c in counts) + f"{sum(counts):>8}")
    totals = [
        sum(int(actual.get(f"{cat}@{b}", 0)) for cat in categories) for b in bin_names
    ]
    lines.append("-" * (14 + 12 * len(bin_names) + 8))
    lines.append(f"{'合计':<14}" + "".join(f"{t:>12}" for t in totals) + f"{sum(totals):>8}")
    return "\n".join(lines)
