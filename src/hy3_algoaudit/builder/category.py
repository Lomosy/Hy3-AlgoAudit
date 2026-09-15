"""算法类别归类：把题目标签归到 6 个算法类别之一。

背景：项目需要按「算法类别 × 难度」做分层分析（技术方案书第十八、十九章），
因此每道题必须落到唯一一个类别上。标签有三个来源，优先级从高到低：

    1. cf_tags      Codeforces 官方标签（最权威，仅 CF 来源题有）
    2. taco         从 TACO 数据集关联到的 tags / skill_types
    3. heuristic    题面关键词启发式（仅在无标签时兜底）

类别定义取自技术方案书 15.2 节：动态规划、贪心、图算法、二分、数据结构、模拟与基础数学。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 6 个类别（顺序即归类优先级）
CATEGORIES = (
    "DP",
    "Graph",
    "DataStructure",
    "BinarySearch",
    "Greedy",
    "Simulation",
)

CATEGORY_NAMES_ZH = {
    "DP": "动态规划",
    "Graph": "图算法",
    "DataStructure": "数据结构",
    "BinarySearch": "二分",
    "Greedy": "贪心",
    "Simulation": "模拟与基础数学",
    "unclassified": "未归类",
}

# Codeforces 官方标签 → 类别
CF_TAG_TO_CATEGORY: dict[str, str] = {
    # DP
    "dp": "DP",
    # 图算法
    "graphs": "Graph",
    "trees": "Graph",
    "dfs and similar": "Graph",
    "shortest paths": "Graph",
    "dsu": "Graph",
    "flows": "Graph",
    "graph matchings": "Graph",
    "2-sat": "Graph",
    "bipartite matching": "Graph",
    "strongly connected components": "Graph",
    "topological sort": "Graph",
    # 数据结构
    "data structures": "DataStructure",
    "sqrt decomposition": "DataStructure",
    "sparse table": "DataStructure",
    "segment tree": "DataStructure",
    "fenwick": "DataStructure",
    "disjoint set union": "DataStructure",
    # 二分
    "binary search": "BinarySearch",
    "ternary search": "BinarySearch",
    # 贪心
    "greedy": "Greedy",
    # 模拟与基础数学
    "implementation": "Simulation",
    "brute force": "Simulation",
    "simulation": "Simulation",
    "math": "Simulation",
    "constructive algorithms": "Simulation",
    "strings": "Simulation",
    "sortings": "Simulation",
    "number theory": "Simulation",
    "combinatorics": "Simulation",
    "geometry": "Simulation",
    "games": "Simulation",
    "probabilities": "Simulation",
    "matrices": "Simulation",
    "bitmasks": "Simulation",
    "two pointers": "Simulation",
    "expression parsing": "Simulation",
    "schedules": "Simulation",
    "chinese remainder theorem": "Simulation",
    "fft": "Simulation",
    "meet-in-the-middle": "Simulation",
    "divide and conquer": "Simulation",
    "hashing": "Simulation",
    "interactive": "Simulation",
    "ternary": "BinarySearch",
}

# TACO 标签（tags / skill_types）→ 类别
# 键必须是小写：_from_tag_set() 会先把标签统一小写再查表。
# 词表来自实测 data/taco/taco_index.parquet 的全部 36 种 tag + 8 种 skill_type。
TACO_TAG_TO_CATEGORY: dict[str, str] = {
    # DP
    "dynamic programming": "DP",
    # 图算法
    "graph algorithms": "Graph",
    "graph traversal": "Graph",
    "directed graphs": "Graph",
    "shortest paths": "Graph",
    "paths and circuits": "Graph",
    "spanning trees": "Graph",
    "strong connectivity": "Graph",
    "flows and cuts": "Graph",
    "tree algorithms": "Graph",
    "graph theory": "Graph",
    "graph": "Graph",
    "tree": "Graph",
    "trees": "Graph",
    # 数据结构
    "data structures": "DataStructure",
    "data structure": "DataStructure",
    "range queries": "DataStructure",
    "segment trees revisited": "DataStructure",
    "square root algorithms": "DataStructure",
    "tree queries": "DataStructure",
    "amortized analysis": "DataStructure",
    # 二分
    "binary search": "BinarySearch",
    "searching": "BinarySearch",
    # 贪心
    "greedy algorithms": "Greedy",
    "greedy": "Greedy",
    # 模拟与基础数学
    "ad-hoc": "Simulation",
    "bit manipulation": "Simulation",
    "combinatorics": "Simulation",
    "complete search": "Simulation",
    "constructive algorithms": "Simulation",
    "divide and conquer": "Simulation",
    "fundamentals": "Simulation",
    "game theory": "Simulation",
    "geometry": "Simulation",
    "implementation": "Simulation",
    "mathematics": "Simulation",
    "math": "Simulation",
    "matrices": "Simulation",
    "number theory": "Simulation",
    "polynomials and generating functions": "Simulation",
    "preprocessing": "Simulation",
    "probability": "Simulation",
    "sorting": "Simulation",
    "string algorithms": "Simulation",
    "string": "Simulation",
    "strings": "Simulation",
    "sweep line algorithms": "Simulation",
    "simulation": "Simulation",
    "brute force": "Simulation",
    "recursion": "Simulation",
}

# 题面关键词启发式（仅在无任何标签时兜底）
HEURISTIC_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("DP", re.compile(r"\b(dynamic programming|dp\b|knapsack|subsequence|memoiz)", re.I)),
    ("Graph", re.compile(r"\b(graph|vertices|edges|tree|forest|dfs|bfs|shortest path|cycle)\b", re.I)),
    ("BinarySearch", re.compile(r"\b(binary search|bisect|monotonic|maximi[sz]e the minimum|minimi[sz]e the maximum)", re.I)),
    ("DataStructure", re.compile(r"\b(segment tree|fenwick|priority queue|heap|union[- ]find|stack|deque|balanced tree)\b", re.I)),
    ("Greedy", re.compile(r"\b(greedy|optimally|maximi[sz]e|minimi[sz]e)\b", re.I)),
)


@dataclass(frozen=True)
class CategoryAssignment:
    category: str
    source: str  # cf_tags | taco | heuristic | unclassified
    matched: tuple[str, ...] = ()


def _from_tag_set(tags: list[str]) -> tuple[str, tuple[str, ...]] | None:
    """按 CATEGORIES 的优先级，从标签集合中取出第一个命中的类别"""
    lowered = {t.strip().lower() for t in tags if t and t.strip()}
    if not lowered:
        return None
    for category in CATEGORIES:
        hits = tuple(sorted(t for t in lowered if _tag_to_category(t) == category))
        if hits:
            return category, hits
    return None


def _tag_to_category(tag: str) -> str | None:
    if tag in CF_TAG_TO_CATEGORY:
        return CF_TAG_TO_CATEGORY[tag]
    return TACO_TAG_TO_CATEGORY.get(tag)


def guess_from_text(text: str) -> CategoryAssignment | None:
    """仅用题面关键词猜类别（供无标签题目兜底，也可在扫描阶段预先算好）"""
    if not text:
        return None
    for category, pattern in HEURISTIC_PATTERNS:
        found = pattern.search(text)
        if found:
            return CategoryAssignment(category, "heuristic", (found.group(0).lower(),))
    return None


def classify(
    *,
    cf_tags: list[str] | None = None,
    taco_tags: list[str] | None = None,
    description: str = "",
    heuristic: CategoryAssignment | None = None,
) -> CategoryAssignment:
    """决定一道题的算法类别，并返回来源以便后续做按标签来源的消融分析。

    采用「并集判定 + 来源归因」：用 cf_tags 与 TACO 标签的**并集**按类别优先级
    取首要类别，再回看该类别是否能仅由 cf_tags 得出，以此决定 source。

    这样做的理由：两套标签体系粒度不同且互补 —— Codeforces 官方标签较粗
    （如仅 "math" + "implementation"），TACO 用的是更细的算法主题体系
    （如 "Segment trees revisited" / "Graph traversal" / "Amortized analysis"）。
    实测数据显示若严格按 cf_tags 优先，会丢掉 TACO 带来的类别信息，
    使 BinarySearch / DataStructure 这类稀缺单元更容易被饿死。

    优先级：cf_tags/taco 并集 > heuristic（题面关键词）> unclassified
    """
    cf_list = [t for t in (cf_tags or []) if t]
    taco_list = [t for t in (taco_tags or []) if t]
    merged = cf_list + taco_list

    if merged:
        hit = _from_tag_set(merged)
        if hit:
            category, matched = hit
            cf_hit = _from_tag_set(cf_list)
            source = "cf_tags" if (cf_hit and cf_hit[0] == category) else "taco"
            return CategoryAssignment(category, source, matched)

    if heuristic is not None:
        return heuristic

    guessed = guess_from_text(description)
    if guessed is not None:
        return guessed

    return CategoryAssignment("unclassified", "unclassified", ())


def all_matched_categories(*tag_lists: list[str] | None) -> list[str]:
    """返回所有命中的类别（一道题可能同时属于多个类别），供下游重新切片使用"""
    found: set[str] = set()
    for tags in tag_lists:
        if not tags:
            continue
        for tag in tags:
            category = _tag_to_category((tag or "").strip().lower())
            if category:
                found.add(category)
    return [c for c in CATEGORIES if c in found]
