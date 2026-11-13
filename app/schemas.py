"""HTTP 请求 / 响应模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class PointOut(BaseModel):
    ts: int = Field(..., description="毫秒时间戳")
    pressure: float = Field(..., description="压力 kPa")
    roles: list[str] = Field(
        ..., description="该点在桶内承担的角色：first/last/max/min"
    )


class BucketOut(BaseModel):
    bucket_index: int = Field(..., description="从 0 开始的桶序号")
    start_ts: int = Field(..., description="桶左边界（含），毫秒")
    end_ts: int = Field(..., description="桶右边界（不含），毫秒；末桶截到查询终点")
    sample_count: int = Field(..., description="桶内采样点数")
    missing: bool = Field(..., description="空桶（缺测）时为 true")
    points: list[PointOut] = Field(
        ..., description="按时序排列、去重后的首/尾/最高/最低点"
    )


class QueryMetrics(BaseModel):
    blocks_used: int = Field(..., description="直接采用摘要块的次数")
    blocks_by_level: dict[str, int] = Field(
        ..., description="各层级被直接采用的块数，层级越高粒度越粗"
    )
    raw_rows_read: int = Field(
        ..., description="回退读取原始采样的行数；边界不切块时为 0"
    )


class QueryResponse(BaseModel):
    test_id: int
    test_name: str
    start_ts: int
    end_ts: int
    bucket_width_ms: int
    bucket_count: int
    missing_bucket_count: int
    metrics: QueryMetrics
    buckets: list[BucketOut]


class TestOut(BaseModel):
    id: int
    name: str
    source_filename: str
    sample_count: int
    start_ts: int
    end_ts: int
    max_level: int
    imported_at: str
    archived: bool = Field(
        False, description="该试验是否已完成长期归档（原采样已压缩为压缩块）"
    )


class ImportResponse(TestOut):
    block_counts: dict[str, int] = Field(
        ..., description="每个摘要层级生成的块数"
    )


class ArchiveOut(BaseModel):
    id: int = Field(..., description="归档记录 id")
    test_id: int = Field(..., description="试验编号（沿用原试验 id 查询）")
    block_count: int = Field(..., description="压缩块数量，与 level 1 摘要块一一对应")
    sample_count: int = Field(..., description="被压缩的原始采样总行数")
    raw_bytes: int = Field(..., description="压缩前定长记录的字节数")
    compressed_bytes: int = Field(..., description="压缩帧落库字节数")
    compression_ratio: float = Field(
        ..., description="压缩比 compressed_bytes / raw_bytes"
    )
    archived_at: str = Field(..., description="归档完成时间（UTC 文本）")
    already_archived: bool = Field(
        ..., description="true 表示该试验此前已归档，本次请求未写入任何副本"
    )
