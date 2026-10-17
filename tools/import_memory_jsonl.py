#!/usr/bin/env python3
"""把 memory.jsonl 导入为 memory.db，并构建 FTS 索引。

独立脚本：只依赖标准库 + jieba，不 import MakeCode 的 utils 包。
可拷到任意目录运行。

用法：
  python import_memory_jsonl.py
  python import_memory_jsonl.py --workdir /path/to/project
  python import_memory_jsonl.py --jsonl PATH --db PATH

目标 memory.db 会被清空后重建。deleted 记录只进 memories、不进 FTS；
active 记录按与运行时相同的 jieba 分词写入 memories_fts。
导入前请退出正在使用该 db 的 MakeCode 进程。
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
from pathlib import Path

import jieba

jieba.setLogLevel(logging.ERROR)

_RECORD_FIELDS = (
    "id",
    "created_at",
    "updated_at",
    "category",
    "insight",
    "evidence",
    "reuse_condition",
    "status",
)
_FTS_FIELDS = ("insight", "reuse_condition")
_CJK_LOW, _CJK_HIGH = "\u4e00", "\u9fff"
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memories (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    category TEXT NOT NULL,
    insight TEXT NOT NULL,
    evidence TEXT NOT NULL,
    reuse_condition TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    insight, reuse_condition,
    tokenize='unicode61'
);
"""


def contains_cjk(text: str) -> bool:
    return any(_CJK_LOW <= ch <= _CJK_HIGH for ch in text)


def merge_single_char_tokens(raw_tokens: list[str]) -> list[str]:
    result = []
    count = len(raw_tokens)
    for index, token in enumerate(raw_tokens):
        is_cjk_single_char = len(token) == 1 and contains_cjk(token)
        if not is_cjk_single_char:
            result.append(token)
            continue
        if result:
            result.append(result[-1] + token)
        if index + 1 < count:
            result.append(token + raw_tokens[index + 1])
    return result


def tokenize_for_index(text: str) -> list[str]:
    tokens = []
    for fragment in re.split(r"(\s+)", text or ""):
        if not fragment.strip():
            continue
        if contains_cjk(fragment):
            raw_tokens = [t for t in jieba.lcut_for_search(fragment) if any(ch.isalnum() for ch in t)]
            tokens.extend(merge_single_char_tokens(raw_tokens))
        else:
            tokens.append(fragment)
    return [token for token in tokens if any(ch.isalnum() for ch in token) and len(token) >= 2]


def segment_text_for_index(text: str) -> str:
    return " ".join(tokenize_for_index(text))


def memory_fts_text(record: dict) -> dict[str, str]:
    return {
        "insight": segment_text_for_index(str(record.get("insight", "") or "")),
        "reuse_condition": segment_text_for_index(str(record.get("reuse_condition", "") or "")),
    }


def sidecar_paths(db_path: Path) -> list[Path]:
    return [
        db_path.with_name(db_path.name + "-wal"),
        db_path.with_name(db_path.name + "-shm"),
    ]


def read_jsonl_records(jsonl_path: Path) -> list[dict]:
    if not jsonl_path.is_file():
        raise FileNotFoundError(f"jsonl 不存在: {jsonl_path}")

    records = []
    seen_ids = set()
    with jsonl_path.open(encoding="utf-8") as handle:
        for line_no, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise ValueError(f"{jsonl_path}:{line_no} 不是 JSON object")

            record = {}
            for field in _RECORD_FIELDS:
                value = payload.get(field, "")
                if field == "id":
                    if not isinstance(value, str) or not value.strip():
                        raise ValueError(f"{jsonl_path}:{line_no} 缺少有效 id")
                    value = value.strip()
                    if value in seen_ids:
                        raise ValueError(f"{jsonl_path}:{line_no} 重复 id: {value}")
                    seen_ids.add(value)
                elif field == "status":
                    value = value.strip() if isinstance(value, str) and value.strip() else "active"
                else:
                    value = value.strip() if isinstance(value, str) else ""
                record[field] = value
            if not record["created_at"]:
                record["created_at"] = record["updated_at"]
            records.append(record)
    return records


def clear_db_files(db_path: Path) -> None:
    for path in [db_path, *sidecar_paths(db_path)]:
        path.unlink(missing_ok=True)


def import_records(records: list[dict], db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    clear_db_files(db_path)

    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_SCHEMA_SQL)
        for record in records:
            cursor = conn.execute(
                "INSERT INTO memories (id, created_at, updated_at, category, insight, evidence, reuse_condition, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record["id"],
                    record["created_at"],
                    record["updated_at"],
                    record["category"],
                    record["insight"],
                    record["evidence"],
                    record["reuse_condition"],
                    record["status"],
                ),
            )
            if record["status"] != "deleted":
                fts_text = memory_fts_text(record)
                conn.execute(
                    "INSERT INTO memories_fts (rowid, insight, reuse_condition) VALUES (?, ?, ?)",
                    (cursor.lastrowid, fts_text["insight"], fts_text["reuse_condition"]),
                )
            conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        clear_db_files(db_path)
        raise
    else:
        conn.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="将 memory.jsonl 导入为 memory.db")
    parser.add_argument("--workdir", type=Path, help="项目根目录，默认当前工作目录")
    parser.add_argument("--jsonl", type=Path, help="memory.jsonl 路径")
    parser.add_argument("--db", type=Path, help="memory.db 路径")
    return parser.parse_args()


def resolve_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    workdir = (args.workdir or Path.cwd()).expanduser().resolve()
    jsonl_path = args.jsonl or (workdir / ".makecode" / "memory" / "memory.jsonl")
    db_path = args.db or (workdir / ".makecode" / "memory" / "memory.db")
    return jsonl_path.expanduser().resolve(), db_path.expanduser().resolve()


def main() -> int:
    args = parse_args()
    jsonl_path, db_path = resolve_paths(args)
    records = read_jsonl_records(jsonl_path)
    active_count = sum(1 for record in records if record["status"] != "deleted")
    deleted_count = len(records) - active_count
    print(f"读取 {jsonl_path}: total={len(records)} active={active_count} deleted={deleted_count}")
    print(f"写入 {db_path}")
    import_records(records, db_path)
    print(f"完成: memories={len(records)} memories_fts={active_count}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"导入失败: {exc}", file=sys.stderr)
        raise SystemExit(1)
