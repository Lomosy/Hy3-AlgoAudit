"""Riegeli/records 读取器（纯 Python 实现）

为什么不用官方库：Google 的 `riegeli` Python 包**未发布到 PyPI**，
而 CodeContests 原始数据正是 Riegeli 格式。本模块按官方文件格式规范
自实现最小读取器，只支持本数据集实际用到的部分：

- chunk_type = 0x72 'r'（Simple chunk）——实测 84/86 chunk 均为该类型
- chunk_type = 0x73 's'（文件签名）、0x6d 'm'（元数据）、0x70 'p'（填充）
  —— num_records 恒为 0，直接跳过
- chunk_type = 0x74 't'（Transposed chunk）——本数据集未出现，遇到时跳过并计数

规范出处：
https://github.com/google/riegeli/blob/master/doc/riegeli_records_file_format.md

关键常量与公式（均来自规范「Implementation notes」）：

    kBlockSize        = 1 << 16
    kBlockHeaderSize  = 24
    kUsableBlockSize  = kBlockSize - kBlockHeaderSize = 65512
    kChunkHeaderSize  = 40

    NumOverheadBlocks(pos, size) = (size + (pos + kUsableBlockSize - 1) % kBlockSize) // kUsableBlockSize
    AddWithOverhead(pos, size)   = pos + size + NumOverheadBlocks * kBlockHeaderSize
    RemainingInBlock(pos)        = kBlockSize - 1 - (pos + kBlockSize - 1) % kBlockSize
    SaturatingSub(a, b)          = a - b if a > b else 0
    RoundUpToPossibleChunkBoundary(pos) = pos + SaturatingSub(RemainingInBlock(pos), kUsableBlockSize - 1)
    chunk_end = max(AddWithOverhead(chunk_begin, kChunkHeaderSize + data_size),
                    RoundUpToPossibleChunkBoundary(chunk_begin + num_records))
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

K_BLOCK_SIZE = 1 << 16
K_BLOCK_HEADER_SIZE = 24
K_USABLE_BLOCK_SIZE = K_BLOCK_SIZE - K_BLOCK_HEADER_SIZE
K_CHUNK_HEADER_SIZE = 40

# chunk_type 取值（ASCII 单字节）
CHUNK_FILE_SIGNATURE = 0x73  # 's'
CHUNK_FILE_METADATA = 0x6D  # 'm'
CHUNK_PADDING = 0x70  # 'p'
CHUNK_SIMPLE = 0x72  # 'r'
CHUNK_TRANSPOSED = 0x74  # 't'

CHUNK_TYPE_NAMES = {
    CHUNK_FILE_SIGNATURE: "file_signature",
    CHUNK_FILE_METADATA: "file_metadata",
    CHUNK_PADDING: "padding",
    CHUNK_SIMPLE: "simple",
    CHUNK_TRANSPOSED: "transposed",
}

# 压缩类型（chunk data 首字节）
COMPRESSION_NONE = 0x00
COMPRESSION_BROTLI = 0x62  # 'b'
COMPRESSION_ZSTD = 0x7A  # 'z'
COMPRESSION_SNAPPY = 0x73  # 's'

# 文件头前 64 字节固定（可用作完整性自检）
FILE_SIGNATURE_64 = bytes.fromhex(
    "83af70d10d884a3f0000000000000000"
    "4000000000000000" "91bac23c9287e1a9"
    "0000000000000000" "e19f13c0e9b1c372"
    "7300000000000000" "0000000000000000"
)


class RiegeliError(RuntimeError):
    """Riegeli 文件结构异常"""


@dataclass(frozen=True)
class ChunkHeader:
    """一个 chunk 的头部信息。

    pos      — 该 chunk 头部在文件中的起始偏移
    data_pos — 该 chunk 数据区的起始偏移。**不要用 pos + 40 代替**：
               若 40 字节头部正好跨越 65536 边界，中间会插入 24 字节 block header。
    """

    pos: int
    data_pos: int
    data_size: int
    chunk_type: int
    num_records: int
    decoded_data_size: int

    @property
    def type_name(self) -> str:
        return CHUNK_TYPE_NAMES.get(self.chunk_type, hex(self.chunk_type))

    @property
    def header_size(self) -> int:
        """头部实际占用的文件字节数（被 block header 打断时为 40+24）"""
        return self.data_pos - self.pos


def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    """读取一个 base128 varint64，返回 (值, 新位置)"""
    value = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise RiegeliError("varint 越界，文件可能已损坏")
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
        if shift > 63:
            raise RiegeliError("varint 超过 64 位")


def _saturating_sub(a: int, b: int) -> int:
    return a - b if a > b else 0


def _remaining_in_block(pos: int) -> int:
    return K_BLOCK_SIZE - 1 - (pos + K_BLOCK_SIZE - 1) % K_BLOCK_SIZE


def _num_overhead_blocks(pos: int, size: int) -> int:
    return (size + (pos + K_USABLE_BLOCK_SIZE - 1) % K_BLOCK_SIZE) // K_USABLE_BLOCK_SIZE


def add_with_overhead(pos: int, size: int) -> int:
    """加上 size 字节后，算上途中插入的 block header 占用"""
    return pos + size + _num_overhead_blocks(pos, size) * K_BLOCK_HEADER_SIZE


def round_up_to_possible_chunk_boundary(pos: int) -> int:
    return pos + _saturating_sub(_remaining_in_block(pos), K_USABLE_BLOCK_SIZE - 1)


def chunk_end(chunk_begin: int, data_size: int, num_records: int) -> int:
    """按规范计算 chunk 的结束偏移"""
    return max(
        add_with_overhead(chunk_begin, K_CHUNK_HEADER_SIZE + data_size),
        round_up_to_possible_chunk_boundary(chunk_begin + num_records),
    )


def read_span(data: bytes, pos: int, size: int) -> tuple[bytes, int]:
    """从 pos 读取 size 个「逻辑字节」，途中跳过 65536 整数倍处的 24 字节 block header。

    规范：block header 打断 chunk，因此它不属于 chunk 数据本身。
    返回 (读到的字节, 读完后在**文件中的真实偏移**)。

    为什么必须返回结束偏移：chunk header（40 字节）本身也可能被 block header 打断，
    此时 chunk 数据的起始位置不是 `chunk_begin + 40`，而是 `chunk_begin + 40 + 24`。
    早期版本直接用 `pos + K_CHUNK_HEADER_SIZE` 定位数据起点，会在这种 chunk 上
    偏移 24 字节，把压缩类型字节读错（实测在 00028 号分片第 8 个 chunk 触发）。
    """
    out = bytearray()
    remaining = size
    while remaining > 0:
        if pos >= len(data):
            break
        if pos % K_BLOCK_SIZE == 0:
            pos += K_BLOCK_HEADER_SIZE
            continue
        take = min(remaining, K_BLOCK_SIZE - (pos % K_BLOCK_SIZE), len(data) - pos)
        if take <= 0:
            break
        out += data[pos : pos + take]
        pos += take
        remaining -= take
    if remaining > 0:
        raise RiegeliError(f"读取 {size} 字节时提前结束（还差 {remaining} 字节）")
    return bytes(out), pos


def read_skipping_block_headers(data: bytes, pos: int, size: int) -> bytes:
    """read_span 的便捷包装，只要字节不要结束偏移"""
    return read_span(data, pos, size)[0]


def iter_chunk_headers(data: bytes) -> Iterator[ChunkHeader]:
    """顺序遍历文件中所有 chunk 的头部"""
    pos = 0
    total = len(data)
    while pos + K_CHUNK_HEADER_SIZE <= total:
        raw, data_pos = read_span(data, pos, K_CHUNK_HEADER_SIZE)
        header = ChunkHeader(
            pos=pos,
            data_pos=data_pos,
            data_size=int.from_bytes(raw[8:16], "little"),
            chunk_type=raw[24],
            num_records=int.from_bytes(raw[25:32], "little"),
            decoded_data_size=int.from_bytes(raw[32:40], "little"),
        )
        yield header
        nxt = chunk_end(header.pos, header.data_size, header.num_records)
        if nxt <= header.pos:
            raise RiegeliError(f"chunk 长度计算异常，pos={pos}")
        pos = nxt


def _decompress(block: bytes, compression_type: int) -> bytes:
    """解压一个压缩块。

    规范：任何压缩块前方都带一个 varint64 表示解压后大小（compression_type=0 时无前缀）。
    """
    if compression_type == COMPRESSION_NONE:
        return block

    expected, offset = read_varint(block, 0)
    payload = block[offset:]

    if compression_type == COMPRESSION_ZSTD:
        import zstandard

        out = zstandard.ZstdDecompressor().decompressobj().decompress(payload)
    elif compression_type == COMPRESSION_BROTLI:
        import brotli

        out = brotli.decompress(payload)
    elif compression_type == COMPRESSION_SNAPPY:
        raise RiegeliError(
            "本数据集未使用 Snappy，且为避免 C 扩展编译问题未引入 python-snappy 依赖"
        )
    else:
        raise RiegeliError(f"未知压缩类型 {compression_type!r}")

    if len(out) != expected:
        raise RiegeliError(f"解压后长度不符：期望 {expected}，实际 {len(out)}")
    return out


def decode_simple_chunk(body: bytes, num_records: int) -> list[bytes]:
    """解码 Simple chunk（chunk_type='r'），返回记录列表。

    chunk data 布局：
        compression_type(1) + compressed_sizes_size(varint64)
        + compressed_sizes(该长度) + compressed_values(剩余全部)
    """
    if not body:
        raise RiegeliError("Simple chunk 数据为空")
    compression_type = body[0]
    sizes_size, offset = read_varint(body, 1)
    sizes_block = body[offset : offset + sizes_size]
    values_block = body[offset + sizes_size :]

    sizes_raw = _decompress(sizes_block, compression_type)
    values_raw = _decompress(values_block, compression_type)

    sizes: list[int] = []
    cursor = 0
    for _ in range(num_records):
        size, cursor = read_varint(sizes_raw, cursor)
        sizes.append(size)

    if sum(sizes) != len(values_raw):
        raise RiegeliError(
            f"记录长度之和 {sum(sizes)} 与数据区长度 {len(values_raw)} 不一致"
        )

    records: list[bytes] = []
    cursor = 0
    for size in sizes:
        records.append(values_raw[cursor : cursor + size])
        cursor += size
    return records


def describe(path: str | Path) -> dict:
    """扫描单个文件，返回 chunk 类型直方图与记录统计（用于数据体检）"""
    data = Path(path).read_bytes()
    hist: dict[str, int] = {}
    records = 0
    decoded = 0
    chunks = 0
    for header in iter_chunk_headers(data):
        hist[header.type_name] = hist.get(header.type_name, 0) + 1
        records += header.num_records
        decoded += header.decoded_data_size
        chunks += 1
    return {
        "path": str(path),
        "file_size": len(data),
        "signature_ok": data[:64] == FILE_SIGNATURE_64,
        "chunks": chunks,
        "chunk_types": hist,
        "records": records,
        "decoded_bytes": decoded,
    }


def iter_records(
    path: str | Path, *, skip_transposed: bool = True
) -> Iterator[bytes]:
    """按顺序产出文件中的全部记录（原始 proto 字节）。

    只解码 Simple chunk；其他 chunk 类型（签名/元数据/填充）num_records 为 0，
    本身就不携带记录。
    """
    data = Path(path).read_bytes()
    for header in iter_chunk_headers(data):
        if header.num_records == 0:
            continue
        if header.chunk_type == CHUNK_TRANSPOSED:
            if skip_transposed:
                continue
            raise RiegeliError("遇到 Transposed chunk，本读取器暂不支持")
        if header.chunk_type != CHUNK_SIMPLE:
            continue
        body = read_skipping_block_headers(data, header.data_pos, header.data_size)
        yield from decode_simple_chunk(body, header.num_records)
