"""归档压缩块的无损二进制编解码。

最细摘要块（level 1）覆盖的原始采样序列被无损压缩后存入同一 SQLite 库，
日后查询仍可逐点还原。帧格式（小端）：

    魔数 b"PCMP" | 版本 uint8(=1) | 原始点数 uint32 |
    原始字节长度 uint32 | zlib 载荷长度 uint32 | zlib 载荷

原始载荷为逐条采样拼接的定长记录：
    ts       int64（毫秒时间戳，严格递增）
    pressure float64（IEEE-754 双精度，位模式原样保留）

zlib 仅做熵压缩，不改变任何数值；decode 后通过严格比对（含时间戳递增
与长度校验）保证 round-trip 无损，归档失败时原采样行不会被删除。
"""

from __future__ import annotations

import struct
import zlib

MAGIC = b"PCMP"
CODEC_VERSION = 1
# 魔数(4) + 版本(1) + 点数(4) + 原始长度(4) + 载荷长度(4)
_HEADER = struct.Struct("<4sBIII")
_RECORD = struct.Struct("<qd")


class ArchiveCodecError(ValueError):
    """压缩块无法还原或还原结果不满足无损契约。"""


def encode_samples(samples: list[tuple[int, float]]) -> bytes:
    """把 (ts, pressure) 序列编码为自描述的 zlib 压缩帧。"""
    raw = b"".join(_RECORD.pack(int(ts), float(p)) for ts, p in samples)
    payload = zlib.compress(raw, level=9)
    return _HEADER.pack(
        MAGIC, CODEC_VERSION, len(samples), len(raw), len(payload)
    ) + payload


def decode_samples(blob: bytes) -> list[tuple[int, float]]:
    """解压压缩帧并严格校验，返回原始 (ts, pressure) 序列。"""
    head_size = _HEADER.size
    if len(blob) < head_size:
        raise ArchiveCodecError("压缩块长度小于帧头，数据已损坏")
    magic, version, count, raw_len, payload_len = _HEADER.unpack(
        blob[:head_size]
    )
    if magic != MAGIC:
        raise ArchiveCodecError("压缩块魔数不匹配，数据已损坏")
    if version != CODEC_VERSION:
        raise ArchiveCodecError(f"不支持的压缩块版本：{version}")
    payload = blob[head_size:]
    if len(payload) != payload_len:
        raise ArchiveCodecError("压缩块载荷长度与帧头声明不一致")
    if raw_len != count * _RECORD.size:
        raise ArchiveCodecError("压缩块声明的原始字节长度与点数不一致")
    try:
        raw = zlib.decompress(payload)
    except zlib.error as exc:  # pragma: no cover - 损坏载荷防御
        raise ArchiveCodecError(f"zlib 解压失败：{exc}") from None
    if len(raw) != raw_len:
        raise ArchiveCodecError("解压后的原始字节长度与帧头声明不一致")

    samples: list[tuple[int, float]] = []
    prev_ts = None
    for i in range(count):
        ts, pressure = _RECORD.unpack_from(raw, i * _RECORD.size)
        if prev_ts is not None and ts <= prev_ts:
            raise ArchiveCodecError("解压后的时间戳不再严格递增")
        samples.append((ts, pressure))
        prev_ts = ts
    return samples
