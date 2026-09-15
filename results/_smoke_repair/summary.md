# 修复策略对比实验（§17 Refine@k）

- 数据集：`demo/samples`（2 题，初始失败 1 题）
- 最大修复轮数 k = 2

| 策略 | 整体 AC 率 | Refine@1 | Refine@2 | 平均修复轮次 | token 开销 |
|---|---|---|---|---|---|
| A. One-shot（不修复） | 0.5 | 0.0 | 0.0 | None | 0 |
| B. Execution-only（盲修） | 0.5 | 0.0 | 0.0 | None | 6240 |
| C. Process-guided（首错定向） | 0.5 | 0.0 | 0.0 | None | 18436 |

> Refine@k 在初始失败的题目子集上计算。
