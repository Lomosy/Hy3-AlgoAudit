"""候选过程构造：为每道题生成「正确 / 自然 / 人工注入错误 / AC-but-Invalid」四类候选。

对应技术方案书 15.3。四类候选各自的用途：

    correct      正确题解 + AC 实现        → 测误报率（False Positive Rate）
    natural      Hy3 自然输出              → 研究模型自发错误
    injected     在指定步骤注入指定错误类型 → 首错定位准确率、错误分类准确率
    ac_invalid   代码保持 AC 但过程不成立   → AC-but-Invalid 识别率

关键设计：**只重写目标步骤及其后续，前面的步骤逐字节保留原样**。
具体做法是让模型只输出从目标步骤开始的内容，然后由程序把「原本正确的前缀」
与「模型输出的后缀」拼接起来。这样：

    1. 首个错误步骤是**程序保证**的，不依赖模型自我声明，标注天然可信；
    2. 目标步骤之前的内容与正确候选完全一致，不同候选之间可做严格对照实验
       （首错定位的评测因此是干净的 A/B 对比）；
    3. 模型即使不听话多输出了别的内容，也不会污染前缀。

一个已知的取舍：要让「后面的步骤与错误保持一致」需要模型真的顺着错误写下去，
这比「只改一节、后面照旧」更接近真实模型输出，也更难注入成功。注入成功的
判据是「S7 的代码不再是 AC 或结构不完整」，失败则丢弃该候选并记录原因。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import SandboxLimits
from ..sandbox import LocalSandbox
from ..sandbox.judge import DEFAULT_COMPARISON, judge_program
from ..schemas import CodeBlock, TestCase
from .._legacy.solver.schema import STEP_KEYS, STEP_TITLES, Solution, extract_code
from .._legacy.solver.solver import SECTION_RE, parse_solution

# ---------------------------------------------------------------- 候选类型
KIND_CORRECT = "correct"
KIND_NATURAL = "natural"
KIND_INJECTED = "injected"
KIND_AC_INVALID = "ac_invalid"

PUBLIC_KINDS = (KIND_CORRECT, KIND_NATURAL)
PRIVATE_KINDS = (KIND_INJECTED, KIND_AC_INVALID)

# ---------------------------------------------------------------- 错误分类体系
# 出处：技术方案书第九章。这里额外给出「该错误最容易出现在哪一步」，
# 注入时由它决定目标步骤；多个错误类型落到同一步是正常的（如 E6/E7 都在 S7）。
ERROR_TYPE_TITLE = {
    "E1": "题意理解错误",
    "E2": "算法选择错误",
    "E3": "推导或逻辑错误",
    "E4": "条件或边界遗漏",
    "E5": "复杂度错误",
    "E6": "题解与代码不一致",
    "E7": "实现错误",
    "E8": "无依据推断或幻觉",
}
ERROR_TYPE_STEP = {
    "E1": "S1",
    "E2": "S3",
    "E3": "S4",
    "E4": "S6",
    "E5": "S5",
    "E6": "S7",
    "E7": "S7",
    "E8": "S2",
}

# 每种错误类型的具体注入指令。
# 写清「错成什么样」比只给错误名称有效得多：模型看到「E2 算法选择错误」
# 容易生成一个含糊的描述，看到具体指令才会真的选错算法并顺着推下去。
ERROR_TYPE_ACTION = {
    "E1": (
        "把题目要求理解偏：例如把「求最小值」当成「求最大值」、把一个维度"
        "看漏（如忘记是无向边 / 忽略下标从 0 开始）、或误以为可以重复选取。"
        "后续必须按这个偏离的理解推进，不要回头纠正。"
    ),
    "E2": (
        "选一个看似合理但实际不适用于本题的算法：例如该用贪心却去做 DP、"
        "该用最短路却用并查集，并说明为什么选它。后续按这个算法展开推导与实现。"
    ),
    "E3": (
        "在推导中植入一个不成立的逻辑跳跃：例如由「局部最优」直接推出"
        "「全局最优」、交换两个不等价命题、或在归纳步中漏掉一种情形。"
    ),
    "E4": (
        "漏掉一个关键边界条件：例如忘记处理 n=1、空集、全部元素相同、"
        "取模后为 0 等情形，并据此给出边界论证。"
    ),
    "E5": (
        "给出错误的时间复杂度：即使实际做法是 O(n^2) 也声称是 O(n log n)，"
        "或用错误的方式统计运算次数。后续按这个（错误的）复杂度结论推进。"
    ),
    "E6": (
        "让代码与前面的题解描述不一致：描述里说用算法 A，代码里却实现了 B；"
        "或描述中的公式与代码中的公式不同。代码本身仍要看起来合理。"
    ),
    "E7": (
        "植入一个实现层面的 bug：例如下标越界、变量写错、循环边界差一、"
        "初始值错误、或漏掉一次必要的更新。bug 要隐蔽，不要写明显会崩的代码。"
    ),
    "E8": (
        "引入一个听起来合理但实际不成立的关键观察，并把它当作事实继续推进："
        "例如断言「最优解一定具有单调性」却给不出依据、或凭空引用一个不存在的定理。"
    ),
}

# AC-but-Invalid 的注入口味：代码不动，只让过程在某一步不成立
AC_INVALID_ACTION = {
    "S2": ("E8", "引入一个不成立的关键观察，或凭空引用一个不存在/不适用定理。"),
    "S3": ("E2", "把算法描述换成另一个不适用的算法，但不要改动代码。"),
    "S4": ("E3", "正确性证明中出现不成立的推理跳跃，或只覆盖了部分情形。"),
    "S5": ("E5", "把时间复杂度分析写错（低估或高估）。"),
    "S6": ("E4", "边界条件论证是错的，或遗漏关键边界却声称已覆盖。"),
}


# ---------------------------------------------------------------- 提示词
INJECT_SYSTEM = """你是一名算法竞赛出题人，负责构造「学生写错」的题解样本，用于测试评估系统能否定位错误。

