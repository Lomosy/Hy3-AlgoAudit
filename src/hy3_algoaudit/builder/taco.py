"""TACO 数据集获取与标签关联。

TACO = Topics in Algorithmic COde generation（BAAI/TACO，Apache-2.0），
用于补充算法标签（tags / skill_types）与难度分层信息。

网络说明：本机 huggingface.co 不可达，因此统一走 hf-mirror.com 镜像。
镜像支持 HTTP Range（返回 206）但**不返回正文**，所以无法做懒惰列读取，
改为逐分片整体下载后只抽取需要的列，实测下载速率约 13 MB/s。

关联策略（关联率与匹配方式都会记录下来，便于后续分析）：
    1. contest_index  用 url 解析出 (平台, 比赛号, 题号)，与 CodeContests 的
                      cf_contest_id / cf_index 精确对齐（Codeforces 题适用）
    2. title          归一化标题模糊匹配（AtCoder/Aizu 等无题号的题适用）
    3. none           未命中，算法类别回落到题面关键词启发式
"""

from __future__ import annotations

import ast
import json
import re
import urllib.request
from pathlib import Path
from typing import Any, Iterable

TACO_API = "https://hf-mirror.com/api/datasets/{repo}/tree/main/{folder}"
TACO_RESOLVE = "https://hf-mirror.com/datasets/{repo}/resolve/main/{path}"

_NON_WORD = re.compile(r"[^a-z0-9]+")
_LEADING_PID = re.compile(r"^p\d+\s+")
_CF_URL = re.compile(r"codeforces\.com/(?:problemset/problem|contest)/(\d+)/(?:problem/)?([A-Za-z]\d?)")
_ATCODER_URL = re.compile(r"atcoder\.jp/contests/([^/]+)/tasks/([^/?#]+)")
_CODECHEF_URL = re.compile(r"codechef\.com/problems/([^/?#]+)")

# hf-mirror 会拦截 urllib 的默认 UA（返回 403），必须显式带上
_USER_AGENT = "Mozilla/5.0 (compatible; Hy3-AlgoAudit/0.1; +https://github.com/Lsyccnu/Hy3-AlgoAudit)"


def _open(url: str, timeout: int = 120):
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    return urllib.request.urlopen(request, timeout=timeout)


