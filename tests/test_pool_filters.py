"""Pool filter calibration tests.

Locks in the two filter tables that were calibrated against real data —
re-adding the noisy patterns would silently drop good problems.
"""
from hy3_algoaudit.builder import pool

# --- 交互题信号 -------------------------------------------------------------

INTERACTIVE_REAL = [
    # 没有 "interactive" 字样，只有 flush（1146_C 的教训）
    "print the answer and flush the output after each query.",
    # 明确的交互声明
    "This is an interactive problem. The interactor will respond to your queries.",
    # 1179_E：Interaction 段落 + flush
    'To ask f_i(x), print symbol "?" and then two integers. '
    "Note, you must flush your output to get a response.",
]

INTERACTIVE_LOOKALIKES = [
    # 117_D：query 是普通名词，题目完全离线
    "You are given m queries (l, r, u, v). You should print the query results modulo mod.",
    # 673_F：q 次查询是待处理的操作，不是向判题器提问
    "Your task is to handle q queries of three types: 1 i j, 2 i, 3 i.",
    # 普通离线题，压根没有 query/flush
    "Print the maximum subarray sum.",
]


def test_detects_real_interactive_problems():
    for text in INTERACTIVE_REAL:
        assert pool.detect_interactive(text), f"漏判交互题: {text[:40]}"


def test_query_sentences_are_not_interactive_signals():
    """`query` 在离线题里是常见名词 —— 不得凭它判定交互题。"""
    for text in INTERACTIVE_LOOKALIKES:
        assert not pool.detect_interactive(text), f"误杀离线题: {text[:40]}"


def test_empty_description_is_safe():
    assert pool.detect_interactive("") == []
    assert pool.detect_interactive(None) == []


# --- 多解题信号（回归） ------------------------------------------------------
def test_multiple_answer_signals_kept_tight():
    assert pool.detect_multiple_answers("If there are several answers, print any of them.")
    assert pool.detect_multiple_answers("Print any valid answer.")
    # 这两条曾在第一版里造成 817 道题被误杀，必须保持不命中
    assert not pool.detect_multiple_answers(
        "The spells can be used any number of times in any order.")
    assert not pool.detect_multiple_answers(
        "the point where Wabbit ends up at does not matter.")
