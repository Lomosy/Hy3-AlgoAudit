"""为每道题构造四类候选过程（技术方案书 15.3）。

    correct      正确题解 + AC 实现          → 测误报率
    natural      Hy3 自然输出                → 研究模型自发错误
    injected     在指定步骤注入指定错误类型   → 首错定位 / 错误分类准确率
    ac_invalid   代码不变、过程不成立         → AC-but-Invalid 识别率

前置条件：先跑完 scripts/build_dataset.py，产出「每题一目录」的公开评测集
（problem.md / meta.json / tests.json / reference.py）。本脚本以「正确候选」为
锚点，注入类候选都从它派生，因此不同候选在目标步骤之前的内容逐字节一致，
首错定位评测是干净的对照实验。

公开 / 私有划分（冰山理论）：
    <dataset_dir>/<pid>/variants.json    correct + natural（无标注）
    <private_dir>/<pid>/variants.json    injected + ac_invalid（正文与**名字**即答案）
    <private_dir>/<pid>/labels.json      全部候选的 ground truth

判题口径与应用侧完全一致：统一走 `sandbox.judge_program`，并按题目 meta 里的
comparison 模式比对。

用法：

    python scripts/build_candidates.py --dry-run --limit 1     # 只看提示词，不花额度
    python scripts/build_candidates.py --limit 2               # 小样本试跑
    python scripts/build_candidates.py                          # 全量
    python scripts/build_candidates.py --report-only            # 只汇总已有候选
    python scripts/build_candidates.py --validate               # 校验三条不变量
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hy3_algoaudit.builder import candidates as C  # noqa: E402
from hy3_algoaudit.builder import dataset, pool, verify  # noqa: E402
from hy3_algoaudit.builder.pool import load_config  # noqa: E402
from hy3_algoaudit.schemas import TestCase  # noqa: E402
from hy3_algoaudit._legacy.llm import Hy3Client  # noqa: E402
from hy3_algoaudit._legacy.solver.prompt import SYSTEM_PROMPT, build_prompt  # noqa: E402
from hy3_algoaudit._legacy.solver.schema import STEP_KEYS, extract_code  # noqa: E402
from hy3_algoaudit._legacy.solver.solver import parse_solution  # noqa: E402


def log(message: str) -> None:
    print(message, flush=True)


# ---------------------------------------------------------------- 题目装载
def load_bundle(cfg: dict[str, Any], pid: str) -> dict[str, Any]:
    """读取一道题的题面、参考解、公开测试用例（每题一目录布局）"""
    pdir = dataset.problem_dir(cfg, pid)
    meta = json.loads((pdir / "meta.json").read_text(encoding="utf-8"))
    cases_raw = json.loads((pdir / "tests.json").read_text(encoding="utf-8"))
    code = (pdir / "reference.py").read_text(encoding="utf-8")
    declared_mb = int(meta.get("memory_limit_bytes") or 0) // (1024 * 1024)
    cap_mb = int(cfg["verification"]["memory_limit_mb"])
    return {
        "pid": pid,
        "statement": (pdir / "problem.md").read_text(encoding="utf-8"),
        "name": meta["name"],
        # 与参考解验证时同一口径；应用侧按题取出使用
        "comparison": verify.normalize_comparison(meta.get("comparison")),
        "time_limit": float(meta["time_limit_seconds"]),
        # 取题目声明值与配置上限的较小者：声明值可能比本机可用内存还大
        "memory_limit_mb": min(declared_mb, cap_mb) if declared_mb else cap_mb,
        "reference_code": code,
        "cases": [
            TestCase(input=c["input"], expected=c["expected"]) for c in cases_raw
        ],
        "problem_meta": {
            "category": meta.get("category"),
            "difficulty_bin": meta.get("difficulty_bin"),
            "source": meta.get("source"),
        },
    }


def judge_timeout(bundle: dict[str, Any], cfg: dict[str, Any]) -> float:
    return min(
        bundle["time_limit"],
        float(cfg["verification"]["timeout_cap_seconds"]),
    )


def judge(bundle: dict[str, Any], code: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """按题目口径判候选代码（复用同一套 sandbox 判题器）"""
    return C.judge_candidate(
        code,
        bundle["cases"],
        timeout=judge_timeout(bundle, cfg),
        comparison=bundle["comparison"],
        memory_limit_mb=int(bundle["memory_limit_mb"]),
        abort_on_failure=bool(cfg["candidates"].get("abort_on_failure", True)),
    )


def model_meta(cfg: dict[str, Any], raw: str, purpose: str) -> dict[str, Any]:
    return {
        "model": "hy3",
        "purpose": purpose,
        "reasoning_effort": cfg["candidates"]["reasoning_effort"],
        "temperature": cfg["candidates"]["temperature"],
        "raw_response_sha256": C.sha256_text(raw),
        "raw_response_bytes": len(raw.encode("utf-8")),
    }


# ---------------------------------------------------------------- 正确候选
def solve_correct(
    client: Hy3Client, bundle: dict[str, Any], cfg: dict[str, Any]
) -> tuple[dict[str, str], str, dict[str, Any], dict[str, Any]] | None:
    """生成「正确过程」候选：要求其代码实测 AC。

    优先保留模型自己的代码（过程与代码同源，一致性最好）。若多次尝试都拿不到
    AC 代码，则退化为「把已验证的参考解放进 S7」，并显式标注 code_source
    与一致性风险 —— 不这样做的话，这类题就会整个丢掉，而它们往往正是难题。
    """
    cand_cfg = cfg["candidates"]
    timeout = judge_timeout(bundle, cfg)
    comparison = bundle["comparison"]
    retries = int(cand_cfg["correct_max_retries"])

    for attempt in range(retries):
        raw = client.chat(
            build_prompt(bundle["statement"]),
            system=SYSTEM_PROMPT,
            reasoning_effort=cand_cfg["reasoning_effort"],
            temperature=cand_cfg["temperature"],
        )
        solution = parse_solution(bundle["statement"], raw or "")
        if not solution.complete:
            log(f"      正确候选 第{attempt + 1}次 结构不完整，缺失 {C.missing_sections(solution.steps)}")
            continue
        verdict = judge(bundle, solution.code, cfg)
        log(f"      正确候选 第{attempt + 1}次 判定 {verdict['verdict']} "
            f"({verdict['passed']}/{verdict['total']})")
        if verdict["verdict"] == "AC":
            return (
                solution.steps,
                "model",
                verdict,
                model_meta(cfg, raw or "", "correct_generation"),
            )

    # 兜底：用已验证的参考解填 S7
    raw = client.chat(
        build_prompt(bundle["statement"]),
        system=SYSTEM_PROMPT,
        reasoning_effort=cand_cfg["reasoning_effort"],
        temperature=cand_cfg["temperature"],
    )
    solution = parse_solution(bundle["statement"], raw or "")
    if not solution.complete:
        return None
    steps = dict(solution.steps)
    steps["S7"] = (
        "按上述思路实现的完整代码（该代码已在全部公开用例上实测 AC）：\n\n"
        "```python\n" + bundle["reference_code"].strip() + "\n```"
    )
    verdict = judge(bundle, extract_code(steps["S7"]), cfg)
    return (
        steps,
        "reference",
        verdict,
        model_meta(cfg, raw or "", "correct_generation_reference_s7"),
    )


# ---------------------------------------------------------------- 注入候选
def inject_candidate(
    client: Hy3Client,
    bundle: dict[str, Any],
    base_steps: dict[str, str],
    target_step: str,
    error_type: str,
    cfg: dict[str, Any],
) -> dict[str, Any] | None:
    """重写 Sk..S7 使 Sk 出现指定错误；S1..S(k-1) 由程序原样拼接"""
    cand_cfg = cfg["candidates"]
    prompt = C.build_inject_prompt(bundle["statement"], base_steps, target_step, error_type)
    raw = client.chat(
        prompt,
        system=C.INJECT_SYSTEM,
        reasoning_effort=cand_cfg["reasoning_effort"],
        temperature=cand_cfg["temperature"],
    ) or ""

    prefix = {k: base_steps[k] for k in STEP_KEYS if k < target_step and base_steps.get(k)}
    merged = C.merge_sections(prefix, raw)

    # 注入成功性检查：前缀必须完整；目标步骤必须由模型真的写出来了
    missing_prefix = [k for k in STEP_KEYS if k < target_step and not merged.get(k)]
    if missing_prefix or not merged.get(target_step):
        return None

    timeout = judge_timeout(bundle, cfg)
    verdict = judge(bundle, extract_code(merged.get("S7", "")), cfg)
    candidate = C.make_candidate(
        bundle["pid"],
        C.KIND_INJECTED,
        merged,
        code_source="model_injected",
        model_meta=model_meta(cfg, raw, f"inject_{error_type}_{target_step}"),
        judge=verdict,
        suffix=f"-{error_type}-{target_step}",
    )
    candidate["label"] = {
        "kind": C.KIND_INJECTED,
        "expected_process_valid": False,
        "first_error": target_step,
        "error_type": error_type,
        "injected": {
            "step": target_step,
            "error_type": error_type,
            "error_title": C.ERROR_TYPE_TITLE[error_type],
            "rewritten_range": [
                k for k in STEP_KEYS if k >= target_step and merged.get(k)
            ],
        },
        "label_confidence": "exact",
        "label_basis": "由程序拼接：目标步骤之前逐字节等于正确候选，首错位置由构造保证",
    }
    return candidate


# ---------------------------------------------------------------- AC-but-Invalid
def ac_invalid_candidate(
    client: Hy3Client,
    bundle: dict[str, Any],
    base_steps: dict[str, str],
    target_step: str,
    cfg: dict[str, Any],
) -> dict[str, Any] | None:
    """代码保持参考解不变，只把某一节的论证改坏

    S7 由程序强制替换成已验证 AC 的参考解，**不采信模型输出的代码**，
    这样才能保证「结果正确」这一前提严格成立，从而干净地测出
    「过程不成立」能否被识别。
    """
    cand_cfg = cfg["candidates"]
    prompt = C.build_ac_invalid_prompt(bundle["statement"], base_steps, target_step)
    raw = client.chat(
        prompt,
        system=C.REWRITE_ONE_SYSTEM,
        reasoning_effort=cand_cfg["reasoning_effort"],
        temperature=cand_cfg["temperature"],
    ) or ""

    patched = C.merge_sections({}, raw)
    corrupted = patched.get(target_step)
    if not corrupted or len(corrupted) < 40:
        return None

    steps = {k: base_steps.get(k, "") for k in STEP_KEYS}
    steps[target_step] = corrupted
    steps["S7"] = (
        f"最终实现（与上述分析对应的完整代码）：\n\n```python\n"
        + bundle["reference_code"].strip()
        + "\n```"
    )

    error_type, _ = C.AC_INVALID_ACTION[target_step]
    timeout = judge_timeout(bundle, cfg)
    verdict = judge(bundle, extract_code(steps["S7"]), cfg)
    candidate = C.make_candidate(
        bundle["pid"],
        C.KIND_AC_INVALID,
        steps,
        code_source="reference",
        model_meta=model_meta(cfg, raw, f"ac_invalid_{target_step}"),
        judge=verdict,
        suffix=f"-{target_step}",
    )
    candidate["label"] = {
        "kind": C.KIND_AC_INVALID,
        "expected_process_valid": False,
        "first_error": target_step,
        "error_type": error_type,
        "changed_section": target_step,
        "code_unchanged": True,
        "label_confidence": "exact",
        "label_basis": "S7 强制复用已验证 AC 的参考解，被改坏的只有目标章节",
    }
    return candidate


# ---------------------------------------------------------------- 自然候选
def natural_candidate(
    client: Hy3Client, bundle: dict[str, Any], cfg: dict[str, Any]
) -> dict[str, Any] | None:
    """Hy3 正常解题，不加任何「必须正确」的约束，用于研究模型自发错误"""
    cand_cfg = cfg["candidates"]
    raw = client.chat(
        build_prompt(bundle["statement"]),
        system=SYSTEM_PROMPT,
        reasoning_effort=cand_cfg["reasoning_effort"],
        temperature=cand_cfg["temperature"],
    ) or ""
    solution = parse_solution(bundle["statement"], raw)
    if not solution.complete:
        return None
    verdict = judge(bundle, solution.code, cfg)
    candidate = C.make_candidate(
        bundle["pid"],
        C.KIND_NATURAL,
        solution.steps,
        code_source="model",
        model_meta=model_meta(cfg, raw, "natural_solve"),
        judge=verdict,
    )
    # 自然候选没有 ground truth：它的过程对错正是待研究的对象，
    # 因此只记录「判定结果」这一客观事实，不伪造 process_valid 标注。
    candidate["label"] = {
        "kind": C.KIND_NATURAL,
        "expected_process_valid": None,
        "first_error": None,
        "error_type": None,
        "label_confidence": "none",
        "label_basis": "自然输出，无人工标注；仅凭 judge 结果可判定结果正确性",
    }
    return candidate


# ---------------------------------------------------------------- 单题编排
def run_problem(
    client: Hy3Client | None,
    cfg: dict[str, Any],
    pid: str,
    plan: dict[str, Any],
    *,
    dry_run: bool = False,
) -> C.CandidateBundle:
    bundle = load_bundle(cfg, pid)
    result = C.CandidateBundle(pid=pid)
    log(f"  {pid}  {bundle['name'][:44]}  [{bundle['problem_meta']['category']} / "
        f"{bundle['problem_meta']['difficulty_bin']}]")

    if dry_run:
        # 干跑只打印提示词长度，便于人工检查注入指令是否说得清楚
        probe = {
            "S1": "（占位）题意", "S2": "（占位）观察", "S3": "（占位）算法",
            "S4": "（占位）证明", "S5": "（占位）复杂度", "S6": "（占位）边界",
            "S7": "（占位）代码",
        }
        for target_step, error_type in plan["injected"]:
            text = C.build_inject_prompt(bundle["statement"], probe, target_step, error_type)
            log(f"      [dry-run] 注入 {error_type}@{target_step} 提示词 {len(text)} 字符")
        for target_step in plan["ac_invalid"]:
            text = C.build_ac_invalid_prompt(bundle["statement"], probe, target_step)
            log(f"      [dry-run] AC-Invalid@{target_step} 提示词 {len(text)} 字符")
        return result

    assert client is not None

    # 1) 正确候选——后续注入以它为锚点
    correct = solve_correct(client, bundle, cfg)
    if correct is None:
        result.notes.append("正确候选生成失败，跳过该题全部注入候选")
        return result
    steps, code_source, verdict, meta = correct
    base = C.make_candidate(
        pid, C.KIND_CORRECT, steps, code_source=code_source, model_meta=meta, judge=verdict
    )
    base["label"] = {
        "kind": C.KIND_CORRECT,
        "expected_process_valid": True,
        "first_error": None,
        "error_type": None,
        "code_verified": verdict["verdict"],
        "label_confidence": "high" if code_source == "model" else "medium",
        "label_basis": (
            "代码实测 AC；过程正确性由模型自洽性保证，未逐句人工复核"
            + ("" if code_source == "model" else "；S7 代码取自参考解，存在题解-代码一致风险")
        ),
    }
    if verdict["verdict"] != "AC":
        result.notes.append(f"正确候选代码非 AC（{verdict['verdict']}），该题可信度下降")
    result.candidates.append(base)
    result.labels[base["candidate_id"]] = base["label"]
    log(f"      正确候选 OK  code_source={code_source}  判定={verdict['verdict']}")

    # 2) 自然候选
    natural = natural_candidate(client, bundle, cfg)
    if natural is not None:
        result.candidates.append(natural)
        result.labels[natural["candidate_id"]] = natural["label"]
        log(f"      自然候选 OK  判定={natural['judge']['verdict']}")

    # 3) 注入候选
    for target_step, error_type in plan["injected"]:
        injected = inject_candidate(client, bundle, steps, target_step, error_type, cfg)
        if injected is None:
            result.notes.append(f"注入 {error_type}@{target_step} 失败：模型未输出合法章节")
            log(f"      注入 {error_type}@{target_step} 失败")
            continue
        result.candidates.append(injected)
        result.labels[injected["candidate_id"]] = injected["label"]
        log(f"      注入 {error_type}@{target_step} OK  判定={injected['judge']['verdict']}")

    # 4) AC-but-Invalid 候选
    for target_step in plan["ac_invalid"]:
        invalid = ac_invalid_candidate(client, bundle, steps, target_step, cfg)
        if invalid is None:
            result.notes.append(f"AC-Invalid@{target_step} 失败：模型未输出合法章节")
            log(f"      AC-Invalid@{target_step} 失败")
            continue
        result.candidates.append(invalid)
        result.labels[invalid["candidate_id"]] = invalid["label"]
        log(f"      AC-Invalid@{target_step} OK  判定={invalid['judge']['verdict']}")

    return result


# ---------------------------------------------------------------- 注入计划
def build_plan(pid: str, cfg: dict[str, Any], order_index: int) -> dict[str, Any]:
    """按题目顺序轮转分配注入的错误类型与目标步骤，保证 E1..E8 覆盖均衡"""
    cand_cfg = cfg["candidates"]
    error_types = list(cand_cfg["injected_error_types"])
    steps_pool = list(cand_cfg["ac_invalid_steps"])
    n_inject = int(cand_cfg["injected_per_problem"])
    n_invalid = int(cand_cfg["ac_invalid_per_problem"])

    injected = []
    for j in range(n_inject):
        error_type = error_types[(order_index * n_inject + j) % len(error_types)]
        injected.append((C.ERROR_TYPE_STEP[error_type], error_type))

    ac_invalid = [
        steps_pool[(order_index * n_invalid + j) % len(steps_pool)]
        for j in range(n_invalid)
    ]
    # 同一题内避免重复目标章节
    ac_invalid = list(dict.fromkeys(ac_invalid))
    return {"injected": injected, "ac_invalid": ac_invalid}


# ---------------------------------------------------------------- 落盘
def write_bundle(cfg: dict[str, Any], result: C.CandidateBundle) -> dict[str, int]:
    """把一道题的候选写进「每题一目录」布局。

        公开 <dataset_dir>/<pid>/variants.json     correct + natural（无标注）
        私有 <private_dir>/<pid>/variants.json     injected + ac_invalid（正文即答案）
        私有 <private_dir>/<pid>/labels.json       全部候选的 ground truth

    为什么注入候选正文也要藏：候选的 **名字**（`injected-E2-S3`）本身就泄露答案 ——
    「目标步骤是 S3、错误类型是 E2」直接写在名字里。所以名称带语义的候选一律
    只在私有侧出现；公开侧只有 correct / natural 两类。
    """
    pub_dir = dataset.problem_dir(cfg, result.pid)
    priv_dir = dataset.problem_dir(cfg, result.pid, private=True)
    pub_dir.mkdir(parents=True, exist_ok=True)
    priv_dir.mkdir(parents=True, exist_ok=True)

    public = [c for c in result.candidates if c["kind"] in C.PUBLIC_KINDS]
    private = [c for c in result.candidates if c["kind"] in C.PRIVATE_KINDS]

    _dump_json(
        pub_dir / "variants.json",
        [C.variant_payload(candidate) for candidate in public],
    )
    _dump_json(
        priv_dir / "variants.json",
        [C.variant_payload(candidate) for candidate in private],
    )

    # 标注覆盖全部候选（公开侧也要有，否则无法评估 correct/natural）
    labels = {
        C.variant_name(candidate): C.label_payload(candidate)
        for candidate in result.candidates
    }
    counts = {
        kind: sum(1 for c in result.candidates if c["kind"] == kind)
        for kind in (C.KIND_CORRECT, C.KIND_NATURAL, C.KIND_INJECTED, C.KIND_AC_INVALID)
    }
    payload = {
        "pid": result.pid,
        "generated_at": C.now_iso(),
        "counts": counts,
        "notes": result.notes,
        "labels": labels,
        "label_schema": {
            "exec_status": "AC/WA/TLE/MLE/RE/CE/NO_CODE —— 由 sandbox 实测得出",
            "process_valid": "true/false/null —— null 表示无 ground truth（natural）",
            "first_error": "S1..S7 或 null —— 首个错误步骤",
            "error_type": "E1..E8 或 NONE",
            "ac_but_invalid": "过程不成立 且 代码实测 AC",
        },
    }
    _dump_json(priv_dir / "labels.json", payload)
    return counts


def _dump_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def load_labels(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """读取全部题目的 labels.json（私有侧）"""
    root = dataset.private_root(cfg)
    out: dict[str, dict[str, Any]] = {}
    if not root.exists():
        return out
    for pdir in sorted(root.iterdir()):
        path = pdir / "labels.json"
        if path.exists():
            out[pdir.name] = json.loads(path.read_text(encoding="utf-8"))
    return out


def load_variants(cfg: dict[str, Any], pid: str, *, private: bool) -> dict[str, dict[str, Any]]:
    """读取一道题的 variants.json，按 name 建索引"""
    path = dataset.problem_dir(cfg, pid, private=private) / "variants.json"
    if not path.exists():
        return {}
    return {
        variant["name"]: variant
        for variant in json.loads(path.read_text(encoding="utf-8"))
    }


def report(cfg: dict[str, Any]) -> int:
    """汇总已生成的候选：类型分布、判定分布、注入成功率"""
    all_labels = load_labels(cfg)
    if not all_labels:
        log("尚未生成任何候选（先运行不带 --dry-run 的本脚本）")
        return 0

    kind_counts: dict[str, int] = {}
    verdict_counts: dict[str, int] = {}
    error_type_counts: dict[str, int] = {}
    first_error_counts: dict[str, int] = {}
    ac_but_invalid = 0
    failed_injections = 0
    for payload in all_labels.values():
        failed_injections += len([n for n in payload.get("notes", []) if "注入" in n])
        for label in payload["labels"].values():
            kind_counts[label["kind"]] = kind_counts.get(label["kind"], 0) + 1
            status = label.get("exec_status") or "?"
            verdict_counts[status] = verdict_counts.get(status, 0) + 1
            if label.get("error_type"):
                error_type_counts[label["error_type"]] = error_type_counts.get(label["error_type"], 0) + 1
            if label.get("first_error"):
                first_error_counts[label["first_error"]] = first_error_counts.get(label["first_error"], 0) + 1
            if label.get("ac_but_invalid"):
                ac_but_invalid += 1

    log(f"题目数 {len(all_labels)}")
    log("\n候选类型分布：")
    for kind, count in sorted(kind_counts.items(), key=lambda kv: -kv[1]):
        log(f"  {kind:<14} {count:>5}")
    log("\n判题结果分布（全部候选）：")
    for verdict, count in sorted(verdict_counts.items(), key=lambda kv: -kv[1]):
        log(f"  {verdict:<14} {count:>5}")
    log("\n注入错误类型分布：")
    for name, count in sorted(error_type_counts.items()):
        title = C.ERROR_TYPE_TITLE.get(name, "")
        log(f"  {name} {title:<14} {count:>5}")
    log("\n首错位置分布：")
    for name, count in sorted(first_error_counts.items()):
        log(f"  {name:<6} {count:>5}")
    log(f"\nAC-but-Invalid（过程不成立 且 代码实测 AC）：{ac_but_invalid}")
    log(f"注入失败次数：{failed_injections}")
    return 0


def validate(cfg: dict[str, Any]) -> int:
    """校验候选集的三条不变量 —— 它们定义了「首错标注为什么可信」。

    1. 前缀不变：injected 候选在目标步骤**之前**的内容必须与 correct 候选逐字节相同。
       破了这条，首错定位的 A/B 对照就不成立（对照变了两个变量）。
    2. 变化起点：injected 候选的变化必须**恰好从目标步骤开始**，
       否则实际首错位置可能早于标注值。
    3. 代码不变：ac_invalid 候选的代码必须与题目目录里的 reference.py 逐字节相同。
       破了这条，「结果正确」这个前提就不成立。

    另附结构检查：7 节齐全、S7 含代码块、correct 候选判定为 AC。
    """
    all_labels = load_labels(cfg)
    if not all_labels:
        log("尚未生成任何候选")
        return 0

    problems: list[str] = []
    checked = 0
    for pid, payload in sorted(all_labels.items()):
        labels = payload["labels"]
        public = load_variants(cfg, pid, private=False)
        private = load_variants(cfg, pid, private=True)

        base = public.get("correct")
        if base is None:
            problems.append(f"{pid}: 缺少 correct 候选")
            continue
        base_status = (base.get("judge") or {}).get("exec_status")
        if base_status != "AC":
            problems.append(f"{pid}: correct 候选判定为 {base_status}，不是 AC")

        ref_code = (
            dataset.problem_dir(cfg, pid) / "reference.py"
        ).read_text(encoding="utf-8").strip()

        for name, candidate in private.items():
            checked += 1
            label = labels.get(name)
            if label is None:
                problems.append(f"{pid}/{name}: 缺少标注")
                continue
            steps = candidate.get("steps") or {}
            if any(not (steps.get(k) or "").strip() for k in STEP_KEYS):
                problems.append(f"{pid}/{name}: 七节不齐全")
            if not (candidate.get("code") or "").strip():
                problems.append(f"{pid}/{name}: S7 未解析出代码块")

            kind = label["kind"]
            if kind == C.KIND_INJECTED:
                target = label["first_error"]
                before = [k for k in STEP_KEYS if k < target]
                if not all(steps.get(k) == (base["steps"] or {}).get(k) for k in before):
                    problems.append(f"{pid}/{name}: 目标步骤之前的内容与 correct 候选不一致")
                changed = [
                    k for k in STEP_KEYS
                    if steps.get(k) != (base["steps"] or {}).get(k)
                ]
                if changed and changed[0] != target:
                    problems.append(
                        f"{pid}/{name}: 变化起点是 {changed[0]}，早于标注的首错 {target}"
                    )
            elif kind == C.KIND_AC_INVALID:
                if (candidate.get("code") or "").strip() != ref_code:
                    problems.append(f"{pid}/{name}: 代码与参考解不一致（AC 前提被破坏）")
                changed = [
                    k for k in STEP_KEYS
                    if steps.get(k) != (base["steps"] or {}).get(k)
                ]
                if len(changed) != 1:
                    problems.append(f"{pid}/{name}: 应只改动 1 节，实际改动了 {changed}")

    log(f"校验 {len(all_labels)} 道题的候选：")
    log(f"  私有候选（injected / ac_invalid）{checked} 条")
    if problems:
        log(f"\n发现 {len(problems)} 处问题：")
        for item in problems:
            log(f"  ✗ {item}")
        return 1
    log("  三条不变量全部通过 ✓")
    return 0


# ---------------------------------------------------------------- 入口
def main() -> int:
    parser = argparse.ArgumentParser(description="构造四类候选过程")
    parser.add_argument("--config", default=None)
    parser.add_argument("--limit", type=int, default=None, help="只处理前 N 道题")
    parser.add_argument("--only", default=None, help="只处理指定 pid（逗号分隔）")
    parser.add_argument("--dry-run", action="store_true", help="只打印注入提示词，不调用模型")
    parser.add_argument("--report-only", action="store_true", help="只汇总已有候选")
    parser.add_argument("--validate", action="store_true", help="只校验已有候选的不变量")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.report_only:
        return report(cfg)
    if args.validate:
        return validate(cfg)

    manifest_path = dataset.dataset_root(cfg) / dataset.MANIFEST_NAME
    if not manifest_path.exists():
        raise SystemExit(
            f"缺少 {manifest_path}，请先运行：python scripts/build_dataset.py --stage all"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    pids = [item["pid"] for item in manifest["problems"]]

    if args.only:
        keep = {p.strip() for p in args.only.split(",") if p.strip()}
        pids = [p for p in pids if p in keep]
    if args.limit:
        pids = pids[: args.limit]
    if not pids:
        log("没有待处理的题目")
        return 0

    client = None if args.dry_run else Hy3Client()
    cand_cfg = cfg["candidates"]

    log("=" * 72)
    log(f"构造候选过程：{len(pids)} 道题")
    log(f"  每题的注入计划：{cand_cfg['injected_per_problem']} 条注入 + "
        f"{cand_cfg['ac_invalid_per_problem']} 条 AC-Invalid")
    log(f"  推理强度 {cand_cfg['reasoning_effort']}，温度 {cand_cfg['temperature']}")
    if args.dry_run:
        log("  [dry-run] 不会调用模型，也不会写入文件")
    log("=" * 72)

    started = time.time()
    totals: dict[str, int] = {}
    for index, pid in enumerate(pids):
        plan = build_plan(pid, cfg, index)
        result = run_problem(client, cfg, pid, plan, dry_run=args.dry_run)
        if args.dry_run:
            continue
        counts = write_bundle(cfg, result)
        for kind, count in counts.items():
            totals[kind] = totals.get(kind, 0) + count
        if result.notes:
            for note in result.notes:
                log(f"      注意：{note}")

    log("")
    log("-" * 72)
    log(f"完成 {len(pids)} 道题，耗时 {(time.time() - started) / 60:.1f} 分钟")
    for kind, count in sorted(totals.items()):
        log(f"  {kind:<14} {count:>5}")
    log("")
    return report(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
