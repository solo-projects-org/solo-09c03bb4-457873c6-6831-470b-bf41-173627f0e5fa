"""归档能力测试：无损压缩、幂等、失败回滚、查询等价、读取统计与并发。

复用 test_api.py 建立的临时库环境变量约定（块大小 4），但使用独立的
试验数据，避免与未归档用例相互影响。
"""

from __future__ import annotations

import io
import os
import tempfile
import threading

import pytest

# 与 test_api.py 相同的隔离环境：临时库 + 块大小 4，必须在导入 app 前固定
_tmp = tempfile.TemporaryDirectory()
os.environ.setdefault("PUMP_DB_PATH", os.path.join(_tmp.name, "archive-test.db"))
os.environ.setdefault("PUMP_BLOCK_SIZE", "4")
os.environ.setdefault("PUMP_MAX_BUCKETS", "1000")

from fastapi.testclient import TestClient  # noqa: E402

from app import archive as archive_mod  # noqa: E402
from app.archive import archive_test, is_archived  # noqa: E402
from app.codec import ArchiveCodecError, decode_samples, encode_samples  # noqa: E402
from app.db import connect, init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()
client = TestClient(app)


def _csv(rows, header=("timestamp_ms", "pressure_kpa")) -> bytes:
    buf = io.StringIO()
    buf.write(",".join(header) + "\n")
    for ts, p in rows:
        buf.write(f"{int(ts)},{p}\n")
    return buf.getvalue().encode("utf-8")


