# ProcessEval-CP 数据与评测集构建说明

本文档记录评测集 **ProcessEval-CP** 的构建方法、全部关键决策的实测依据，以及已知限制。
目标不是「说明我们做了什么」，而是「让任何人能复现，并且知道每个数值是怎么来的」。

对应技术方案书：第十五章「数据与评测集建设方案」。

---

## 一、产物一览

公开部分（**入库**）：

```
data/processeval-cp/
├── manifest.json              构建清单：配置快照 + 各阶段统计 + 逐题索引
└── <pid>/
    ├── problem.md             完整题面
    ├── meta.json              元数据（难度、标签、时限、判题比对模式）
    ├── tests.json             测试用例 [{"input","expected"}, ...]（「已验证用例集」的前缀）
    ├── reference.py           参考解（实测全部公开用例 AC）
    └── variants.json          公开候选：correct + natural（不含任何标注）
```

私有部分（**不入 git**，冰山理论：核心细节不公开）：

```
private/processeval-cp/
└── <pid>/
    ├── variants.json          私有候选：injected + ac_invalid
    │                          （正文即答案，**名字**也泄露答案，故一律私有）
    ├── labels.json            全部候选的 ground truth 标注
    └── verification.json      参考解验证证据（逐用例哈希、耗时、比对模式、失败样本）
```

原始件与中间产物（不入库）：

```
data/
├── codecontests_data/         原始数据（2.9 GB）
├── taco/                      TACO 原始分片与索引（2.4 GB）
└── interim/
    ├── pool.jsonl             全量题池（含未通过门槛的题，便于统计淘汰原因）
    ├── enriched.jsonl         叠加 TACO 标签与分层后的题池
    ├── selected.jsonl         采样结果（超额采样，待验证）
    ├── verify_report.json     逐题验证证据与淘汰明细
    └── verified.jsonl         通过验证的题目（含参考解代码）
```

`<pid>` 采用 `cc-{split}-{shard:03d}-{record:04d}`，直接编码「在哪个分片的第几条记录」，
因此任何一道题都能原地回溯到原始数据，不需要额外的映射表。

**为什么是「每题一目录 + 标注外置」**：这套布局与应用侧
`hy3_algoaudit.benchmark.dataset.load_dataset()` 的读取契约**逐字段对齐** ——
一个目录就是一道题，题面/元数据/用例/参考解/候选各自独立成文件。而标注不写在
`variants.json` 里，而是外置到 `private/` 下，于是：

- 公开仓库可以被第三方直接跑基准（`load_dataset(root)` 就能起来）；
- 但拿不到答案（`process_valid` / `first_error` / `error_type` 全在私有侧）。

---

## 二、构建流程

一条命令走完五阶段：

```bash
python scripts/build_dataset.py --stage all --n 180
```

分阶段执行（调参时更快）：

```bash
python scripts/build_dataset.py --stage scan                        # 原始数据体检
python scripts/build_dataset.py --stage pool                        # 题池
python scripts/build_dataset.py --stage enrich --reuse-pool         # TACO 关联
python scripts/build_dataset.py --stage sample --n 180 --reuse-pool --reuse-enriched
python scripts/build_dataset.py --stage verify --workers 8
python scripts/build_dataset.py --stage write --n 180 --prune
```

| 阶段 | 脚本 | 做什么 | 耗时量级 |
|---|---|---|---|
| 1 体检 | `check_riegeli.py` | 逐 chunk 解码全部分片，确认原始数据可完整读取 | 3.5 min |
| 2 题池 | `build_pool.py` | 抽取轻量元数据 + 施加硬门槛过滤 | 3.7 min |
| 3 关联 | `build_dataset.py --stage enrich` | 关联 TACO，补算法标签与难度分层 | 10 s |
| 4 采样 | `build_dataset.py --stage sample` | 「算法类别 × 难度箱」分层采样 | 1 s |
| 5 实测 | `build_dataset.py --stage verify` | 逐条参考解跑全部用例，淘汰不可信题目 | 见下方 |
| 6 落盘 | `build_dataset.py --stage write` | 写「每题一目录」+ 私有验证证据 + 清单 | 1 s |
| 7 候选 | `build_candidates.py` | 构造四类候选过程（需 API Key） | 见 §四 |

