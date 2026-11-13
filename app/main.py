"""FastAPI 入口：试验导入、列表、分桶查询。

启动后访问 /docs 即可在 Swagger UI 上传样例 CSV 并调整桶宽查询。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import sqlite3

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from sqlite3 import Connection

from . import config
from .archive import AlreadyArchived, ArchiveError, archive_test, get_archive
from .codec import ArchiveCodecError
from .db import init_db, session
from .ingest import CsvValidationError, import_test
from .schemas import ArchiveOut, ImportResponse, QueryResponse, TestOut
from .service import bucket_query


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="泵站启泵试验水锤压力分析",
    description=(
        "导入记录仪单次试验 CSV（毫秒时间戳、kPa 压力），"
        "在 SQLite 中保存原始采样与多级摘要；按起止时刻与桶宽分桶，"
        "返回每桶首/尾/最高/最低点，空桶标记为缺测。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


def get_conn() -> Connection:
    with session() as conn:
        yield conn


@app.get("/health", tags=["系统"])
def health():
    return {"status": "ok"}


@app.post(
    "/tests/import",
    response_model=ImportResponse,
    status_code=201,
    tags=["试验导入"],
    summary="上传单次试验 CSV",
)
def upload_test(
    file: UploadFile = File(..., description="记录仪导出的 CSV，首行表头"),
    name: str = Form(..., description="试验名称，库内唯一"),
    conn: Connection = Depends(get_conn),
):
    content = file.file.read(config.MAX_UPLOAD_BYTES + 1)
    if len(content) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"文件超过 {config.MAX_UPLOAD_BYTES} 字节上限",
        )
    if not content:
        raise HTTPException(status_code=400, detail="上传文件为空")
    try:
        result = import_test(conn, name=name, filename=file.filename or "", content=content)
    except CsvValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return result


_TEST_COLUMNS = (
    "t.id, t.name, t.source_filename, t.sample_count, t.start_ts, t.end_ts, "
    "t.max_level, t.imported_at, "
    "CASE WHEN a.test_id IS NULL THEN 0 ELSE 1 END AS archived"
)


@app.get("/tests", response_model=list[TestOut], tags=["试验查询"], summary="列出全部试验")
def list_tests(conn: Connection = Depends(get_conn)):
    rows = conn.execute(
        f"SELECT {_TEST_COLUMNS} FROM tests t "
        "LEFT JOIN archives a ON a.test_id = t.id ORDER BY t.id"
    ).fetchall()
    return [dict(r) for r in rows]


@app.get(
    "/tests/{test_id}",
    response_model=TestOut,
    tags=["试验查询"],
    summary="查看单个试验元信息",
)
def get_test(test_id: int, conn: Connection = Depends(get_conn)):
    row = conn.execute(
        f"SELECT {_TEST_COLUMNS} FROM tests t "
        "LEFT JOIN archives a ON a.test_id = t.id WHERE t.id = ?",
        (test_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"试验 {test_id} 不存在")
    return dict(row)


@app.get(
    "/tests/{test_id}/archive",
    response_model=ArchiveOut,
    tags=["试验归档"],
    summary="查看试验的归档状态与压缩统计",
)
def get_test_archive(test_id: int, conn: Connection = Depends(get_conn)):
    if conn.execute("SELECT 1 FROM tests WHERE id = ?", (test_id,)).fetchone() is None:
        raise HTTPException(status_code=404, detail=f"试验 {test_id} 不存在")
    info = get_archive(conn, test_id)
    if info is None:
        raise HTTPException(status_code=404, detail=f"试验 {test_id} 尚未归档")
    info["already_archived"] = False
    return info


@app.post(
    "/tests/{test_id}/archive",
    response_model=ArchiveOut,
    tags=["试验归档"],
    summary="按试验编号把压力明细无损压缩归档（按最细摘要块边界）",
)
def archive_test_by_id(test_id: int, conn: Connection = Depends(get_conn)):
    try:
        return archive_test(conn, test_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except AlreadyArchived as exc:
        # 幂等：重复请求返回既有归档，不写副本
        info = dict(exc.info)
        info["already_archived"] = True
        return info
    except (ArchiveError, ArchiveCodecError) as exc:
        # 事务已回滚：原采样与摘要均未改动，仍可按原接口查询
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except sqlite3.OperationalError as exc:
        # 写锁竞争（另一归档/导入正在提交）：本次未改动任何数据
        raise HTTPException(status_code=409, detail=f"归档暂时无法执行：{exc}") from None


@app.get(
    "/tests/{test_id}/query",
    response_model=QueryResponse,
    tags=["试验查询"],
    summary="按起止时刻与桶宽分桶，定位每桶水锤峰谷",
)
def query_buckets(
    test_id: int,
    start_ts: int = Query(..., description="查询起点毫秒时间戳（含），桶从该时刻起划分"),
    end_ts: int = Query(..., description="查询终点毫秒时间戳（不含），末桶截到该时刻"),
    bucket_width_ms: int = Query(..., gt=0, description="桶宽，毫秒，必须为正整数"),
    conn: Connection = Depends(get_conn),
):
    if end_ts <= start_ts:
        raise HTTPException(
            status_code=400, detail="end_ts 必须大于 start_ts（半开区间 [start, end)）"
        )
    try:
        return bucket_query(conn, test_id, start_ts, end_ts, bucket_width_ms)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