规则（必须严格遵守）：
1. 只输出从指定章节开始的 Markdown 章节，**不要输出它前面的任何章节**。
2. 章节标题必须原样写成 `## S<编号> <名称>` 的形式，便于程序解析。
3. 指定的错误必须真的存在、且易于辨认（评审人看一遍就能指出错在哪）。
4. 不要说明「这里故意写错了」「这是一道测试题」之类的话，就当自己在写一份
   真诚但确实写错了的题解。
5. 你输出的第 1 个章节是错的；它的**后续章节必须顺着这个错误继续推进**，
   保持内部自洽，不要悄悄修正回正确的做法。
6. 如果输出范围包含 S7，S7 里必须有一个完整的 ```python 代码块，从标准输入
   读取、向标准输出打印结果。
"""

REWRITE_ONE_SYSTEM = """你是一名算法竞赛出题人，负责构造「过程不成立但代码能通过」的题解样本。

规则（必须严格遵守）：
1. 只输出**指定的那一节**，不要输出任何其它章节。
2. 章节标题必须原样写成 `## S<编号> <名称>` 的形式。
3. 你要改写的那一节必须真的有不成立的论证，但**文字上要写得像真的**：
   自信、具体、有术语，不要自曝破绽。
4. 不要提到代码，不要输出任何代码块。
5. 不要输出「这是一道测试题」「此处故意出错」之类的话。
"""


def _section_block(step: str, text: str) -> str:
    return f"## {step} {STEP_TITLES[step]}\n{text.strip()}"


def build_full_solution_text(steps: dict[str, str], upto: str | None = None) -> str:
    """把结构化题解渲染成给模型看的 Markdown 文本"""
    keys = [k for k in STEP_KEYS if upto is None or k <= upto]
    return "\n\n".join(_section_block(k, steps[k]) for k in keys if steps.get(k))


def build_inject_prompt(
    statement: str,
    steps: dict[str, str],
    target_step: str,
    error_type: str,
) -> str:
    """构造「重写 Sk..S7，让 Sk 出现 Ex 类错误」的提示"""
    prefix = [k for k in STEP_KEYS if k < target_step]
    suffix = [k for k in STEP_KEYS if k >= target_step]
    seen = build_full_solution_text({k: steps[k] for k in prefix}) if prefix else "（无）"

    return f"""下面是一道算法题和它的**正确**题解。

