#!/usr/bin/env python3
"""归档能力端到端演示：导入 -> 归档前查询 -> 归档 -> 归档后同窗查询对比。

演示重点（对应验收要求）：
1. 选一个桶边界切开 level 1 最细摘要块的时窗，归档前需要回读原始采样；
2. 调用 POST /tests/{id}/archive 按最细摘要块边界无损压缩，
   压缩块写回同一个 SQLite 库，原采样行在同一事务内删除；
3. 用完全相同的参数再次查询：分桶结果逐点一致，读取统计一致，
   只是数据来源从 samples 表换成 compressed_blocks 解压；
4. 再用与块边界对齐的桶宽查询，验证全程只走摘要、raw_rows_read=0；
5. 重复归档请求返回 already_archived=true，不写副本。

仅使用标准库，默认对接 docker compose 启动的本地服务：
    docker compose up --build
    python3 scripts/demo_archive.py
可用环境变量覆盖：PUMP_BASE_URL、PUMP_DEMO_CSV、PUMP_DEMO_NAME。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE_URL = os.getenv("PUMP_BASE_URL", "http://localhost:8000")
CSV_PATH = Path(
    os.getenv(
        "PUMP_DEMO_CSV",
        Path(__file__).resolve().parent.parent
        / "sample_data"
        / "pump_start_demo.csv",
    )
)
DEMO_NAME = os.getenv("PUMP_DEMO_NAME", "archive-demo-001")


def _request(method: str, path: str, data: bytes | None = None,
             content_type: str | None = None) -> tuple[int, dict]:
    url = BASE_URL.rstrip("/") + "/" + path.lstrip("/")
    req = urllib.request.Request(url, data=data, method=method)
    if content_type:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _multipart_body(name: str, filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = "----pump-demo-boundary-7f3a91c2"
    parts = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="name"\r\n\r\n'
        f"{name}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: text/csv\r\n\r\n"
    ).encode("utf-8")
    body = parts + content + f"\r\n--{boundary}--\r\n".encode("utf-8")
    return body, f"multipart/form-data; boundary={boundary}"


def _print(title: str, body: dict) -> None:
    print(f"\n=== {title} ===")
    print(
        f"桶数={body['bucket_count']} 缺测桶={body['missing_bucket_count']} "
        f"摘要命中 blocks_used={body['metrics']['blocks_used']} "
        f"按级={body['metrics']['blocks_by_level']} "
        f"回读/解压采样 raw_rows_read={body['metrics']['raw_rows_read']}"
    )
    for bucket in body["buckets"]:
        if bucket["missing"]:
            print(
                f"  桶{bucket['bucket_index']} "
                f"[{bucket['start_ts']}, {bucket['end_ts']}) 缺测"
            )
            continue
        roles = ", ".join(
            f"{pt['ts']}={pt['pressure']}kPa({'/'.join(pt['roles'])})"
            for pt in bucket["points"]
        )
        print(
            f"  桶{bucket['bucket_index']} "
            f"[{bucket['start_ts']}, {bucket['end_ts']}) "
            f"{bucket['sample_count']} 点 -> {roles}"
        )


def main() -> int:
    print(f"服务地址：{BASE_URL}")
    status, _ = _request("GET", "/health")
    if status != 200:
        print(f"服务不可达（/health 返回 {status}），请先 docker compose up --build")
        return 1

    if not CSV_PATH.exists():
        print(f"找不到样例 CSV：{CSV_PATH}，可先运行 python3 scripts/generate_sample.py")
        return 1

    # 同名演示试验已存在则直接复用，保证脚本可重复执行
    status, listing = _request("GET", "/tests")
    existing = next((t for t in listing if t["name"] == DEMO_NAME), None)
    if existing is not None:
        test_id = existing["id"]
        print(f"复用已存在的演示试验 id={test_id}（如需全新演示请更换 PUMP_DEMO_NAME）")
    else:
        body, ctype = _multipart_body(
            DEMO_NAME, CSV_PATH.name, CSV_PATH.read_bytes()
        )
        status, imported = _request(
            "POST", "/tests/import", data=body, content_type=ctype
        )
        if status != 201:
            print(f"导入失败：{status} {imported}")
            return 1
        test_id = imported["id"]
        print(
            f"导入完成 id={test_id} 采样={imported['sample_count']} "
            f"各级块数={imported['block_counts']}"
        )

    # 演示时窗：10ms 采样、128 点/块（块宽 1280ms）。
    # 桶宽取 1000ms 且起点偏移 50ms，桶边界必然切开 level 1 最细摘要块。
    window = {
        "start_ts": 1_731_300_000_000 + 50,
        "end_ts": 1_731_300_000_000 + 4_050,
        "bucket_width_ms": 1000,
    }
    qs = urllib.parse.urlencode(window)

    status, before = _request("GET", f"/tests/{test_id}/query?{qs}")
    assert status == 200, before
    _print("归档前查询（边界切块 -> 回读 samples 原表）", before)

    status, archive_info = _request("POST", f"/tests/{test_id}/archive")
    if status != 200:
        print(f"归档失败：{status} {archive_info}")
        return 1
    print("\n=== 归档结果 ===")
    print(json.dumps(archive_info, ensure_ascii=False, indent=2))

    status, again = _request("POST", f"/tests/{test_id}/archive")
    print(
        f"重复归档请求 HTTP {status}，already_archived="
        f"{again.get('already_archived')}（不产生第二份压缩块）"
    )

    status, after = _request("GET", f"/tests/{test_id}/query?{qs}")
    assert status == 200, after
    _print("归档后查询（同一时窗 -> 解压 compressed_blocks）", after)

    if before["buckets"] != after["buckets"]:
        print("校验失败：归档前后分桶结果不一致！")
        return 1
    if before["metrics"] != after["metrics"]:
        print("校验失败：归档前后读取统计不一致！")
        return 1
    print(
        "\n✓ 归档前后分桶结果与读取统计完全一致："
        f"raw_rows_read 均为 {after['metrics']['raw_rows_read']}（只计桶内采样）"
    )

    # 与块边界对齐的桶宽（1280ms = 128 点 × 10ms）：全程摘要命中，无需解压
    aligned = {
        "start_ts": 1_731_300_000_000,
        "end_ts": 1_731_300_000_000 + 5_120,
        "bucket_width_ms": 1280,
    }
    status, aligned_resp = _request(
        "GET", f"/tests/{test_id}/query?{urllib.parse.urlencode(aligned)}"
    )
    print(
        "\n对齐桶宽查询："
        f"raw_rows_read={aligned_resp['metrics']['raw_rows_read']}，"
        f"blocks_used={aligned_resp['metrics']['blocks_used']}"
        "（块不被切开时不解压任何压缩块）"
    )

    status, detail = _request("GET", f"/tests/{test_id}")
    print(f"试验详情 archived={detail['archived']}，原查询接口与字段保持不变")
    print("\n演示完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
