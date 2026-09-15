"""数据构建：从 CodeContests 原始数据抽取并构建 ProcessEval-CP 评测集。

模块划分：
    riegeli.py    Riegeli/records 纯 Python 读取器
    proto.py      ContestProblem proto 解码与字段号核对
    taco.py       TACO 数据获取与标签关联
    pool.py       题目池构建与过滤
    sample.py     分层采样（算法类别 × 难度）
    verify.py     参考解实测验证关卡
    dataset.py    评测集落盘与 manifest
    candidates.py 四类候选过程构造

构建脚本位于仓库根目录的 scripts/ 下（build_pool.py / build_dataset.py /
build_candidates.py）。产物路径统一取自 configs/dataset.yaml 的相对路径，
基准目录就是仓库根目录（下称 PROJECT_ROOT）。

为什么 PROJECT_ROOT 不直接取应用侧 hy3_algoaudit.config：
应用侧 config 只负责「运行期设置」（API 密钥、沙箱限额等），刻意不含路径常量；
数据构建是离线流程，需要的是仓库根目录。两者职责不同，不应互相耦合。
当前以 `_legacy.config` 为唯一来源（该包的定位见 hy3_algoaudit/__init__.py），
`_legacy` 未来被移除时再回落到本地推算，保证构建流水线不会因此中断。
"""

from __future__ import annotations

from pathlib import Path

try:  # 与流水线其余部分保持同一来源
    from .._legacy.config import PROJECT_ROOT
except ImportError:  # pragma: no cover - _legacy 移除后的兜底
    # __file__ = <root>/src/hy3_algoaudit/builder/__init__.py → parents[3] = <root>
    PROJECT_ROOT = Path(__file__).resolve().parents[3]

__all__ = ["PROJECT_ROOT"]