阶段 5 的耗时完全由「TLE 用例数 × 候选解条数」决定。实测在提前中止优化之后，
11 道题 4 进程 **48 秒**；优化前同样 11 道题跑 13 分钟仍未结束（详见 §3.3）。

---

## 三、关键决策与实测依据

### 3.1 Riegeli 必须纯 Python 自解析，而 chunk 边界推算有个 24 字节陷阱

CodeContests 用 Riegeli/records 格式存储，而 Google 的 `riegeli` Python 包**未发布到 PyPI**。
本项目按官方文件格式规范自实现了最小读取器（`builder/riegeli.py`）。

规范里 `chunk_end` 的计算要同时考虑「数据本身长度」和「记录条数下界」：

```
chunk_end = max(AddWithOverhead(chunk_begin, kChunkHeaderSize + data_size),
                RoundUpToPossibleChunkBoundary(chunk_begin + num_records))
```

第一版实现直接用 `chunk_begin + 40` 作为数据起点。这在**40 字节的 chunk header 自身跨过
65536 边界**时会偏移 24 字节（少算了一个 block header），把压缩类型字节读成 `200`，
报出「未知压缩类型 200」。这个 bug 在 130 个分片里只在 **00028 号分片第 8 个 chunk** 触发 ——
属于「一跑全量才炸、且只炸一次」的类型。

修法是把读取封装成返回结束偏移的 `read_span()`，让 `ChunkHeader` 记录真实的 `data_pos`，
任何调用方都不允许再用 `pos + 40` 推算。修复后全量体检结果：

```
检查 130 个文件，异常 0 个，累计记录 13610
```

> 可复现：`python scripts/check_riegeli.py`（任一异常都会打印可疑 chunk 的头部字段与前后字节）

### 3.2 protobuf 字段号不能靠 proto 文本推断，必须用真实数据核对

`ContestProblem` 的官方 proto 定义了字段语义，但**字段号与语义的对应必须实测确认**。
第一版按文本推断写下的字段号有四处是错的：

| 字段 | 第一版（错） | 实测（对） |
|---|---|---|
| `cf_contest_id` | 12 | **10** |
| `cf_index` | 13 | **12** |
| `cf_points` | 19 | **13** |
| `incorrect_solutions` | 10 | **19** |

因此 `scripts/inspect_fields.py` 被设计成一道**闸门**：扫描真实记录、输出字段号直方图
（出现次数 / wire type / 样例值），核对通过后才允许写进 `proto.py`。
`proto.py` 的模块 docstring 里完整记录了核对结论。

顺带得到两条对数据结构有影响的实测结论：

- 全部记录均未出现 `input_file` / `output_file` → 数据集题目一律 stdio
- `language=1` 是 Python 2（`raw_input` / `xrange`），**在 Python 3 下必然失败**；
  只有 `language=3` 可直接运行。这是参考解筛选的第一道过滤器

### 3.3 「参考解实测」是整条链路的地基，但它必须提前中止

本评测集的所有下游指标（误报率、首错定位准确率、AC-but-Invalid 识别率）都建立在
「参考解确实正确」之上。所以判据只有一个：**某条 Python 3 解真实跑过全部用例并全 AC**。

第一版实现遇到失败用例后仍会把剩余用例跑完（为了留下完整证据）。问题在于错误解
往往是「未优化的朴素写法」，在 Python 下逐条 TLE，而 CF 题时限可达 2～4 秒 ——
单条错误解就能烧掉几十秒，而每题最多试 8 条解。实测 11 道题跑 13 分钟未结束。

改为「遇到第一个非 AC 立即停止」后，同样 11 道题 **48 秒**完成。判定目标本来就是
「这条解是否全 AC」，一旦出现非 AC 就没有继续的必要。