def _import(name: str, rows) -> dict:
    resp = client.post(
        "/tests/import",
        data={"name": name},
        files={"file": (f"{name}.csv", _csv(rows), "text/csv")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _raw_rows(test_id: int):
    conn = connect()
    rows = conn.execute(
        "SELECT ts, pressure FROM samples WHERE test_id = ? ORDER BY rowid",
        (test_id,),
    ).fetchall()
    conn.close()
    return [(r["ts"], r["pressure"]) for r in rows]


# 12 个点，block_size=4 -> 3 个完整 level 1 块（含负值、极端值与并列）
ROWS = [
    (5000 + i * 100, float(p))
    for i, p in enumerate(
        [10, 12, 8, 15, 9, 14, 7, 20, 11, 6, -3.5, 1000000.25]
    )
]


# --------------------------------------------------------------- 编解码单测
def test_codec_roundtrip_preserves_exact_values():
    import math

    samples = [(1, 0.1), (2, -0.0), (3, 1.7e308), (4, math.nan)]
    blob = encode_samples(samples)
    restored = decode_samples(blob)
    assert len(restored) == len(samples)
    # NaN 不等于自身，逐字段比较；其余值（含 -0.0）必须位级一致
    for (ts, p), (ts2, p2) in zip(samples, restored):
        assert ts == ts2
        if math.isnan(p):
            assert math.isnan(p2)
        else:
            assert p == p2 and p2 == p
    assert restored[1][1] == 0.0 and math.copysign(1.0, restored[1][1]) == -1.0


def test_codec_rejects_truncated_blob():
    blob = encode_samples([(1, 1.0), (2, 2.0)])
    with pytest.raises(ArchiveCodecError):
        decode_samples(blob[:-2])


def test_codec_rejects_bad_magic():
    blob = bytearray(encode_samples([(1, 1.0)]))
    blob[0] = ord("X")
    with pytest.raises(ArchiveCodecError):
        decode_samples(bytes(blob))


# --------------------------------------------------------------- 归档主流程
@pytest.fixture(scope="module")
def imported():
    return _import("archive-unit-1", ROWS)


def test_archive_compresses_by_level1_boundaries(imported):
    test_id = imported["id"]
    raw_before = _raw_rows(test_id)
    assert len(raw_before) == 12

    resp = client.post(f"/tests/{test_id}/archive")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["already_archived"] is False
    assert body["test_id"] == test_id
    assert body["block_count"] == 3  # 与 level 1 块数一致
    assert body["sample_count"] == 12
    assert body["compressed_bytes"] < body["raw_bytes"]
    assert 0 < body["compression_ratio"] < 1

    conn = connect()
    # 原采样行已随压缩块写入在同一事务中删除
    left = conn.execute(
        "SELECT COUNT(*) FROM samples WHERE test_id = ?", (test_id,)
    ).fetchone()[0]
    assert left == 0
    # 压缩块与 level 1 摘要块一一对应、rowid 区间一致
    pairs = conn.execute(
        """SELECT c.block_index, c.first_rowid, c.last_rowid, c.sample_count,
                  b.first_rowid AS b_first, b.last_rowid AS b_last,
                  b.sample_count AS b_count
           FROM compressed_blocks c
           JOIN blocks b ON b.test_id = c.test_id
                        AND b.level = 1
                        AND b.block_index = c.block_index
           WHERE c.test_id = ?
           ORDER BY c.block_index""",
        (test_id,),
    ).fetchall()
    assert len(pairs) == 3
    for p in pairs:
        assert (p["first_rowid"], p["last_rowid"], p["sample_count"]) == (
            p["b_first"],
            p["b_last"],
            p["b_count"],
        )
    # 原有摘要一行未动
    block_total = conn.execute(
        "SELECT COUNT(*) FROM blocks WHERE test_id = ?", (test_id,)
    ).fetchone()[0]
    assert block_total == sum(imported["block_counts"].values())
    conn.close()


def test_repeated_archive_is_idempotent(imported):
    test_id = imported["id"]
    resp1 = client.post(f"/tests/{test_id}/archive")
    assert resp1.status_code == 200
    assert resp1.json()["already_archived"] is True

    conn = connect()
    n_archives = conn.execute(
        "SELECT COUNT(*) FROM archives WHERE test_id = ?", (test_id,)
    ).fetchone()[0]
    n_chunks = conn.execute(
        "SELECT COUNT(*) FROM compressed_blocks WHERE test_id = ?", (test_id,)
    ).fetchone()[0]
    conn.close()
    assert n_archives == 1  # 没有第二份归档记录
    assert n_chunks == 3  # 没有重复压缩块


def test_archive_status_endpoint(imported):
    test_id = imported["id"]
    resp = client.get(f"/tests/{test_id}/archive")
    assert resp.status_code == 200
    assert resp.json()["test_id"] == test_id

    listing = client.get("/tests").json()
    entry = next(t for t in listing if t["id"] == test_id)
    assert entry["archived"] is True
    detail = client.get(f"/tests/{test_id}").json()
    assert detail["archived"] is True


def test_archive_unknown_test_404():
    resp = client.post("/tests/999001/archive")
    assert resp.status_code == 404
    resp = client.get("/tests/999001/archive")
    assert resp.status_code == 404


# ----------------------------------------------------- 归档前后查询完全等价
def test_query_identical_before_and_after_archive():
    body = _import(
        "archive-query-equiv",
        [(8000 + i * 100, float(p)) for i, p in enumerate(
            [3, 1, 4, 1, 5, 9, 2, 6, 5, 3, 5, 8, 9, 7, 9, 3]
        )],
    )
    test_id = body["id"]
    params = {"start_ts": 8050, "end_ts": 9450, "bucket_width_ms": 250}
    before = client.get(f"/tests/{test_id}/query", params=params).json()
    assert before["metrics"]["raw_rows_read"] > 0  # 桶边界切开了 level 1 块

    ar = client.post(f"/tests/{test_id}/archive")
    assert ar.status_code == 200
    after = client.get(f"/tests/{test_id}/query", params=params).json()

    # 分桶结果（含峰谷点、角色、缺测标记）逐字段一致
    assert after["buckets"] == before["buckets"]
    assert after["metrics"] == before["metrics"]
    # 读取统计口径不变：只计桶内采样，且对齐桶宽时全程不碰压缩块
    aligned = client.get(
        f"/tests/{test_id}/query",
        params={"start_ts": 8000, "end_ts": 9600, "bucket_width_ms": 400},
    ).json()
    assert aligned["metrics"]["raw_rows_read"] == 0
    assert aligned["metrics"]["blocks_used"] >= 1


def test_archived_query_matches_bruteforce(imported):
    """归档后随机区间对拍：解压路径必须还原出与原数据一致的统计。"""
    test_id = imported["id"]
    conn = connect()
    chunks = conn.execute(
        "SELECT block_index, payload FROM compressed_blocks WHERE test_id = ? "
        "ORDER BY block_index",
        (test_id,),
    ).fetchall()
    raw_rows = [pt for ch in chunks for pt in decode_samples(ch["payload"])]
    conn.close()

    lo_ts, hi_ts = raw_rows[0][0], raw_rows[-1][0]
    for q_start, q_end in [
        (lo_ts + 50, lo_ts + 450),
        (lo_ts, lo_ts + 100),
        (lo_ts + 250, hi_ts),
        (hi_ts - 150, hi_ts + 500),
        (lo_ts - 999, lo_ts - 100),  # 完全落在数据外 -> 缺测
    ]:
        resp = client.get(
            f"/tests/{test_id}/query",
            params={
                "start_ts": q_start,
                "end_ts": q_end,
                "bucket_width_ms": max(1, (q_end - q_start) // 3 or 1),
            },
        )
        assert resp.status_code == 200, resp.text
        got = sum(b["sample_count"] for b in resp.json()["buckets"])
        expected = sum(1 for ts, _ in raw_rows if q_start <= ts < q_end)
        assert got == expected


def test_archived_raw_rows_count_only_in_bucket(imported):
    """桶边界切开 level 1 块才解压；回读行数只统计桶内采样，不含块内桶外点。"""
    test_id = imported["id"]
    # 单桶 [5150, 5250)：只含 ts=5200 一个点，但它所在 level 1 块有 4 个点
    resp = client.get(
        f"/tests/{test_id}/query",
        params={"start_ts": 5150, "end_ts": 5250, "bucket_width_ms": 100},
    )
    body = resp.json()
    assert body["metrics"]["raw_rows_read"] == 1
    bucket = body["buckets"][0]
    assert bucket["sample_count"] == 1
    assert bucket["points"][0]["ts"] == 5200


# --------------------------------------------------------------- 失败回滚
def test_failed_archive_keeps_raw_samples():
    body = _import(
        "archive-fail-rollback",
        [(12000 + i * 100, float(i)) for i in range(8)],
    )
    test_id = body["id"]
    raw_before = _raw_rows(test_id)

    conn = connect()
    # 模拟压缩块写入后、事务提交前解压校验失败。
    # archive.py 以 from-import 把 decode_samples 绑定进自己的命名空间，
    # 必须 patch archive_mod.decode_samples 才能拦到调用点。
    original_decode = archive_mod.decode_samples

    def broken_decode(blob):
        result = original_decode(blob)
        return result[:-1]  # 丢掉末点 -> round-trip 比对必然失败

    archive_mod.decode_samples = broken_decode
    try:
        with pytest.raises(ArchiveCodecError):
            archive_test(conn, test_id)
        conn.rollback()
        # 同一故障注入下，路由层表现为 409 且不产生任何归档数据
        resp = client.post(f"/tests/{test_id}/archive")
        assert resp.status_code == 409
    finally:
        archive_mod.decode_samples = original_decode
    conn.close()

    assert _raw_rows(test_id) == raw_before  # 原采样一行未删
    conn = connect()
    assert conn.execute(
        "SELECT COUNT(*) FROM compressed_blocks WHERE test_id = ?", (test_id,)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM archives WHERE test_id = ?", (test_id,)
    ).fetchone()[0] == 0
    # 归档失败后原查询接口仍可用
    q = client.get(
        f"/tests/{test_id}/query",
        params={"start_ts": 12000, "end_ts": 12800, "bucket_width_ms": 200},
    )
    assert q.status_code == 200
    assert sum(b["sample_count"] for b in q.json()["buckets"]) == 8
    conn.close()


# --------------------------------------------------------------- 并发归档
def test_concurrent_archives_only_one_wins(imported):
    test_id = imported["id"]  # 该试验已归档，这里换一个新试验验证竞争
    body = _import(
        "archive-concurrent",
        [(20000 + i * 100, float(i * 1.5)) for i in range(16)],
    )
    new_id = body["id"]
    results: list[dict] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(4)

    def worker():
        conn = connect()
        try:
            barrier.wait()
            try:
                info = archive_test(conn, new_id)
                conn.commit()  # 唯一胜出者提交写事务
                results.append(info)
            except archive_mod.AlreadyArchived as exc:
                conn.rollback()
                results.append(exc.info)
            except Exception as exc:  # 写锁等待超时等：本次未改动数据
                conn.rollback()
                errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert len(results) == 4
    conn = connect()
    assert conn.execute(
        "SELECT COUNT(*) FROM archives WHERE test_id = ?", (new_id,)
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM compressed_blocks WHERE test_id = ?", (new_id,)
    ).fetchone()[0] == 4
    assert conn.execute(
        "SELECT COUNT(*) FROM samples WHERE test_id = ?", (new_id,)
    ).fetchone()[0] == 0
    conn.close()


# ------------------------------------------------- 提交时序（read-your-writes）
def test_import_is_committed_before_returning():
    """写操作返回前必须已提交：另一个全新连接要立刻看到数据。

    回归 uvicorn 多线程下“依赖清理在响应后才提交、紧接请求读不到新写入”
    的竞态；TestClient 同进程无法暴露时序，这里直接验证提交契约。
    """
    from app.ingest import import_test

    rows = [(30000 + i * 100, float(i)) for i in range(8)]
    writer = connect()
    result = import_test(
        writer, name="commit-timing", filename="c.csv", content=_csv(rows)
    )
    test_id = result["id"]
    try:
        # 不调用 writer.commit()：能读到即说明 import_test 已自行提交
        reader = connect()
        visible = reader.execute(
            "SELECT COUNT(*) FROM tests WHERE id = ?", (test_id,)
        ).fetchone()[0]
        sample_n = reader.execute(
            "SELECT COUNT(*) FROM samples WHERE test_id = ?", (test_id,)
        ).fetchone()[0]
        reader.close()
        assert visible == 1 and sample_n == 8
    finally:
        writer.close()


def test_archive_is_committed_before_returning():
    body = _import(
        "archive-commit-timing",
        [(40000 + i * 100, float(i)) for i in range(8)],
    )
    test_id = body["id"]
    writer = connect()
    try:
        archive_test(writer, test_id)  # 内部已提交
        reader = connect()
        n_arch = reader.execute(
            "SELECT COUNT(*) FROM archives WHERE test_id = ?", (test_id,)
        ).fetchone()[0]
        n_left = reader.execute(
            "SELECT COUNT(*) FROM samples WHERE test_id = ?", (test_id,)
        ).fetchone()[0]
        n_chunks = reader.execute(
            "SELECT COUNT(*) FROM compressed_blocks WHERE test_id = ?", (test_id,)
        ).fetchone()[0]
        reader.close()
        assert n_arch == 1 and n_left == 0 and n_chunks == 2
    finally:
        writer.close()


def test_query_during_inflight_archive_sees_consistent_snapshot():
    """归档写事务未提交时，独立连接读到完整的归档前快照，且看不到半成品。

    保证单次查询只可能看到“归档前”或“归档后”的完整数据，
    读不被写阻塞（WAL），也不会读到压缩块已写、原采样未删的中间态。
    """
    import sqlite3

    from app.codec import encode_samples

    body = _import(
        "archive-snapshot",
        [(50000 + i * 100, float(i)) for i in range(12)],
    )
    test_id = body["id"]

    writer = connect()
    writer.execute("BEGIN IMMEDIATE")  # 模拟“归档进行中”的写事务
    try:
        b0 = writer.execute(
            "SELECT block_index, first_rowid, last_rowid FROM blocks "
            "WHERE test_id = ? AND level = 1 ORDER BY block_index LIMIT 1",
            (test_id,),
        ).fetchone()
        srows = writer.execute(
            "SELECT ts, pressure FROM samples WHERE test_id = ? "
            "AND rowid BETWEEN ? AND ? ORDER BY rowid",
            (test_id, b0["first_rowid"], b0["last_rowid"]),
        ).fetchall()
        payload = encode_samples([(r["ts"], r["pressure"]) for r in srows])
        writer.execute(
            "INSERT INTO compressed_blocks (test_id, block_index, first_rowid, "
            "last_rowid, sample_count, start_ts, end_ts, codec, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                test_id, b0["block_index"], b0["first_rowid"], b0["last_rowid"],
                len(srows), srows[0]["ts"], srows[-1]["ts"], 1,
                sqlite3.Binary(payload),
            ),
        )
        # 写事务仍未提交：独立只读连接看到的是完整归档前状态
        reader = connect()
        try:
            n_samples = reader.execute(
                "SELECT COUNT(*) FROM samples WHERE test_id = ?", (test_id,)
            ).fetchone()[0]
            n_chunks = reader.execute(
                "SELECT COUNT(*) FROM compressed_blocks WHERE test_id = ?",
                (test_id,),
            ).fetchone()[0]
            assert n_samples == 12  # 原采样一行不少
            assert n_chunks == 0    # 未提交的压缩块对外不可见
            assert is_archived(reader, test_id) is False
        finally:
            reader.close()
    finally:
        writer.rollback()
        writer.close()
