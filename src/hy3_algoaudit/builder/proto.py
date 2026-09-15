"""ContestProblem proto 解码器（手写 protobuf wire 解析）

为什么手写：CodeContests 每条记录是一个 `ContestProblem` 消息（proto2），
结构固定且字段很少。手写解析可以避免引入 protobuf 运行时 + 代码生成，
也便于在字段号存疑时直接输出「字段直方图」供人工核对。

proto 定义出处（Apache-2.0）：
    https://github.com/google-deepmind/code_contests/blob/main/contest_problem.proto

字段号确认状态：
    全部字段号已由 `scripts/inspect_fields.py` 扫描 0 号分片 105 条真实记录
    （CodeChef 6 / Codeforces 62 / HackerEarth 9 / AtCoder 11 / Aizu 17）
    逐项核对确认。核对中发现并修正了三处与官方 proto 文本推断不符的字段：

        字段号 10 = cf_contest_id   （varint，样例 1000 / 1025，恰好只出现在 62 道 CF 题上）
        字段号 12 = cf_index        （string，样例 'A' / 'B'）
        字段号 13 = cf_points       （fixed32/float，样例 1000.0 / 500.0）
        字段号 19 = incorrect_solutions（length-delimited，内容形如 Solution{language=2, solution=...}）

    另有两点实测结论：
        - 全部 105 条记录均未出现 input_file / output_file 字段 → 数据集题目一律使用 stdin/stdout
        - untranslated_description 存在日文题面 → 部分 AtCoder/Aizu 题目为英译题，
          抽取时统一使用英文 description
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any, Iterator

WIRE_VARINT = 0
WIRE_FIXED64 = 1
WIRE_LENGTH = 2
WIRE_FIXED32 = 5

# ---------------------------------------------------------------- 字段号表
# 保留号（官方 proto）：3, 9, 11, 16, 17, 22, 23, 24, 25, 26, 27, 28
# 全部字段号已用真实数据核对（见模块 docstring）
FIELD_NUMBER: dict[str, int] = {
    # --- 基础信息 ---
    "name": 1,
    "description": 2,
    # --- 测试用例 ---
    "public_tests": 4,
    "private_tests": 5,
    "generated_tests": 18,  # 实测每题固定 100 条
    # --- 来源与难度 ---
    "source": 6,
    "difficulty": 7,
    # --- 解答 ---
    "solutions": 8,  # 正确解（含 PYTHON=1 Python2 / PYTHON3=3）
    "incorrect_solutions": 19,  # 错误解，数量极大（CF 题平均上千条）
    # --- Codeforces 专属元数据 ---
    "cf_contest_id": 10,
    "cf_index": 12,
    "cf_points": 13,  # float（wire type 5）
    "cf_rating": 14,
    "cf_tags": 15,
    # --- 翻译信息 ---
    "is_description_translated": 20,
    "untranslated_description": 21,
    # --- 资源限制与 IO ---
    "time_limit": 29,  # 嵌套 google.protobuf.Duration
    "memory_limit_bytes": 30,
    "input_file": 31,  # 实测该数据集中从未出现
    "output_file": 32,  # 实测该数据集中从未出现
}

# 已由真实数据实测确认的字段（目前为全部字段）
EMPIRICAL_FIELDS = frozenset(FIELD_NUMBER) - {"input_file", "output_file"}

# 枚举值（官方 contest_problem.proto）
SOURCE_NAMES = {
    0: "UNKNOWN_SOURCE",
    1: "CODECHEF",
    2: "CODEFORCES",
    3: "HACKEREARTH",
    4: "CODEJAM",
    6: "ATCODER",
    7: "AIZU",
}

DIFFICULTY_NAMES = {
    0: "UNKNOWN_DIFFICULTY",
    1: "EASY",
    2: "MEDIUM",
    3: "HARD",
    4: "HARDER",
    5: "HARDEST",
    6: "EXTERNAL",
}
# 7..28 为 A..V（跳过 18）
for _code in range(7, 29):
    DIFFICULTY_NAMES[_code] = chr(ord("A") + (_code - 7))

LANGUAGE_NAMES = {
    0: "UNKNOWN_LANGUAGE",
    1: "PYTHON",  # Python 2，无法在 Python 3 下直接运行
    2: "CPP",
    3: "PYTHON3",
    4: "JAVA",
}
LANGUAGE_PYTHON3 = 3


class ProtoError(RuntimeError):
    """protobuf 数据异常"""


# ---------------------------------------------------------------- 通用解析
def read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ProtoError("varint 越界")
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
        if shift > 63:
            raise ProtoError("varint 超过 64 位")


@dataclass(frozen=True)
class RawField:
    number: int
    wire_type: int
    value: Any  # varint->int, length->bytes, fixed32->float, fixed64->float
    raw: bytes  # 该字段的完整原始字节（含 tag）


def iter_fields(buf: bytes) -> Iterator[RawField]:
    """按顺序遍历一条消息的所有字段（未知字段也能读出来）"""
    pos = 0
    total = len(buf)
    while pos < total:
        start = pos
        tag, pos = read_varint(buf, pos)
        number = tag >> 3
        wire_type = tag & 0x07
        if number == 0:
            raise ProtoError(f"非法字段号 0（offset={start}）")

        if wire_type == WIRE_VARINT:
            value, pos = read_varint(buf, pos)
        elif wire_type == WIRE_LENGTH:
            length, pos = read_varint(buf, pos)
            if pos + length > total:
                raise ProtoError(f"长度越界（字段 {number}，offset={start}）")
            value = buf[pos : pos + length]
            pos += length
        elif wire_type == WIRE_FIXED32:
            if pos + 4 > total:
                raise ProtoError("fixed32 越界")
            value = struct.unpack_from("<f", buf, pos)[0]
            pos += 4
        elif wire_type == WIRE_FIXED64:
            if pos + 8 > total:
                raise ProtoError("fixed64 越界")
            value = struct.unpack_from("<d", buf, pos)[0]
            pos += 8
        else:
            raise ProtoError(f"不支持的 wire type {wire_type}（字段 {number}，offset={start}）")

        yield RawField(number, wire_type, value, buf[start:pos])


def _decode_text(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- 字段诊断
def field_report(records: list[bytes], *, max_samples: int = 4) -> dict[int, dict]:
    """统计一批记录里各字段号的出现情况，用于与官方 proto 对照。

    输出每个字段号：出现次数、wire type、以及若干样例值（字符串截断）。
    这是「字段号核对」闸门的数据来源：任何推断字段号在核对前都不得采信。
    """
    report: dict[int, dict] = {}
    for raw in records:
        for field in iter_fields(raw):
            entry = report.setdefault(
                field.number, {"count": 0, "wire_types": set(), "samples": []}
            )
            entry["count"] += 1
            entry["wire_types"].add(field.wire_type)
            if len(entry["samples"]) < max_samples:
                if field.wire_type == WIRE_LENGTH:
                    sample: Any = _decode_text(field.value[:120])
                else:
                    sample = field.value
                entry["samples"].append((field.wire_type, sample))

    for entry in report.values():
        entry["wire_types"] = sorted(entry["wire_types"])
    return report


# ---------------------------------------------------------------- 业务解析
def parse_test(raw: bytes) -> dict[str, str]:
    """Test { input = 1; output = 2; }"""
    out = {"input": "", "output": ""}
    for field in iter_fields(raw):
        if field.number == 1 and field.wire_type == WIRE_LENGTH:
            out["input"] = _decode_text(field.value)
        elif field.number == 2 and field.wire_type == WIRE_LENGTH:
            out["output"] = _decode_text(field.value)
    return out


def parse_solution(raw: bytes) -> dict[str, Any]:
    """Solution { language = 1 (enum); solution = 2 (string); }"""
    out: dict[str, Any] = {"language": 0, "language_name": "", "solution": ""}
    for field in iter_fields(raw):
        if field.number == 1 and field.wire_type == WIRE_VARINT:
            out["language"] = field.value
        elif field.number == 2 and field.wire_type == WIRE_LENGTH:
            out["solution"] = _decode_text(field.value)
    out["language_name"] = LANGUAGE_NAMES.get(out["language"], f"UNKNOWN_{out['language']}")
    return out


def parse_duration(raw: bytes) -> float:
    """google.protobuf.Duration { seconds = 1; nanos = 2; }"""
    seconds = 0
    nanos = 0
    for field in iter_fields(raw):
        if field.number == 1 and field.wire_type == WIRE_VARINT:
            seconds = field.value
        elif field.number == 2 and field.wire_type == WIRE_VARINT:
            nanos = field.value
    return seconds + nanos / 1e9


def parse_contest_problem(
    raw: bytes, *, keep_incorrect_solutions: bool = False
) -> dict[str, Any]:
    """把一条 ContestProblem 记录的字节解析为 dict。

    未在 FIELD_NUMBER 中声明的字段号会被忽略（通过 field_report 核对后再补）。

    keep_incorrect_solutions=False 时只统计错误解数量、不保留其代码。
    Codeforces 题目的 incorrect_solutions 动辄上千条，全量保留会让扫描阶段
    的内存与解析开销明显上升，而构建评测集并不需要这些代码。
    """
    n = FIELD_NUMBER
    result: dict[str, Any] = {
        "name": "",
        "description": "",
        "public_tests": [],
        "private_tests": [],
        "generated_tests": [],
        "source": 0,
        "difficulty": 0,
        "solutions": [],
        "incorrect_solutions": [],
        "n_incorrect_solutions": 0,
        "cf_contest_id": None,
        "cf_index": None,
        "cf_points": None,
        "cf_rating": None,
        "cf_tags": [],
        "is_description_translated": False,
        "untranslated_description": "",
        "time_limit_seconds": None,
        "memory_limit_bytes": None,
        "input_file": "",
        "output_file": "",
    }

    for field in iter_fields(raw):
        number, wire_type, value = field.number, field.wire_type, field.value

        if wire_type == WIRE_LENGTH and number in (
            n["public_tests"],
            n["private_tests"],
            n["generated_tests"],
        ):
            key = {
                n["public_tests"]: "public_tests",
                n["private_tests"]: "private_tests",
                n["generated_tests"]: "generated_tests",
            }[number]
            result[key].append(parse_test(value))

        elif wire_type == WIRE_LENGTH and number in (n["solutions"], n["incorrect_solutions"]):
            if number == n["solutions"]:
                result["solutions"].append(parse_solution(value))
            elif keep_incorrect_solutions:
                result["incorrect_solutions"].append(parse_solution(value))
            else:
                result["n_incorrect_solutions"] += 1

        elif wire_type == WIRE_VARINT and number == n["source"]:
            result["source"] = value
        elif wire_type == WIRE_VARINT and number == n["difficulty"]:
            result["difficulty"] = value
        elif wire_type == WIRE_VARINT and number == n["cf_contest_id"]:
            result["cf_contest_id"] = value
        elif wire_type == WIRE_VARINT and number == n["cf_rating"]:
            result["cf_rating"] = value
        elif wire_type == WIRE_VARINT and number == n["memory_limit_bytes"]:
            result["memory_limit_bytes"] = value
        elif wire_type == WIRE_VARINT and number == n["is_description_translated"]:
            result["is_description_translated"] = bool(value)
        elif wire_type == WIRE_FIXED32 and number == n["cf_points"]:
            result["cf_points"] = value

        elif wire_type == WIRE_LENGTH and number == n["name"]:
            result["name"] = _decode_text(value)
        elif wire_type == WIRE_LENGTH and number == n["description"]:
            result["description"] = _decode_text(value)
        elif wire_type == WIRE_LENGTH and number == n["untranslated_description"]:
            result["untranslated_description"] = _decode_text(value)
        elif wire_type == WIRE_LENGTH and number == n["cf_index"]:
            result["cf_index"] = _decode_text(value)
        elif wire_type == WIRE_LENGTH and number == n["cf_tags"]:
            result["cf_tags"].append(_decode_text(value))
        elif wire_type == WIRE_LENGTH and number == n["input_file"]:
            result["input_file"] = _decode_text(value)
        elif wire_type == WIRE_LENGTH and number == n["output_file"]:
            result["output_file"] = _decode_text(value)
        elif wire_type == WIRE_LENGTH and number == n["time_limit"]:
            result["time_limit_seconds"] = parse_duration(value)

    result["source_name"] = SOURCE_NAMES.get(result["source"], f"UNKNOWN_{result['source']}")
    result["difficulty_name"] = DIFFICULTY_NAMES.get(
        result["difficulty"], f"UNKNOWN_{result['difficulty']}"
    )
    return result


def count_tests(problem: dict[str, Any], origin: str) -> int:
    return len(problem[f"{origin}_tests"])
