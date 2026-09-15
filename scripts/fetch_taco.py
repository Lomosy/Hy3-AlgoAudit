"""获取 TACO 数据集并建立标签索引。

用法（项目根目录下）：

    python scripts/fetch_taco.py                # 下载 + 建索引
    python scripts/fetch_taco.py --skip-download  # 只用已有分片重建索引

说明：本机 huggingface.co 不可达，统一走 hf-mirror.com 镜像（实测约 13 MB/s，
全量 2.42 GB 约需 3 分钟）。索引只保留关联需要的列，体积远小于原始分片。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hy3_algoaudit.builder import pool, taco  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="下载 TACO 并建立标签索引")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--skip-download", action="store_true", help="跳过下载")
    args = parser.parse_args()

    cfg = pool.load_config(args.config)

    raw_dir = Path(cfg["paths"]["taco_dir"]) / "raw"
    if args.skip_download:
        raw_paths = sorted(raw_dir.glob("*.parquet"))
        if not raw_paths:
            print(f"未在 {raw_dir} 找到已下载的分片", file=sys.stderr)
            return 1
        print(f"使用已有分片 {len(raw_paths)} 个")
    else:
        raw_paths = taco.download_taco(cfg)

    taco.extract_index(cfg, raw_paths)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