> 相关代码：`builder/verify.py::_run_cases(abort_on_failure=True)`。
> 注意 `cases_passed` 在中止时只是「中止前通过数」，失败证据里会带 `aborted_early` 标记。
>
> 执行与比对**统一走应用侧 `sandbox`**（`LocalSandbox` + `sandbox.judge.compare_output`），
> 不再自带一套 `run_python`。理由见 §3.5 末尾：离线验证与在线判题必须是同一套口径，
> 否则同一份参考解会在数据集里是 AC、在应用里是 WA。顺带一个性能收益 ——
> `prepare()` 只需落盘一次，后续用例反复 `run_once()`，不必每条用例重启解释器。

### 3.4 「答案不唯一」的题必须排除 —— 而且句式过滤要先校准误杀

Codeforces 上有一类题由**特殊判题器**（special judge）判定：只要输出是任一合法方案即可 AC。
数据集却只保存了**其中一个**参考答案。实测例：

```
题目：1141_G Privatization of Roads in Treeland
期望：3 \n 3 1 2 3 2 3 1 1 2
某解：3 \n 1 1 2 3 2 3 1 3 1     ← 首个数一致，另一种合法方案
该题 8 条 Python3 解全部被判 WA
```

题面里通常有 `print any` / `multiple answers` / `If there are several ...` 这类句式，
因此按题面正则排除（`pool.detect_multiple_answers`）。

**第一版句式表过宽，误杀了真题目**，实测反例：

| 句式 | 被误杀的题面原文 | 为什么是误杀 |
|---|---|---|
| `in any order` | "The spells can be used any number of times **in any order**." | 说的是输入里操作的使用顺序，答案唯一 |
| `does not matter` | "the point where Wabbit ends up at **does not matter**." | 说的是无关紧要的细节，不是「输出不唯一」 |

移除这两条、并把其余全部收紧为「必须出现在 `print` / `output` 的输出说明语境里」之后：

| 版本 | 排除的题目数 | 通过门槛的题目数 |
|---|---|---|
| 过宽版 | 2337 | 6582 |
| 校准版 | 1520 | **7087** |

找回 817 道被误杀的题。召回率下降由后续「参考解实测」关卡兜住：漏网的题会因参考解
跑不通过而被淘汰。

此外，验证阶段会额外标记**疑似漏网**：多条解全部以 WA 收场、且输出与期望的 token
个数相同（结构对、方案不同）的题会被单独列出供人工复核，不改变淘汰判定。
见 `builder/verify.py::suspected_special_judge`。

### 3.5 浮点输出题的精确比对会误判 —— 用一个 `comparison` 字段贯穿离线与在线

```
题目：437_D The Child and Zoo
期望：16.6666666667          ← 固定 10 位小数
解输出：16.666666666666668   ← Python 默认精度
5 条 Python3 解全被判 WA，而它们在 Codeforces 上都是 AC
```

Codeforces 对这类题用「绝对或相对误差 1e-6」判定。因此比对统一到一处
（`sandbox/judge.py::compare_output`），三种模式：

| 模式 | 判据 | 用途 |
|---|---|---|
| `exact` | 逐行比对（去行尾空白） | 需要严格格式的题 |
| `token` | 空白分词后逐 token 相等（**默认**，Codeforces 风格） | 绝大多数题 |
| `float` | token 结构必须一致，**数值 token** 允许 `|got-want| ≤ max(1e-6, 1e-6·|want|)` | 浮点输出题 |

两条设计约束值得记录：

1. **`float` 模式只放宽数值 token，不放宽结构。** 早期实现是「把两边所有数字抓出来逐个比」，
   这会放过纯文本的错答 —— `YES` 与 `Impossible` 都不含数字，于是「相等」。现在的做法是
   先要求 token 个数与位置一一对应，只有双方都能**完整**解析为数字的 token 才启用容差。
2. **判据取 CF 约定**：`max(abs_tol, rel_tol·|expected|)`。纯绝对容差在 `1e6` 量级下过严，
   纯相对容差在 0 附近过松，Codeforces 用的是二者取宽。

验证时采用**两段式**：先 `token`（最严格）；仅当失败原因全是 WA 且失败样本里存在
「长度对应」的数字 token 时，才用 `float` 再试一次。生效的题目会记录在：

