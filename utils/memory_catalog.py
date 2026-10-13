"""长期记忆的 SQLite 磁盘存储层。

记录表 `memories` 与 FTS5 索引表 `memories_fts` 共存于同一个磁盘库文件
（`.makecode/memory/memory.db`），不再有 memory.jsonl，也不在内存里
`sqlite3.connect(':memory:')` 临时建表再查询——FTS5 虚表本身就是磁盘库里的
常驻表，直接对它 MATCH。

本模块故意不 import jieba：分词只在 utils/memory.py 里发生，写入前把分词
结果（空格分隔的 token 串）传进来即可。这样 `--memory-list` 等 CLI 轻量读
路径只需要这个模块，不会被拖着加载 jieba / TUI / 模型客户端。

FTS5 不走 `content=` 外挂模式：外挂模式默认镜像记录表原文，而我们需要存的
是分词后的 token 串而不是原文，所以用独立虚表、以 `memories.rowid` 对齐。
"""

import sqlite3
import threading
from pathlib import Path

_FTS_FIELDS = ("insight", "reuse_condition")

_SCHEMA_SQL = f"""
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
    {", ".join(_FTS_FIELDS)},
    tokenize='unicode61'
);
"""


class MemoryStore:
    """单个 workdir 对应一个 MemoryStore 实例，内部持有一条 sqlite3 连接。

    所有公开方法都用同一把 `threading.RLock` 串行化：sqlite3 连接默认不允许
    跨线程直接共享游标操作，TUI、召回、主循环都可能并发调用，必须显式加锁，
    而不是用 `check_same_thread=False` 不加保护地共享连接。
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._open()

    def _open(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA_SQL)
        conn.commit()
        self._conn = conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def reopen(self, db_path: Path | None = None) -> None:
        """切换工作区时调用：关闭旧连接，按新路径（或原路径）重新打开。"""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            if db_path is not None:
                self.db_path = Path(db_path)
            self._open()

    # ---- 行 <-> dict 映射 ----

    @staticmethod
    def _row_to_record(row: tuple) -> dict:
        return {
            "id": row[0],
            "created_at": row[1],
            "updated_at": row[2],
            "category": row[3],
            "insight": row[4],
            "evidence": row[5],
            "reuse_condition": row[6],
            "status": row[7],
        }

    _RECORD_COLUMNS = "id, created_at, updated_at, category, insight, evidence, reuse_condition, status"

    # ---- CRUD 原语 ----

    def insert_memory(self, record: dict, *, fts_text: dict[str, str]) -> None:
        """插入一条 memories 行，并写入对应的 FTS 行。

        `fts_text` 必须提供 `insight`/`reuse_condition` 两个字段对应的已分词
        （空格分隔 token）文本，由调用方（utils/memory.py）负责分词。
        """
        with self._lock:
            cursor = self._conn.execute(
                f"INSERT INTO memories ({self._RECORD_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
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
            fts_rowid = cursor.lastrowid
            self._conn.execute(
                f"INSERT INTO memories_fts (rowid, {', '.join(_FTS_FIELDS)}) VALUES (?, ?, ?)",
                (fts_rowid, fts_text.get("insight", ""), fts_text.get("reuse_condition", "")),
            )
            self._conn.commit()

    def update_memory_fields(
            self,
            memory_id: str,
            fields: dict,
            *,
            fts_text: dict[str, str] | None = None,
    ) -> bool:
        """更新 memories 行的指定字段。

        若 `fts_text` 非空（insight/reuse_condition 发生变化时才需要传），
        同步重建该行的 FTS 内容；只改 updated_at 等不影响分词字段的场景应
        使用 `touch_memory`，不要经过这里触发不必要的索引重写。
        """
        if not fields:
            return False
        with self._lock:
            set_clause = ", ".join(f"{key} = ?" for key in fields)
            cursor = self._conn.execute(
                f"UPDATE memories SET {set_clause} WHERE id = ? AND status = 'active'",
                (*fields.values(), memory_id),
            )
            if cursor.rowcount == 0:
                self._conn.rollback()
                return False
            if fts_text is not None:
                row = self._conn.execute(
                    "SELECT rowid FROM memories WHERE id = ?", (memory_id,)
                ).fetchone()
                if row is not None:
                    self._conn.execute(
                        "DELETE FROM memories_fts WHERE rowid = ?", (row[0],)
                    )
                    self._conn.execute(
                        f"INSERT INTO memories_fts (rowid, {', '.join(_FTS_FIELDS)}) VALUES (?, ?, ?)",
                        (row[0], fts_text.get("insight", ""), fts_text.get("reuse_condition", "")),
                    )
            self._conn.commit()
            return True

    def touch_memory(self, memory_id: str, updated_at: str) -> bool:
        """只更新 updated_at，不触碰 FTS 行——分词内容没变，没必要重建索引。"""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE memories SET updated_at = ? WHERE id = ? AND status = 'active'",
                (updated_at, memory_id),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def soft_delete_memories(self, memory_ids: list[str], *, updated_at: str) -> list[str]:
        """把指定 active 记忆标记为 deleted，并从 FTS 表移除对应行。

        已删除记忆不应再参与召回匹配，所以必须同步从 FTS 里删，不能只改状态位。
        返回实际被删除的 id 列表（忽略不存在或已非 active 的 id）。
        """
        if not memory_ids:
            return []
        with self._lock:
            deleted_ids = []
            for memory_id in memory_ids:
                row = self._conn.execute(
                    "SELECT rowid FROM memories WHERE id = ? AND status = 'active'",
                    (memory_id,),
                ).fetchone()
                if row is None:
                    continue
                self._conn.execute(
                    "UPDATE memories SET status = 'deleted', updated_at = ? WHERE id = ?",
                    (updated_at, memory_id),
                )
                self._conn.execute(
                    "DELETE FROM memories_fts WHERE rowid = ?", (row[0],)
                )
                deleted_ids.append(memory_id)
            if deleted_ids:
                self._conn.commit()
            else:
                self._conn.rollback()
            return deleted_ids

    def list_active_memories(self) -> list[dict]:
        return self._list_memories(include_deleted=False)

    def list_all_memories(self, *, include_deleted: bool = True) -> list[dict]:
        return self._list_memories(include_deleted=include_deleted)

    def _list_memories(self, *, include_deleted: bool) -> list[dict]:
        with self._lock:
            if include_deleted:
                rows = self._conn.execute(
                    f"SELECT {self._RECORD_COLUMNS} FROM memories "
                    "ORDER BY (updated_at IS NULL), updated_at, created_at"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    f"SELECT {self._RECORD_COLUMNS} FROM memories WHERE status = 'active' "
                    "ORDER BY (updated_at IS NULL), updated_at, created_at"
                ).fetchall()
            return [self._row_to_record(row) for row in rows]

    def get_document_frequency(self, tokenize_fn) -> dict:
        """扫 active 行，统计每个 token 出现在多少条记录里。

        45 条规模的活跃记忆下，启动即扫全表足够便宜，不需要额外的增量
        token_df 表。`tokenize_fn(text) -> list[str]` 由调用方传入，这里
        不 import jieba。
        """
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {', '.join(_FTS_FIELDS)} FROM memories WHERE status = 'active'"
            ).fetchall()
        df: dict[str, int] = {}
        for row in rows:
            text = " ".join(str(value or "") for value in row)
            for token in set(tokenize_fn(text)):
                df[token] = df.get(token, 0) + 1
        return df

    def search_fts(self, match_query: str) -> list[tuple[str, float]]:
        """对 FTS 表执行 MATCH + bm25 排序，返回 (memory_id, score) 列表（降序）。

        空的 match_query 直接返回空列表。已删除记忆的 FTS 行在删除时已被
        移除，这里天然不会命中它们。
        """
        if not match_query:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT m.id, -bm25(memories_fts) AS score "
                "FROM memories_fts JOIN memories AS m ON m.rowid = memories_fts.rowid "
                "WHERE memories_fts MATCH ? ORDER BY score DESC",
                (match_query,),
            ).fetchall()
        return [(row[0], row[1]) for row in rows]


def read_memory_records(db_path: Path, *, include_deleted: bool = False) -> list[dict]:
    """CLI 等轻量只读场景使用：独立打开一次性连接查询，不依赖全局 MemoryStore。

    不存在的 db 文件视为没有记忆，返回空列表（新工作区尚未写入任何记忆时）。
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    try:
        columns = "id, created_at, updated_at, category, insight, evidence, reuse_condition, status"
        if include_deleted:
            rows = conn.execute(f"SELECT {columns} FROM memories").fetchall()
        else:
            rows = conn.execute(
                f"SELECT {columns} FROM memories WHERE status = 'active'"
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    return [MemoryStore._row_to_record(row) for row in rows]


def sort_memory_records(records: list[dict]) -> list[dict]:
    return sorted(
        records,
        key=lambda record: record.get("updated_at") or record.get("created_at") or "",
    )