# ---------------------------------------------------------------- 下载
def list_shards(repo: str, mirror: str = "https://hf-mirror.com", folder: str = "ALL") -> list[dict]:
    """列出 TACO 仓库 ALL 配置下的全部分片（含大小）"""
    url = TACO_API.format(repo=repo, folder=folder)
    with _open(url, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return [item for item in payload if item["type"] == "file" and item["path"].endswith(".parquet")]


def download_shard(relative_path: str, dest: Path, repo: str, mirror: str = "https://hf-mirror.com") -> Path:
    """下载单个分片（流式写入，避免整体驻留内存）"""
    if dest.exists() and dest.stat().st_size > 0:
        print(f"    已存在，跳过：{dest.name}")
        return dest
    url = TACO_RESOLVE.format(repo=repo, path=relative_path)
    tmp = dest.with_suffix(dest.suffix + ".part")
    downloaded = 0
    with _open(url, timeout=120) as response, tmp.open("wb") as handle:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
            downloaded += len(chunk)
    tmp.replace(dest)
    print(f"    完成：{dest.name}  {downloaded / 1048576:.1f} MB")
    return dest


def download_taco(cfg: dict[str, Any]) -> list[Path]:
    """下载全部 TACO 分片到 data/taco/raw/"""
    taco_cfg = cfg["taco"]
    repo = taco_cfg["repo"]
    mirror = taco_cfg.get("mirror", "https://hf-mirror.com")
    raw_dir = Path(cfg["paths"]["taco_dir"]) / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    shards = list_shards(repo, mirror)
    total = sum(item.get("size", 0) for item in shards)
    print(f"  TACO {repo}: {len(shards)} 个分片，共 {total / 1073741824:.2f} GB")
    paths: list[Path] = []
    for item in shards:
        name = Path(item["path"]).name
        paths.append(download_shard(item["path"], raw_dir / name, repo, mirror))
    return paths


# ---------------------------------------------------------------- 索引
def extract_index(cfg: dict[str, Any], raw_paths: Iterable[Path]) -> Path:
    """把分片裁剪成只含所需列的紧凑索引 parquet"""
    import pyarrow as pa
    import pyarrow.parquet as pq

    columns = list(cfg["taco"]["columns"])
    if cfg["taco"].get("use_solutions"):
        columns = columns + ["solutions"]

    out_path = Path(cfg["paths"]["taco_dir"]) / "taco_index.parquet"
    writer: pq.ParquetWriter | None = None
    rows = 0
    try:
        for path in raw_paths:
            table = pq.ParquetFile(path).read(columns=columns)
            if writer is None:
                writer = pq.ParquetWriter(out_path, table.schema)
            writer.write_table(table)
            rows += table.num_rows
            print(f"    抽取 {path.name:<34} {table.num_rows:>6} 行  {len(columns)} 列")
            del table
    finally:
        if writer is not None:
            writer.close()
    print(f"  TACO 索引写入 {out_path}（{rows} 行）")
    return out_path


# ---------------------------------------------------------------- 关联
def normalize_title(text: str) -> str:
    """标题归一化：去题号前缀、去大小写、去标点、压缩空白"""
    cleaned = _LEADING_PID.sub("", (text or "").strip())
    return _NON_WORD.sub(" ", cleaned.lower()).strip()


def title_candidates(name: str) -> list[str]:
    """CodeContests 题名可能带比赛名前缀，这里给出若干候选标题"""
    cleaned = _LEADING_PID.sub("", (name or "").strip())
    candidates = [normalize_title(cleaned)]
    if " - " in cleaned:
        candidates.append(normalize_title(cleaned.rsplit(" - ", 1)[-1]))
    return [c for c in dict.fromkeys(candidates) if c]


def parse_url(url: str) -> str | None:
    """把 TACO 的 url 归一化成关联键"""
    if not url:
        return None
    text = url.strip()
    matched = _CF_URL.search(text)
    if matched:
        return f"codeforces:{matched.group(1)}:{matched.group(2).upper()}"
    matched = _ATCODER_URL.search(text)
    if matched:
        return f"atcoder:{matched.group(1)}:{matched.group(2)}"
    matched = _CODECHEF_URL.search(text)
    if matched:
        return f"codechef::{matched.group(1)}"
    return None


def cc_key(row: dict[str, Any]) -> str | None:
    """CodeContests 题目的关联键（目前只有 Codeforces 题能精确对齐）"""
    if row.get("source_name") == "CODEFORCES" and row.get("cf_contest_id"):
        index = (row.get("cf_index") or "").strip().upper()
        if index:
            return f"codeforces:{row['cf_contest_id']}:{index}"
    return None


def load_index(path: str | Path) -> tuple[dict[str, dict], dict[str, dict]]:
    """读取 TACO 索引，返回 (按键索引, 按标题索引)"""
    import pyarrow.parquet as pq

    table = pq.read_table(path)
    by_key: dict[str, dict] = {}
    by_title: dict[str, dict] = {}
    for row in table.to_pylist():
        key = parse_url(row.get("url") or "")
        record = {
            "taco_key": key,
            "name": row.get("name") or "",
            "source": row.get("source") or "",
            "difficulty": row.get("difficulty") or "",
            "tags": _split_tags(row.get("tags")),
            "skill_types": _split_tags(row.get("skill_types")),
            "time_limit": row.get("time_limit") or "",
            "memory_limit": row.get("memory_limit") or "",
            "url": row.get("url") or "",
        }
        if key and key not in by_key:
            by_key[key] = record
        title = normalize_title(record["name"])
        if title and title not in by_title:
            by_title[title] = record
    return by_key, by_title


def _split_tags(value: Any) -> list[str]:
    """TACO 的 tags / skill_types 列是「Python 列表的字符串表示」。

    实测样例是 `"['Combinatorics', 'Mathematics']"`：单引号包裹，不是合法 JSON，
    因此优先用 ast.literal_eval，失败再退回 json 与手工切分。
    """
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    if text in ("[]", "null", "None", ""):
        return []
    try:
        parsed = ast.literal_eval(text)
        if isinstance(parsed, (list, tuple)):
            return [str(v).strip() for v in parsed if str(v).strip()]
    except (ValueError, SyntaxError):
        pass
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [str(v).strip() for v in parsed if str(v).strip()]
    except (json.JSONDecodeError, TypeError):
        pass
    cleaned = text.strip("[]")
    return [part.strip().strip("'\"") for part in cleaned.split(",") if part.strip().strip("'\"")]


def match(row: dict[str, Any], by_key: dict[str, dict], by_title: dict[str, dict]) -> tuple[dict | None, str]:
    """给一道 CodeContests 题目找 TACO 记录，返回 (记录, 匹配方式)"""
    key = cc_key(row)
    if key and key in by_key:
        return by_key[key], "contest_index"
    for candidate in title_candidates(row.get("name", "")):
        if candidate in by_title:
            return by_title[candidate], "title"
    return None, "none"


def enrich(rows: list[dict[str, Any]], index_path: str | Path) -> dict[str, int]:
    """就地给题目池补充 TACO 字段，返回匹配方式统计"""
    by_key, by_title = load_index(index_path)
    stats = {"contest_index": 0, "title": 0, "none": 0}
    for row in rows:
        record, how = match(row, by_key, by_title)
        stats[how] += 1
        row["taco_match"] = how
        if record is None:
            row["taco_tags"] = []
            row["taco_skill_types"] = []
            row["taco_difficulty"] = None
            row["taco_url"] = None
            continue
        row["taco_tags"] = record["tags"]
        row["taco_skill_types"] = record["skill_types"]
        row["taco_difficulty"] = record["difficulty"] or None
        row["taco_url"] = record["url"]
    return stats
