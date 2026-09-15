# data/ — ProcessEval-CP 数据集（公开部分）

本目录是 **ProcessEval-CP** 的公开部分：题面、元数据、测试用例、参考解、公开候选。
构建方法、关键决策的实测依据、已知限制，见 [`../docs/DATASET.md`](../docs/DATASET.md)。
本文件只讲**怎么用**。

> 过程评估所需的标注（首个错误步骤、错误类型）与注入错误的候选过程不在本目录，
> 属于 `private/processeval-cp/` 范围。

---

## 目录结构

```
data/processeval-cp/
├── manifest.json            构建清单：配置快照 + 各阶段统计 + 逐题索引
└── <pid>/
    ├── problem.md           完整题面
    ├── meta.json            元数据（难度、标签、时限、判题比对模式）
    ├── tests.json           测试用例 [{"input","expected"}, ...]
    ├── reference.py         参考解（实测全部公开用例 AC）
    └── variants.json        公开候选（correct + natural，不含标注）
```

一个目录 = 一道题。`<pid>` 形如 `cc-train-010-0033`，编码了「分片 + 记录序号」，
可原地回溯到 CodeContests 原始数据。

私有材料（**不随仓库发布**）结构镜像同一布局：

```
private/processeval-cp/
└── <pid>/
    ├── variants.json        私有候选（injected + ac_invalid，正文与名字即答案）
    ├── labels.json          全部候选的 ground truth 标注
    └── verification.json    参考解验证证据（逐用例哈希、耗时、比对模式）
```

---

## 快速上手

最省事的方式是直接用应用侧的加载器 —— 它读的就是这套目录契约：

```python
from hy3_algoaudit.benchmark.dataset import load_dataset

# 只读公开部分：能跑基准，但拿不到答案
cases = load_dataset("data/processeval-cp")

# 带上私有目录：注入候选与 ground truth 一并并入
cases = load_dataset("data/processeval-cp", private_root="private/processeval-cp")

case = cases[0]
print(case.problem_id, case.difficulty, case.tags, len(case.tests), case.comparison)
for v in case.variants:
    print(v.name, v.kind, v.label_exec_status, v.label_first_error, v.label_ac_but_invalid)
```

只想手工读一份也行：

```python
import json
from pathlib import Path

from hy3_algoaudit.config import SandboxLimits
from hy3_algoaudit.sandbox import LocalSandbox
from hy3_algoaudit.sandbox.judge import judge_program
from hy3_algoaudit.schemas import CodeBlock, TestCase

pid = "cc-train-010-0033"
pdir = Path("data/processeval-cp") / pid

meta = json.loads((pdir / "meta.json").read_text(encoding="utf-8"))
cases = [TestCase(**t) for t in json.loads((pdir / "tests.json").read_text(encoding="utf-8"))]
code = CodeBlock(language="python", code=(pdir / "reference.py").read_text(encoding="utf-8"))

# 关键：按 meta.json 的 comparison 字段选择比对模式
limits = SandboxLimits(time_limit_seconds=meta["time_limit_seconds"])
result = judge_program(LocalSandbox(limits), code, cases, limits,
                       comparison=meta["comparison"])

assert result.accepted, f"参考解未通过：{result.status}"
print(f"{meta['name']}  用例 {result.passed}/{result.total}  AC")
```

**务必按 `meta.json` 的 `comparison` 字段构造判题器。** 少数题目的期望输出是固定精度的
浮点数，而参考解打印的是 Python 默认精度（`16.6666666667` vs `16.666666666666668`），
需要在 `float` 模式下按 `|got-want| ≤ max(1e-6, 1e-6·|want|)` 判定 ——
这类题的 `comparison` 是 `float`。用 `token` 模式会把它误判成 WA，这是踩过的坑。

---

## 字段说明

### `meta.json`