- `private/processeval-cp/<pid>/verification.json` 的 `comparison` 字段（验证证据）
- `data/processeval-cp/<pid>/meta.json` 的 `comparison` 字段（**交付给应用侧的口径**）
- `data/processeval-cp/manifest.json` 的 `stages.comparison_modes`（汇总，能一眼看出哪些题不能按最严模式判）

> **下游必须按 `meta.json` 的 `comparison` 字段构造判题器。** 应用侧已按此打通：
> `AlgoAuditPipeline.solve_mode/evaluate_mode(comparison=...)` →
> `judge_program(..., comparison=...)`；基准运行器直接传 `ProblemCase.comparison`；
> CLI 也开放了 `hy3-audit judge --comparison {exact,token,float}`。

实测（本次改造后对新实现复跑，确认两段式仍然正确）：

```
cc-train-008-0043  437_D. The Child and Zoo
  attempt 1: WA   token  0/50    ← 分词精确比对的必然结果
  attempt 2: AC   float  50/50   ← 容差比对通过
  首次失败样本：'16.666666666666668\n' vs '16.6666666667\n'
```

> 口径迁移：早期版本把这个字段叫 `exact` / `float_tolerance`，其中 `exact` 的语义
> （归一化后分词比对）与新 `token` **完全相同**。`verify.normalize_comparison()`
> 负责把历史缓存里的旧值归一，避免 `interim/` 里的旧产物把两种口径混进同一份数据集。

### 3.6 TACO 的实际价值：补齐 643 道题的难度分层

TACO 提供两样 CodeContests 没有的东西：**Codeforces 的细粒度算法标签**，以及
**AtCoder 题目的难度**。后者是关键 —— CodeContests 里 Aizu/AtCoder 题既没有 `cf_rating`
也没有可用的 Difficulty 枚举，本来无法参与「按难度分层」：

| 分层来源 | 题目数 |
|---|---|
| `cf_rating`（Codeforces 原生评星） | 5569 |
| `taco`（TACO 的 EASY/MEDIUM/HARD…） | 680 |
| `cc_enum`（CodeContests Difficulty 枚举） | 13 |
| **无法分层（`none`）** | **1927** |

用 `cf_rating + Difficulty 枚举` 只能分层 5619 / 8189 道；叠加 TACO 后达到 6262 / 8189，
**净增 643 道**。剩余 1927 道（主要是 TACO 未命中的 Aizu 题）在采样中被排除，
`manifest.json` 的 `stages.sampling.excluded_unrated` 会明确记录这个数字。

TACO 的关联率（以 8189 道通过门槛的题为例）：

| 方式 | 题数 | 说明 |
|---|---|---|
| `contest_index`（比赛号+题号精确对齐） | 4558 | 覆盖绝大多数 Codeforces 题 |
| `title`（归一化标题匹配） | 1171 | 主要覆盖 AtCoder |
| `none` | 1358 | 主要是 Aizu —— TACO 中 Aizu 记录的 name/url 均为 `None`，无可用的关联键 |

> 网络注意：本机 `huggingface.co` 不可达，统一走 `hf-mirror.com`。该镜像会拦截
> urllib 默认 User-Agent（返回 403），必须显式带上浏览器 UA。镜像支持 HTTP Range
> 但**不返回正文**，因此无法做懒惰列读取，改为逐分片整体下载再抽取所需列。

### 3.7 分层采样的三条修正

采样目标是在「算法类别 × 难度箱」矩阵上按配额抽题。三个坑都是实测踩出来的：

**（1）配额必须按「放宽后的候选池」统计，不能只看首要类别。**
一道题常同时命中多个算法标签。若配额只看首要类别，`Graph@1600-1999` 这类没有
「首要供给」的单元就永远分不到配额，而放宽后可选的题其实存在 —— 表现为
「配额总数低于目标题数」。

**（2）类别保底。** 严格的按供给比例分配会把稀缺类别（`BinarySearch` 在池中占比约 6%）
在小样本上四舍五入成 0，直接违背「六类算法全覆盖」的设计要求。现在会给每个有货的
类别先保底 1 题。