【题目】
{statement}

【已经确认正确的部分】（不要改动，也不要重复输出）
{seen}

【原始的正确后续章节】（供你理解原意，你要把它替换掉）
{build_full_solution_text({k: steps[k] for k in suffix if steps.get(k)})}

【你的任务】
从 `## {target_step} {STEP_TITLES[target_step]}` 开始，重写 {'、'.join(suffix)} 这几节。
要求 `{target_step} {STEP_TITLES[target_step]}` 节中出现一个【{error_type} {ERROR_TYPE_TITLE[error_type]}】类型的错误：

{ERROR_TYPE_ACTION[error_type]}

之后各节要顺着这个错误继续写，保持内部自洽。
只输出从 `## {target_step}` 开始的章节，不要输出前面的内容，不要加任何解释。"""


def build_ac_invalid_prompt(
    statement: str,
    steps: dict[str, str],
    target_step: str,
) -> str:
    """构造「代码不动、只让某一步论证不成立」的提示"""
    error_type, action = AC_INVALID_ACTION[target_step]
    return f"""下面是一道算法题和它的正确题解。它的代码是正确的，不要改动代码。

【题目】
{statement}

【完整正确题解】
{build_full_solution_text(steps)}

【你的任务】
只重写 `## {target_step} {STEP_TITLES[target_step]}` 这一节，让其中出现一个
【{error_type} {ERROR_TYPE_TITLE[error_type]}】类型的问题：

{action}