| 字段 | 说明 |
|---|---|
| `id` | 题目唯一标识 |
| `name` | 题名（题面在 `problem.md`） |
| `source` | `CODEFORCES` / `ATCODER` / `AIZU` |
| `difficulty` | 难度数值（Codeforces rating），下游按它做难度曲线 |
| `difficulty_name` | CodeContests 原生 Difficulty 枚举（A~V / EASY…） |
| `difficulty_bin` / `difficulty_bin_source` | 采样用的难度箱及**分层依据**（`cf_rating` / `taco` / `cc_enum`） |
| `tags` | 原始算法标签（CF tags），下游按它做类别分析 |
| `category` / `category_source` | 首要算法类别及**归类依据**（`cf_tags` / `taco` / `heuristic`） |
| `category_matched` / `categories_all` | 命中的原始标签 / 命中的全部算法类别 |
| `taco_tags` / `taco_skill_types` / `taco_difficulty` / `taco_match` / `taco_url` | TACO 关联结果 |
| `time_limit_seconds` / `time_limit_source` | 判题时限及来源（`dataset` = 原生，否则为默认值） |
| `memory_limit_bytes` | 内存上限 |
| `comparison` | **判题比对模式**：`token` 或 `float` |
| `tests.total` / `tests.by_origin` | 用例条数 / 按来源（`public`/`private`/`generated`）的分布 |
| `tests.cases_digest` | 写入用例集的 sha256，可与 `verification.json` 交叉校验 |
| `reference.sha256` | 与 `reference.py` 对应 |
| `n_python3_solutions` / `n_incorrect_solutions` | 原始数据中 Python3 正确解 / 错误解的条数 |

`difficulty_bin_source` 与 `category_source` 是刻意保留的：TACO 的分层是粗粒度映射、
题面关键词启发式更弱，下游做分层分析时应把不同来源分开看待，而不是混在一起。

### `tests.json`

裸数组 `[{"input": "...", "expected": "..."}, ...]`，与加载器的读取契约一致。

**公开用例是「参考解验证用例集」的前缀**，不含任何未经验证的用例 ——
用例集的 sha256 记录在 `meta.json` 与私有 `verification.json` 里，可交叉校验。

### `variants.json`（公开侧）

| 字段 | 说明 |
|---|---|
| `name` | 候选名（公开侧只有 `correct` / `natural`） |
| `language` / `code` | 实现语言与代码 |
| `steps` | `{"S1": "...", ..., "S7": "..."}` 七步题解正文 |
| `code_source` | 代码来自模型还是参考解 |
| `steps_sha256` | 七步正文的哈希，用于对照实验的等价性检查 |
| `judge` | 该代码在公开用例上的实测结果（`exec_status` / `passed` / `total` / `comparison`） |

**公开候选不含任何 ground truth 标注。**

### `labels.json`（私有侧）

键是候选名，值是标注：

| 字段 | 说明 |
|---|---|
| `kind` | `correct` / `natural` / `injected` / `ac_invalid` |
| `exec_status` | `AC`/`WA`/`TLE`/`MLE`/`RE`/`CE`/`NO_CODE`（沙箱实测） |
| `process_valid` | `true` / `false` / `null`（`null` = 无 ground truth，如 `natural`） |
| `first_error` | 首个错误步骤 `S1`..`S7`，或 `null` |
| `error_type` | `E1`..`E8` 或 `NONE` |
| `ac_but_invalid` | **过程不成立 且 代码实测 AC**（由程序推导，非人工声明） |
| `confidence` / `basis` | 标注置信度与依据 |
| `injected` / `changed_section` / `code_unchanged` | 构造细节，便于审计 |

---

## 不在这份数据里的东西

- **首个错误步骤 / 错误类型的标注** —— 属于 `private/`
- **注入错误的候选过程**（含 `ac_invalid`）—— 属于 `private/`。
  这些候选的**名字**本身（`injected-E2-S3`）就写明了答案，因此正文与名字都不能公开
- **参考解验证的逐用例证据** —— 属于 `private/<pid>/verification.json`
  （其中含失败用例的输出片段，会暴露期望答案）

---

## 复现

```bash
# 端到端构建（扫描 → TACO 关联 → 分层采样 → 实测验证 → 落盘）
python scripts/build_dataset.py --stage all --n 180

# 构造四类候选过程（需要 Hy3 API Key）
python scripts/build_candidates.py

# 校验候选的三条不变量（前缀不变 / 变化起点 / ac_invalid 代码不变）
python scripts/build_candidates.py --validate
```

各阶段可独立运行，详见 [`../docs/DATASET.md`](../docs/DATASET.md) §二。
`manifest.json` 内含完整配置快照与随机种子，同配置同种子可复现同一份数据集。

跑基准：

```bash
hy3-audit benchmark --dataset data/processeval-cp \
                    --private private/processeval-cp --out results
```