**（3）兜底填充。** 放宽供给后配额是「期望值」而非硬约束（一道题可能被多个单元同时
计入供给），第一轮抽完若有缺口，就用剩余未选题按首要类别所属单元补齐到目标题数。
补进来的题在 `diagnostics.filled_beyond_quota` 里单独计数，不与配额口径混淆。

配额分配还有一个容易被忽略的正确性问题：**类别上限的裁剪不能双重扣减**。
`quota` 是增量更新的，`category_total()` 读到的已是本轮最新值，若再额外减去
「本轮已给该类别多少」，额度会被扣两次 —— 实测表现为「目标 8 题只选出 6 题」。

### 3.8 交互题必须在入池时排除 —— 而且「query」句式不能当判别依据

交互题的输入是**运行时产生的**（程序提问、判题器回答），静态测试用例无从表达，
判题结果必然没有意义。它不会被"误判为 AC"，而是会**白白烧掉验证预算并污染审计线索**。

发现路径是一次实测淘汰：`1146_C Tree Diameter` 两条 Python3 解一条 RE、一条 WA，
输出看起来像命令序列而不是答案，而题面里**一次 `interactive` 都没出现**，
只有 6 处 `flush`。这说明「等 interactive 这个词」的思路本身就是错的。

于是按「信号 + 校准」的老办法处理，在 630 条原始记录上实测：

| 候选信号 | 命中 | 人工核对结果 |
|---|---|---|
| `flush`（题面要求刷新输出） | 4 | **全部为真交互题** |
| `interactive`（关键词） | 3 | **全部为真交互题** |
| `ask/make a query` 句式 | 2 | **全部误杀**：`117_D "You should print the query results modulo mod"` 是普通离线题 |
| `at most N queries` 句式 | 1 | **全部误杀**：`673_F "handle q queries of three types"` 是普通离线题 |

后两条被删除 —— `query` 在离线题里是常见名词（查询操作），不具备判别力。
保留两条信号：命中 4/630（约 **0.63%**，全量 13610 条对应约 80 余道题）。

顺带验证了一个**不需要**再加的信号：题面里的 `Interaction` 段落标题。样本里
「含 `interaction` 但未被前两条信号命中」的题目是 **0/630** —— 凡是交互题都会要求 flush，
所以这个信号冗余，而它在物理/图论题面里还可能指「粒子相互作用」，属于纯风险。

> 相关代码：`builder/pool.py::detect_interactive`，开关 `filters.exclude_interactive`。

### 3.9 两条工程约束
**公开测试用例必须是「参考解验证用例集的前缀」。** 否则公开用例的正确性只能靠数据集
自身背书。题面/用例/参考解三者共享同一套选择规则（`verify.select_cases_with_origins`），
`meta.json` 的 `tests.cases_digest` 与 `verification.json` 的 `cases_digest`
（验证时的完整用例集）可交叉校验。

**目录必须与清单一致。** 改过采样规模或过滤条件后重跑，旧**题目目录**会留在目录里，
而 `manifest.json` 只列本次入选的题；下游按目录遍历（`load_dataset` 正是按目录遍历）
就会读到「不在评测集里」的题。落盘时会主动检测陈旧目录（`--prune` 才真的删除，
默认只提示）。

---

## 四、候选过程构造

对应技术方案书 15.3，每道题构造四类候选：

| 类型 | 内容 | 用途 | 存放位置 |
|---|---|---|---|
| `correct` | 正确题解 + AC 实现 | 测误报率（FPR） | `data/processeval-cp/<pid>/variants.json` |
| `natural` | Hy3 正常解题输出 | 研究模型自发错误 | `data/processeval-cp/<pid>/variants.json` |
| `injected` | 指定步骤注入指定错误类型 | 首错定位 / 错误分类准确率 | `private/processeval-cp/<pid>/variants.json` |
| `ac_invalid` | 代码保持 AC，过程不成立 | AC-but-Invalid 识别率 | `private/processeval-cp/<pid>/variants.json` |

全部候选的标注汇总在 `private/processeval-cp/<pid>/labels.json`。