注意：代码是对的，所以这一节的问题不能靠代码暴露出来 —— 问题只存在于论证本身。
其它章节一个字都不要输出。"""


# ---------------------------------------------------------------- 工具
def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def merge_sections(prefix: dict[str, str], patch_raw: str) -> dict[str, str]:
    """把模型输出的「后缀章节」拼到原前缀上；同名章节以模型输出为准"""
    normalized = normalize_headings(patch_raw)
    patched = {m.group(1): m.group(2).strip() for m in SECTION_RE.finditer(normalized)}
    merged = {k: prefix[k] for k in STEP_KEYS if prefix.get(k)}
    merged.update(patched)
    return merged


_HEADING_RE = re.compile(r"^#{2,5}\s*(S[1-7])\b", re.MULTILINE)


def normalize_headings(raw: str) -> str:
    """把 `### S3 xxx` / `## S3：xxx` 之类的标题统一成 `## S3 xxx`。

    模型不总会严格输出二级标题，而解析器只认 `##`。不改写的话会整段丢失，
    表现为「注入的候选缺步骤」而被误判成注入失败。
    """
    return _HEADING_RE.sub(lambda m: f"## {m.group(1)}", raw)


def missing_sections(steps: dict[str, str]) -> list[str]:
    return [k for k in STEP_KEYS if not (steps.get(k) or "").strip()]


@dataclass
class CandidateBundle:
    """一道题的全部候选与标注"""

    pid: str
    candidates: list[dict[str, Any]] = field(default_factory=list)
    labels: dict[str, dict[str, Any]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def judge_candidate(
    code: str,
    cases: list[TestCase],
    *,
    timeout: float,
    comparison: str = DEFAULT_COMPARISON,
    memory_limit_mb: int = 512,
    abort_on_failure: bool = True,
) -> dict[str, Any]:
    """对候选代码跑公开测试用例（走应用侧同一套 sandbox 判题器）。

    判题口径必须与应用侧完全一致：这里用同一个 `sandbox.judge_program`、
    同一个 `comparison` 模式，标签里的 exec_status 才能在应用里被复现。

    abort_on_failure 默认开启：错误解常常是朴素写法，在 Python 下逐条 TLE，
    而 CF 时限可达 2~4 秒；认定「非 AC」不需要跑完剩余用例。
    """
    if not code.strip():
        return {
            "verdict": "NO_CODE",
            "passed": 0,
            "total": len(cases),
            "comparison": comparison,
            "max_ms": 0.0,
        }
    limits = SandboxLimits(time_limit_seconds=timeout, memory_limit_mb=memory_limit_mb)
    result = judge_program(
        LocalSandbox(limits),
        CodeBlock(language="python", code=code),
        cases,
        limits,
        comparison=comparison,
        abort_on_failure=abort_on_failure,
    )
    return {
        "verdict": result.status.value,
        "passed": result.passed,
        "total": result.total,
        "comparison": comparison,
        "max_ms": round(max((t.time_ms for t in result.test_results), default=0.0), 2),
        "failed_index": result.failed_index,
        "aborted_early": bool(abort_on_failure and result.passed < result.total),
    }


# ---------------------------------------------------------------- 落盘序列化
def variant_name(candidate: dict[str, Any]) -> str:
    """`<pid>#<kind><suffix>` → `<kind><suffix>`，用作 variants.json 里的 name"""
    return str(candidate["candidate_id"]).split("#", 1)[-1]


def variant_payload(candidate: dict[str, Any]) -> dict[str, Any]:
    """variants.json 的一条候选（**不含任何标注**）。

    字段形状严格对齐 `benchmark.dataset._load_variants` 的读取契约：
    `name` / `language` / `code` / `steps`（step_id → 正文的映射）。
    标注不进这里 —— 公开仓库可以看到候选，但看不到答案。
    """
    steps = candidate.get("steps") or {}
    return {
        "name": variant_name(candidate),
        "language": "python",
        "code": candidate.get("code") or "",
        "steps": {key: steps.get(key, "") for key in STEP_KEYS},
        # 溯源信息（不泄露标注）：候选怎么来的、跑出来什么结果
        "code_source": candidate.get("code_source"),
        "steps_sha256": candidate.get("steps_sha256"),
        "judge": {
            "exec_status": (candidate.get("judge") or {}).get("verdict"),
            "passed": (candidate.get("judge") or {}).get("passed"),
            "total": (candidate.get("judge") or {}).get("total"),
            "comparison": (candidate.get("judge") or {}).get("comparison"),
        },
    }


def label_payload(candidate: dict[str, Any]) -> dict[str, Any]:
    """labels.json 的一条 ground truth（**私有**）。

    规范字段直接对齐 `benchmark.dataset.Variant`：
        exec_status / process_valid / first_error / error_type / ac_but_invalid

    其中 `ac_but_invalid` 由程序推导而非人工声明：
    过程不成立 **且** 代码实测 AC —— 这正是「结果正确但过程不成立」的定义。
    其余键（注入细节、标注依据、模型元信息）是审计用的附加证据，读取方忽略即可。
    """
    label = candidate.get("label") or {}
    judge = candidate.get("judge") or {}
    exec_status = judge.get("verdict", "")
    process_valid = label.get("expected_process_valid")

    payload = {
        "kind": label.get("kind") or candidate.get("kind"),
        "exec_status": exec_status,
        "process_valid": process_valid,
        "first_error": label.get("first_error"),
        "error_type": label.get("error_type") or "NONE",
        "ac_but_invalid": process_valid is False and exec_status == "AC",
        "confidence": label.get("label_confidence"),
        "basis": label.get("label_basis"),
    }
    # 附加审计证据（注入范围、被改章节、代码来源、模型返回摘要）
    for key in (
        "injected",
        "changed_section",
        "code_unchanged",
        "code_verified",
    ):
        if key in label:
            payload[key] = label[key]
    if candidate.get("model"):
        payload["model"] = candidate["model"]
    return payload


def make_candidate(
    pid: str,
    kind: str,
    steps: dict[str, str],
    *,
    code_source: str,
    model_meta: dict[str, Any],
    judge: dict[str, Any],
    suffix: str = "",
) -> dict[str, Any]:
    cid = f"{pid}#{kind}{suffix}"
    return {
        "candidate_id": cid,
        "pid": pid,
        "kind": kind,
        "steps": {k: steps.get(k, "") for k in STEP_KEYS},
        "code": extract_code(steps.get("S7", "")),
        "code_source": code_source,
        "judge": judge,
        "model": model_meta,
        "steps_sha256": sha256_text(
            "\x1e".join(steps.get(k, "") for k in STEP_KEYS)
        ),
    }
