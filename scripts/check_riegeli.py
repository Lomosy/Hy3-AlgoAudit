"""Riegeli 文件体检：逐个分片走一遍 chunk，报告结构异常。

用途：在构建评测集之前确认原始数据可完整解码。若某个分片出现
「未知压缩类型」或「长度不一致」，说明 chunk 边界推算失准或数据损坏，
本工具会打印可疑 chunk 的位置、头部字段与前后字节，便于定位。

用法：

    python scripts/check_riegeli.py                       # 检查全部分片
    python scripts/check_riegeli.py --shard <path>         # 检查单个文件
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hy3_algoaudit.builder import pool, proto, riegeli  # noqa: E402


def inspect_file(path: Path) -> dict:
    """走一遍所有 chunk，逐个尝试解码记录，记录第一处异常"""
    data = path.read_bytes()
    result = {
        "path": str(path),
        "size": len(data),
        "signature_ok": data[:64] == riegeli.FILE_SIGNATURE_64,
        "chunks": 0,
        "records": 0,
        "types": {},
        "error": None,
        "error_at": None,
        "proto_errors": 0,
    }

    try:
        headers = list(riegeli.iter_chunk_headers(data))
    except riegeli.RiegeliError as exc:
        result["error"] = f"chunk 遍历失败: {exc}"
        result["error_at"] = result["chunks"]
        return result

    for header in headers:
        result["chunks"] += 1
        result["types"][header.type_name] = result["types"].get(header.type_name, 0) + 1
        if header.num_records == 0 or header.chunk_type != riegeli.CHUNK_SIMPLE:
            continue
        body = b""
        try:
            body = riegeli.read_skipping_block_headers(
                data, header.data_pos, header.data_size
            )
            records = riegeli.decode_simple_chunk(body, header.num_records)
        except riegeli.RiegeliError as exc:
            result["error"] = f"{exc}"
            result["error_at"] = header.pos
            result["error_header"] = {
                "pos": header.pos,
                "data_pos": header.data_pos,
                "header_size": header.header_size,
                "data_size": header.data_size,
                "num_records": header.num_records,
                "decoded_data_size": header.decoded_data_size,
                "chunk_type": chr(header.chunk_type)
                if 32 <= header.chunk_type < 127
                else header.chunk_type,
                "first_bytes": body[:16].hex(),
            }
            result["hex_window"] = data[max(0, header.pos - 48) : header.pos + 80].hex()
            return result
        result["records"] += len(records)
        for raw in records:
            try:
                proto.parse_contest_problem(raw)
            except proto.ProtoError:
                result["proto_errors"] += 1
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Riegeli 分片体检")
    parser.add_argument("--shard", default=None, help="只检查指定文件")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    cfg = pool.load_config(args.config)
    if args.shard:
        targets = [Path(args.shard)]
    else:
        targets = [s.path for s in pool.discover_shards(cfg["paths"]["codecontests_dir"])]

    bad: list[dict] = []
    total_records = 0
    for path in targets:
        info = inspect_file(path)
        total_records += info["records"]
        status = "OK " if info["error"] is None and info["proto_errors"] == 0 else "BAD"
        print(
            f"  {status} {path.name:<46} chunk {info['chunks']:>4}  记录 {info['records']:>5}  "
            f"proto错误 {info['proto_errors']}",
            flush=True,
        )
        if info["error"] is not None:
            print(f"        异常：{info['error']}", flush=True)
            if info.get("error_header"):
                print(f"        头部：{info['error_header']}", flush=True)
            if info.get("hex_window"):
                print(f"        字节：{info['hex_window']}", flush=True)
            bad.append(info)

    print("-" * 72)
    print(f"检查 {len(targets)} 个文件，异常 {len(bad)} 个，累计记录 {total_records}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