> **为什么注入候选连「正文」都要藏起来**：候选的**名字**是 `injected-E2-S3` ——
> 「目标是 S3、错误类型是 E2」已经写在名字里。所以带语义名字的候选一律只出现在私有侧，
> 公开侧只有 `correct` / `natural`。

```bash
python scripts/build_candidates.py --dry-run --limit 2   # 只看提示词，不花额度
python scripts/build_candidates.py --limit 2             # 小样本
python scripts/build_candidates.py                       # 全量
python scripts/build_candidates.py --report-only         # 只汇总
python scripts/build_candidates.py --validate            # 校验三条不变量
```

**核心机制：只重写目标步骤及其后续，前面的步骤逐字节保留原样。**

做法是让模型只输出从目标步骤开始的章节，再由程序把「原本正确的前缀」与「模型输出的
后缀」拼接起来。这样带来三个好处：

1. **首个错误步骤是程序保证的**，不依赖模型自我声明，标注天然可信；
2. 目标步骤之前的内容与 `correct` 候选完全一致，不同候选之间构成严格的对照实验，
   首错定位的评测因此是干净的 A/B 对比；
3. 模型即使不听话多输出了别的内容，也不会污染前缀。

`ac_invalid` 更进一步：**S7 由程序强制替换成已验证 AC 的参考解，不采信模型输出的代码**，
这样「结果正确」这个前提严格成立，「过程不成立」才是唯一的变量。

### 4.1 标注模式（`labels.json`）

| 字段 | 取值 | 怎么来 |
|---|---|---|
| `exec_status` | `AC`/`WA`/`TLE`/`MLE`/`RE`/`CE`/`NO_CODE` | 沙箱实测（与应用侧同一套 `judge_program`，同样按题目 `comparison` 模式比对） |
| `process_valid` | `true`/`false`/`null` | 构造保证；`natural` 为 `null`（无 ground truth） |
| `first_error` | `S1`..`S7` / `null` | 构造保证（注入/改写的那一步） |
| `error_type` | `E1`..`E8` / `NONE` | 构造保证 |
| `ac_but_invalid` | `true`/`false` | **程序推导**：`process_valid is False` 且 `exec_status == "AC"` |
| `confidence` / `basis` | 文本 | 标注依据，便于审计 |

`ac_but_invalid` 刻意由程序推导而非人工声明 —— 它正是「结果正确但过程不成立」的定义，
用两个已实测的事实合成，比再引入一个人工判断更可靠。

### 4.2 三条不变量（`--validate` 会逐题检查）

这三条不变量定义了「首错标注为什么可信」，任何一条被破坏，首错定位的评测就不再干净：

| # | 不变量 | 破坏了会怎样 |
|---|---|---|
| 1 | **前缀不变**：`injected` 候选在目标步骤之前与 `correct` 逐字节相同 | A/B 对照同时变了两个变量 |
| 2 | **变化起点**：`injected` 的变化必须恰好从目标步骤开始 | 真实首错可能早于标注值 |
| 3 | **代码不变**：`ac_invalid` 的代码与 `reference.py` 逐字节相同 | 「结果正确」这个前提不成立 |

另附结构检查：七节齐全、S7 能解析出代码块、`correct` 候选实测为 AC。

错误类型与注入步骤的对应（E1～E8 出处：技术方案书第九章）：

| 类型 | 名称 | 注入步骤 |
|---|---|---|
| E1 | 题意理解错误 | S1 |
| E2 | 算法选择错误 | S3 |
| E3 | 推导或逻辑错误 | S4 |
| E4 | 条件或边界遗漏 | S6 |
| E5 | 复杂度错误 | S5 |
| E6 | 题解与代码不一致 | S7 |
| E7 | 实现错误 | S7 |
| E8 | 无依据推断或幻觉 | S2 |

注入类型按题目顺序轮转分配，保证 E1～E8 覆盖均衡。

**已知限制**：`natural` 候选没有 ground truth —— 它的过程对错正是待研究的对象，
因此只记录 `judge` 结果这一客观事实，不伪造 `process_valid` 标注。
`correct` 候选的「过程正确」由模型自洽性保证，未逐句人工复核，标注置信度记为
`high`（代码由模型自己写）或 `medium`（S7 兜底复用参考解，存在题解-代码一致风险）。

