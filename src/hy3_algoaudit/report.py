"""Structured evaluation report rendering: Markdown + standalone HTML."""
from __future__ import annotations

import html

from .schemas import (AuditReport, ErrorType, ExecStatus, EvidenceKind,
                      ProcessVerdict, StructuredSolution, STEP_NAMES_ZH)

_STATUS_COLOR = {
    "AC": "#16a34a", "WA": "#dc2626", "TLE": "#d97706", "MLE": "#d97706",
    "RE": "#dc2626", "CE": "#7c3aed", "SE": "#6b7280",
}


def render_markdown(report: AuditReport) -> str:
    lines: list[str] = []
    ap = lines.append
    ap(f"# Hy3-AlgoAudit 评估报告：{report.problem_id or '未命名题目'}")
    ap("")
    ap(f"- 模式：{'AI 自动解题' if report.mode == 'solve' else '用户解答评估'}")
    ap(f"- 时间：{report.created_at}")
    if report.repair_rounds:
        ap(f"- 定向修复轮数：{report.repair_rounds}")
    ap("")

    # ---- 四象限结论 ----
    ap("## 总体结论")
    ap("")
    ap(f"**{report.quadrant or '未评定'}**")
    ap("")
    ap(f"> {report.process.summary}")
    ap("")

    # ---- 结果通道 ----
    ap("## 结果通道（沙箱执行）")
    ap("")
    if report.execution is not None:
        ex = report.execution
        ap(f"- 状态：**{ex.status.value}**（通过 {ex.passed}/{ex.total}）")
        if ex.failed_index is not None:
            ap(f"- 首个失败测试：#{ex.failed_index} — {ex.detail}")
        for tr in ex.test_results:
            ap(f"  - 测试 #{tr.index}: {tr.status.value} ({tr.time_ms} ms)")
    else:
        ap("- 未提供测试用例，跳过沙箱执行。")
    ap("")

    # ---- 过程通道 ----
    p = report.process
    ap("## 过程通道（过程评估器）")
    ap("")
    ap(f"- 过程是否有效：**{'有效' if p.process_valid else '无效'}**")
    if not p.process_valid:
        fe = p.first_error_step or "-"
        name = STEP_NAMES_ZH.get(p.first_error_step or "", "")
        ap(f"- 首个错误步骤：**{fe}**（{name}）")
        ap(f"- 错误类型：**{p.error_type.value}** {p.error_type.label}")
    ap(f"- 置信度：{p.confidence:.2f}")
    ap("")

    ap("### 分步审查")
    ap("")
    ap("| 步骤 | 名称 | 判定 | 置信度 | 错误类型 | 依据 |")
    ap("|---|---|---|---|---|---|")
    for v in p.step_verdicts:
        name = STEP_NAMES_ZH.get(v.step_id, v.step_id)
        reason = (v.reasoning or v.suspicious_claim or "").replace("|", "\\|")[:80]
        ap(f"| {v.step_id} | {name} | {v.status} | {v.confidence:.2f} "
           f"| {v.error_type.value if v.error_type != ErrorType.NONE else '-'} | {reason} |")
    ap("")

    ap("### 证据清单")
    ap("")
    for e in p.evidence:
        flag = "❌" if e.indicates_invalid else "✅"
        ap(f"- {flag} **[{e.kind.value}]** (强度 {e.strength}) "
           f"{e.step_id or '-'} {e.error_type.value if e.error_type != ErrorType.NONE else ''}"
           f"：{e.detail or e.claim}")
    ap("")

    # ---- 修复记录 ----
    if report.notes:
        ap("## 修复记录")
        ap("")
        for n in report.notes:
            ap(f"- {n}")
        ap("")

    # ---- 最终题解 ----
    sol = report.final_solution
    if sol is not None:
        ap("## 最终题解（S1-S7）")
        ap("")
        for s in sol.steps:
            ap(f"### {s.step_id} {s.title}")
            ap("")
            ap(s.content.strip() or "（空）")
            ap("")
        ap("### 代码")
        ap("")
        ap(f"```{sol.code.language}")
        ap(sol.code.code.rstrip())
        ap("```")
        ap("")
    return "\n".join(lines)


def render_html(report: AuditReport) -> str:
    """Minimal standalone HTML (no external assets)."""
    md = html.escape(render_markdown(report))
    status = report.execution.status.value if report.execution else "-"
    color = _STATUS_COLOR.get(status, "#6b7280")
    badge = ("green" if report.quadrant_key == "full_correct"
             else "amber" if report.quadrant_key == "ac_but_invalid" else "red")
    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>Hy3-AlgoAudit 报告 - {html.escape(report.problem_id or '未命名')}</title>
<style>
  body {{ font-family: "Microsoft YaHei", "PingFang SC", sans-serif; margin: 0;
        background: #f8fafc; color: #0f172a; }}
  header {{ background: #0f172a; color: #fff; padding: 24px 32px; }}
  header h1 {{ margin: 0; font-size: 20px; }}
  header .meta {{ color: #94a3b8; font-size: 13px; margin-top: 6px; }}
  main {{ max-width: 980px; margin: 24px auto; padding: 0 16px; }}
  .badge {{ display: inline-block; padding: 3px 10px; border-radius: 999px;
          font-size: 13px; font-weight: 600; }}
  .badge.green {{ background: #dcfce7; color: #166534; }}
  .badge.amber {{ background: #fef3c7; color: #92400e; }}
  .badge.red   {{ background: #fee2e2; color: #991b1b; }}
  pre {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 8px;
        padding: 16px; overflow-x: auto; font-size: 12.5px; line-height: 1.55; }}
  .exec {{ font-weight: 700; color: {color}; }}
</style>
</head>
<body>
<header>
  <h1>Hy3-AlgoAudit 评估报告：{html.escape(report.problem_id or '未命名题目')}</h1>
  <div class="meta">模式 {html.escape(report.mode)} · 时间 {html.escape(report.created_at)}
   · 执行状态 <span class="exec">{status}</span>
   · 修复轮数 {report.repair_rounds} · Tokens {report.tokens_used}</div>
</header>
<main>
  <p><span class="badge {badge}">{html.escape(report.quadrant or '未评定')}</span></p>
  <pre>{md}</pre>
</main>
</body>
</html>"""
