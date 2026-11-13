"""按试验编号的长期归档：以最细摘要块（level 1）为边界无损压缩采样。

契约：
- 压缩块与原采样同在一个 SQLite 库（compressed_blocks 表），
  每个 level 1 摘要块对应一个压缩块，块边界完全一致；
- 原有多级摘要（blocks 表）保持不变，查询接口、分桶语义不变；
- 压缩块写入与原采样删除在同一事务内提交，要么整体生效要么整体回滚，
  归档失败时原采样仍可正常查询；
- 已归档的试验重复请求直接返回既有归档，不再写任何副本；
- 提交前对每个块做压缩 -> 解压 round-trip 比对，任何不一致都回滚。
"""

from __future__ import annotations

import sqlite3

from .codec import ArchiveCodecError, decode_samples, encode_samples


class ArchiveError(ValueError):
    """归档无法完成（含无损校验失败）。"""


class AlreadyArchived(Exception):
    """试验已归档：路由层据此返回既有归档（already_archived=true），不重复写入。"""

    def __init__(self, info: dict):
        super().__init__(f"试验 {info['test_id']} 已归档")
        self.info = info


def get_archive(conn: sqlite3.Connection, test_id: int) -> dict | None:
    """读取归档元信息；未归档返回 None。"""
    row = conn.execute(
        "SELECT * FROM archives WHERE test_id = ?", (test_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def is_archived(conn: sqlite3.Connection, test_id: int) -> bool:
    return get_archive(conn, test_id) is not None


def read_compressed_samples(
    conn: sqlite3.Connection, test_id: int, first_rowid: int, last_rowid: int
) -> list[tuple[int, float]]:
    """按 level 1 块的 rowid 区间取回并解压该块的原始采样。"""
    row = conn.execute(
        """SELECT payload FROM compressed_blocks
           WHERE test_id = ? AND first_rowid = ? AND last_rowid = ?""",
        (test_id, first_rowid, last_rowid),
    ).fetchone()
    if row is None:  # pragma: no cover - 由调用路径保证块已归档
        raise ArchiveError(
            f"level 1 块 [{first_rowid}, {last_rowid}] 缺少对应压缩块"
        )
    return decode_samples(row["payload"])


def archive_test(conn: sqlite3.Connection, test_id: int) -> dict:
    """把试验的全部采样按 level 1 块边界压缩归档。

    调用方的连接会被立即提升为写事务（BEGIN IMMEDIATE），保证同一试验
    的并发归档请求在写锁上串行化：先到者完成归档，后到者走 AlreadyArchived。
    任何校验/无损自检失败都显式回滚；成功则在返回前提交，保证紧随其后的
    查询立刻能读到压缩块（不依赖请求结束后的依赖清理）。
    """
    # 抢先进入写事务：失败会抛 sqlite3.OperationalError(database is locked)
    conn.execute("BEGIN IMMEDIATE")

    test = conn.execute(
        "SELECT * FROM tests WHERE id = ?", (test_id,)
    ).fetchone()
    if test is None:
        conn.rollback()
        raise KeyError(f"试验 {test_id} 不存在")

    existing = get_archive(conn, test_id)
    if existing is not None:
        conn.rollback()
        raise AlreadyArchived(existing)

    level1 = conn.execute(
        """SELECT block_index, first_rowid, last_rowid, sample_count
           FROM blocks
           WHERE test_id = ? AND level = 1
           ORDER BY block_index""",
        (test_id,),
    ).fetchall()
    if not level1:
        conn.rollback()
        raise ArchiveError(f"试验 {test_id} 没有 level 1 摘要块，无法归档")

    total_samples = sum(b["sample_count"] for b in level1)
    if total_samples != test["sample_count"]:
        conn.rollback()
        raise ArchiveError(
            f"level 1 块覆盖采样数 {total_samples} 与试验采样数 "
            f"{test['sample_count']} 不一致，拒绝归档"
        )

    # level 1 块必须首尾相接、无缝覆盖全部 rowid
    expect_first = level1[0]["first_rowid"]
    expect_last = level1[-1]["last_rowid"]
    cursor_rid = expect_first
    for b in level1:
        if b["first_rowid"] != cursor_rid:
            conn.rollback()
            raise ArchiveError("level 1 块 rowid 区间不连续，拒绝归档")
        cursor_rid = b["last_rowid"] + 1
    if cursor_rid != expect_last + 1:
        conn.rollback()
        raise ArchiveError("level 1 块未无缝覆盖全部采样，拒绝归档")

    try:
        inserted = 0
        raw_bytes = 0
        compressed_bytes = 0

        for b in level1:
            rows = conn.execute(
                """SELECT ts, pressure FROM samples
                   WHERE test_id = ? AND rowid BETWEEN ? AND ?
                   ORDER BY rowid""",
                (test_id, b["first_rowid"], b["last_rowid"]),
            ).fetchall()
            samples = [(r["ts"], r["pressure"]) for r in rows]

            if len(samples) != b["sample_count"]:
                raise ArchiveError(
                    f"块 {b['block_index']} 实际采样数与摘要不一致，拒绝归档"
                )

            payload = encode_samples(samples)
            # 提交前无损自检：解压结果必须与原采样逐点完全一致
            restored = decode_samples(payload)
            if restored != samples:
                raise ArchiveCodecError(
                    f"块 {b['block_index']} 压缩往返校验失败，拒绝归档"
                )

            conn.execute(
                """INSERT INTO compressed_blocks (
                        test_id, block_index, first_rowid, last_rowid,
                        sample_count, start_ts, end_ts, codec, payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    test_id,
                    b["block_index"],
                    b["first_rowid"],
                    b["last_rowid"],
                    len(samples),
                    samples[0][0],
                    samples[-1][0],
                    1,
                    sqlite3.Binary(payload),
                ),
            )
            inserted += 1
            raw_bytes += len(samples) * 16
            compressed_bytes += len(payload)

        if inserted != len(level1):  # pragma: no cover - 计数兜底
            raise ArchiveError("压缩块写入数量与 level 1 块数量不一致")

        deleted = conn.execute(
            "DELETE FROM samples WHERE test_id = ?", (test_id,)
        ).rowcount
        if deleted != total_samples:
            raise ArchiveError(
                f"原采样删除行数 {deleted} 与预期 {total_samples} 不一致，拒绝归档"
            )

        ratio = round(compressed_bytes / raw_bytes, 4) if raw_bytes else 0.0
        cur = conn.execute(
            """INSERT INTO archives (
                    test_id, block_count, sample_count,
                    raw_bytes, compressed_bytes, compression_ratio
                ) VALUES (?, ?, ?, ?, ?, ?)""",
            (test_id, inserted, total_samples, raw_bytes, compressed_bytes, ratio),
        )
        archive_id = cur.lastrowid

        info = dict(
            conn.execute(
                "SELECT * FROM archives WHERE id = ?", (archive_id,)
            ).fetchone()
        )
        info["already_archived"] = False
        conn.commit()  # 响应返回前提交，压缩块与采样删除一起生效
        return info
    except Exception:
        # 任何失败都整体回滚：压缩块不残留、原采样不删除，原查询仍可用
        conn.rollback()
        raise