**遗留依赖**：候选的**生成**仍在用 `_legacy/solver` 的「七节 Markdown」提示词与解析器
（这条路径已经实测跑通、注入成功率稳定）；而应用侧 solver 现在产出的是 JSON
`StructuredSolution`。二者只在**落盘格式**上收敛（`variants.json` 的 `steps` 映射 + 独立
`code` 字段，正是 `benchmark.dataset` 的读取契约），生成阶段尚未统一到 JSON ——
这是后续可以清理的一处技术债。

---

## 五、复现步骤

```bash
# 0) 环境（Python >= 3.10，推荐 3.11）
conda create -n hy3 python=3.11 -y && conda activate hy3
pip install -e .
pip install -r requirements.txt
pip install -e ".[dev]"          # pytest（跑 tests/）
pip install -e ".[sandbox]"      # psutil（沙箱内存监控，可选但建议）

# 1) 原始数据：CodeContests（Riegeli，2.9 GB）
#    放到 data/codecontests_data/dm-code_contests/
#    来源：https://github.com/google-deepmind/code_contests

# 2) TACO（2.4 GB，走 hf-mirror 镜像）
python scripts/fetch_taco.py

# 3) 体检 + 构建
python scripts/check_riegeli.py
python scripts/build_dataset.py --stage all --n 180

# 4) 候选过程（需要 .env 里配置 HY3_BASE_URL / HY3_API_KEY）
python scripts/build_candidates.py
python scripts/build_candidates.py --validate     # 三条不变量
```

`.env` 从 `.env.example` 复制后填写，**绝不入库**（`.gitignore` 已覆盖）。

### 5.1 用这份数据跑基准

```bash
# 单元测试
python -m pytest -q

# 只加载公开部分（第三方拿到仓库就能跑，但没有答案可评）
hy3-audit benchmark --dataset data/processeval-cp --out results

# 带上私有材料（注入候选 + ground truth），这才是能出指标的完整跑法
hy3-audit benchmark --dataset data/processeval-cp \
                    --private private/processeval-cp --out results
```

### 5.2 当前进度

| 项 | 值 |
|---|---|
| 目标规模 | **180 题**（技术方案书 15.2 为 60～100，本项目向上扩到 150～200） |
| 已端到端跑通 | 8 题（小样本验收，见 `data/processeval-cp/manifest.json`） |
| 分层维度 | 6 个算法类别 × 5 个难度箱 |
| 单题用例数 | 30（公开）／最多 50（验证用） |

扩到 180 题只需把上面的 `--n` 与 `dataset.n_problems` 一并设为 180 重跑；
采样器会自动按新的供给重新分配配额（`manifest.json` 的 `stages.sampling` 记录了
每个单元格的配额与实际值，便于核对）。

---

## 六、已知限制

1. **只支持 Python。** 本机没有 C/C++/Java 编译器，参考解与判题全部限定 Python 3。
   这同时意味着：C++ 参考解数量众多的题目在本数据集中被完全跳过。
2. **依赖 Python 执行性能。** 时限超过 8 秒的题目被直接排除；时限在 2～4 秒的题目
   里，Python 参考解仍可能比 C++ 慢数十倍，存在被误判 TLE 的风险（实测通过率良好，
   但这是一条系统性偏差）。
3. **`unclassified` 占比偏高。** 可归类题目约 73.5%，其余 26.5% 既没有 `cf_tags`
   也没有可识别的题面关键词，被排除在采样之外。这是当前的准确率天花板。
4. **答案不唯一的题只能靠题面句式排除**，仍有漏网（每轮验证会列出「疑似漏网」清单
   供人工复核）。
5. **部分题目的 TACO 难度是粗粒度映射。** TACO 的 EASY/MEDIUM/HARD 七级被压缩到五档
   评级箱，且映射表是人工设定的。凡是用到 TACO 难度的地方，`difficulty_bin_source`
   都会标记为 `taco`，下游分析时应把这一部分单独看待。
6. **`natural`/`correct` 候选的过程正确性未经人工逐句复核**（见 §四）。
