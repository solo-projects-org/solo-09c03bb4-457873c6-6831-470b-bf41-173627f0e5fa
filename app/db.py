"""SQLite 连接与建表。

表结构：
- tests              试验元信息
- samples            原始毫秒采样（rowid 即严格递增的采样序号）；
                     归档后对应试验的采样行迁移为 compressed_blocks
- blocks             多级摘要块，level 1 为最细索引块，level 越大粒度越粗；
                     归档前后内容保持不变
- archives           按试验编号的归档元信息（一行即一个已归档试验）
- compressed_blocks  以 level 1 块为边界的无损压缩采样块，与 blocks 中
                     level 1 块一一对应，存同一个 SQLite 库
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from . import config

_SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS tests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE,
    source_filename TEXT    NOT NULL,
    sample_count    INTEGER NOT NULL,
    start_ts        INTEGER NOT NULL,
    end_ts          INTEGER NOT NULL,
    max_level       INTEGER NOT NULL,
    imported_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS samples (
    test_id    INTEGER NOT NULL,
    ts         INTEGER NOT NULL,
    pressure   REAL    NOT NULL,
    FOREIGN KEY (test_id) REFERENCES tests(id)
);

CREATE INDEX IF NOT EXISTS idx_samples_test_ts
    ON samples (test_id, ts);

CREATE TABLE IF NOT EXISTS blocks (
    test_id          INTEGER NOT NULL,
    level            INTEGER NOT NULL,
    block_index      INTEGER NOT NULL,
    start_ts         INTEGER NOT NULL,
    end_ts           INTEGER NOT NULL,
    sample_count     INTEGER NOT NULL,
    first_ts         INTEGER NOT NULL,
    last_ts          INTEGER NOT NULL,
    first_pressure   REAL    NOT NULL,
    last_pressure    REAL    NOT NULL,
    max_pressure     REAL    NOT NULL,
    max_pressure_ts  INTEGER NOT NULL,
    min_pressure     REAL    NOT NULL,
    min_pressure_ts  INTEGER NOT NULL,
    first_rowid      INTEGER NOT NULL,
    last_rowid       INTEGER NOT NULL,
    PRIMARY KEY (test_id, level, block_index)
);

CREATE INDEX IF NOT EXISTS idx_blocks_test_level_ts
    ON blocks (test_id, level, start_ts, end_ts);

-- 归档元信息：每个试验至多一行，重复归档请求据此识别为“已归档”
CREATE TABLE IF NOT EXISTS archives (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    test_id            INTEGER NOT NULL UNIQUE,
    block_count        INTEGER NOT NULL,
    sample_count       INTEGER NOT NULL,
    raw_bytes          INTEGER NOT NULL,
    compressed_bytes   INTEGER NOT NULL,
    compression_ratio  REAL    NOT NULL,
    archived_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (test_id) REFERENCES tests(id)
);

-- 无损压缩块：与 blocks 中 level 1 块按 (test_id, block_index) 一一对应，
-- first_rowid/last_rowid 沿用原采样 rowid 区间，便于摘要查询直接定位解压
CREATE TABLE IF NOT EXISTS compressed_blocks (
    test_id       INTEGER NOT NULL,
    block_index   INTEGER NOT NULL,
    first_rowid   INTEGER NOT NULL,
    last_rowid    INTEGER NOT NULL,
    sample_count  INTEGER NOT NULL,
    start_ts      INTEGER NOT NULL,
    end_ts        INTEGER NOT NULL,
    codec         INTEGER NOT NULL,
    payload       BLOB    NOT NULL,
    PRIMARY KEY (test_id, block_index),
    FOREIGN KEY (test_id) REFERENCES tests(id)
);

CREATE INDEX IF NOT EXISTS idx_compressed_test_rowid
    ON compressed_blocks (test_id, first_rowid, last_rowid);
"""


def connect(db_path=None) -> sqlite3.Connection:
    """打开一个带常用 PRAGMA 的连接。"""
    path = str(db_path or config.DB_PATH)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn


@contextmanager
def session() -> Iterator[sqlite3.Connection]:
    """请求级连接上下文。

    写操作必须在返回响应前自行 ``conn.commit()``：FastAPI/Starlette 的
    yield 依赖清理发生在响应发出之后，若把提交留到这里，紧随其后的请求
    可能在另一线程先打开连接而读不到未提交写入（read-your-writes 竞态）。
    这里仅负责：正常结束时兜底提交（只读事务为 no-op），异常时回滚。
    """
    conn = connect()
    try:
        yield conn
        if conn.in_transaction:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """进程启动时调用，幂等建库建表。"""
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = connect()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()
