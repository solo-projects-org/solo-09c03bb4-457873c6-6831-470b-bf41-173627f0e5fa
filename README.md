# 泵站启泵试验 · 水锤压力峰谷分析服务

面向泵站调试员的启泵试验复盘工具：把记录仪导出的**单次试验 CSV** 导入
FastAPI 服务，采样与多级摘要落盘到 **SQLite**；复盘时给定起止时刻和桶宽，
按桶返回首尾点与压力最高 / 最低点，快速定位水锤造成的峰谷，空桶自动标记缺测。
验收通过后，档案员可按**试验编号**把长期留存的压力明细按最细摘要块边界
**无损压缩归档**到同一个 SQLite 库，日后仍用原试验编号查询峰谷。

## 功能与约定

- **CSV 导入**（`POST /tests/import`，Swagger 可直接上传）
  - 首行表头，自动识别时间戳列与压力列，支持中英文表头
    （`timestamp_ms / ts / 时间戳 …`、`pressure_kpa / pressure / 压力(kPa) …`），
    UTF-8 与 GBK 编码均可
  - 毫秒时间戳必须为整数且**严格递增**，重复或倒序整次拒绝（HTTP 400）
  - 压力为有限数值，单位 kPa；单事务写入，失败不留半成品
- **分桶查询**（`GET /tests/{id}/query`，归档前后接口与字段完全一致）
  - 从 `start_ts` 起按 `bucket_width_ms` 分桶，**含左端、不含右端**，
    末桶截到 `end_ts`
  - 每桶按时序返回首点、尾点、压力最高点、最低点；**同一点只列一次**，
    角色合并到 `roles`；极值并列时取时间戳最早的点
  - 桶内无采样时 `missing=true`、`points=[]`
- **多级摘要加速**（核心）
  - level 1 为最细索引块（默认每 128 个采样一块），其上逐级合并，
    每块保存首尾点、最高 / 最低点（含时间戳）、采样数与原始 rowid 区间
  - 查询时从最粗层级开始：**完整落在桶内的块直接采用摘要**；
    只有桶边界切开 level 1 最细块时，才按 rowid 区间回读采样
  - 响应中的 `metrics` 如实回显：`blocks_by_level`（各级摘要命中数）
    与 `raw_rows_read`（回读采样行数，边界不切块时为 0）
- **长期归档**（`POST /tests/{id}/archive`）
  - 以 **level 1 最细摘要块为边界**逐块无损压缩（定长 `int64` 时间戳 +
    `float64` 压力位模式，再 zlib 压缩），压缩块写入同库 `compressed_blocks` 表，
    与 level 1 摘要块按 `(test_id, block_index)` 一一对应
  - 压缩块写入与原 `samples` 行删除在**同一事务**内提交，要么整体生效
    要么整体回滚；归档失败时原数据完好，仍可正常查询
  - 提交前逐块做压缩-解压 round-trip 严格比对，任何不一致都回滚
  - 幂等：已归档的试验重复请求直接返回既有归档（`already_archived=true`），
    不写第二份副本；归档元信息见 `archives` 表与 `GET /tests/{id}/archive`
  - 原有多级摘要一行不动；归档后查询只有桶边界切开 level 1 块时才
    **解压对应压缩块**，`raw_rows_read` 仍只统计桶内采样；块不被切开时
    全程走摘要、零解压

## 目录结构

```
app/
  config.py      环境变量配置（库路径、块大小、上传上限、桶数上限）
  db.py          SQLite 连接、建表（tests / samples / blocks /
                 archives / compressed_blocks）
  codec.py       归档压缩块的无损二进制帧编解码（zlib + 严格校验）
  archive.py     按试验编号归档：单事务压缩、无损自检、删原采样、幂等控制
  ingest.py      CSV 解析、严格校验、导入
  summaries.py  多级摘要构建与区间递归查询（含归档后解压路径）
  service.py     分桶编排、点去重与角色合并
  schemas.py     Pydantic 请求 / 响应模型
  main.py        FastAPI 路由
sample_data/     样例 CSV（由 scripts/generate_sample.py 生成）
scripts/         样例生成脚本与归档端到端演示
tests/           pytest 测试（33 项）
Dockerfile, docker-compose.yml
```

## Docker Compose 启动

```bash
docker compose up --build
```

- 服务：<http://localhost:8000>
- Swagger UI：<http://localhost:8000/docs>
- 健康检查：`GET /health`
- SQLite 数据保存在命名卷 `pump-data`（容器内 `/data/pump_tests.db`），
  归档压缩块与原采样、摘要同在这一个库文件中

## 复现样例试验

样例 `sample_data/pump_start_demo.csv` 为 2 万点、10 ms 间隔的合成启泵曲线
（静压 → 启泵爬升 → 衰减振荡，含一次低压深谷），可重新生成：

```bash
python3 scripts/generate_sample.py
```

在 Swagger（`/docs`）中的操作步骤：
1. `POST /tests/import`：`name` 填 `demo-001`，`file` 选择
   `sample_data/pump_start_demo.csv`，执行。响应返回试验 id、时间范围与各级块数。
2. `GET /tests`：确认试验已入库，`archived=false`。
3. `GET /tests/{id}/query`：
   - `start_ts=1731300000050`、`end_ts=1731300004050`、`bucket_width_ms=1000`
     → 桶边界（1000 ms）与最细块边界（1280 ms）不对齐，`metrics.raw_rows_read>0`；
   - 改成 `bucket_width_ms=1280`（与块边界对齐）再试，
     可见 `metrics.raw_rows_read = 0`，全程只走摘要；
   - 换 `start_ts=1731299990000&end_ts=1731299995000` 可看到全部桶 `missing=true`。
4. `POST /tests/{id}/archive`：完成无损归档，返回压缩块数、压缩前后字节数与
   压缩比；再次调用返回 `already_archived=true`，不产生副本。
5. 用第 3 步完全相同的参数再次 `GET /tests/{id}/query`：分桶峰谷结果与
   `metrics` 逐字段一致，只是数据来源换成压缩块解压；`GET /tests/{id}` 的
   `archived` 已为 `true`。

小样本 `sample_data/small_demo.csv`（60 点）适合在 Swagger 里肉眼逐桶核对。

### 归档前后对比演示（推荐）

`scripts/demo_archive.py` 自动跑通「导入 → 归档前查询 → 归档 → 归档后同窗查询
→ 结果比对 → 对齐桶宽验证 → 重复归档幂等」全流程，演示同一段**切开最细摘要块**
的时窗在归档前后返回完全一致的峰谷：

```bash
docker compose up --build            # 先启动服务
python3 scripts/demo_archive.py      # 另开终端执行（仅用标准库）
```

### curl 示例

```bash
# 导入
curl -X POST http://localhost:8000/tests/import \
  -F "name=demo-001" \
  -F "file=@sample_data/pump_start_demo.csv;type=text/csv"

# 归档前：桶边界切开最细摘要块，回读原始采样
curl "http://localhost:8000/tests/1/query?start_ts=1731300000050&end_ts=1731300004050&bucket_width_ms=1000"

# 按试验编号归档（按最细摘要块边界无损压缩，原采样同事务删除）
curl -X POST http://localhost:8000/tests/1/archive

# 归档状态与压缩统计
curl http://localhost:8000/tests/1/archive

# 归档后：同一时窗同一查询，峰谷结果与读取统计不变（切块时改为解压压缩块）
curl "http://localhost:8000/tests/1/query?start_ts=1731300000050&end_ts=1731300004050&bucket_width_ms=1000"
```

## 本地开发（无 Docker）

```bash
python3 -m pip install -r requirements.txt
python3 -m pytest tests/ -q
PUMP_DB_PATH=./data/dev.db python3 -m uvicorn app.main:app --reload --port 8000
```

## 关键环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PUMP_DB_PATH` | `/data/pump_tests.db` | SQLite 文件路径 |
| `PUMP_BLOCK_SIZE` | `128` | 每级摘要块大小（点数 / 子块数），也是归档压缩块边界 |
| `PUMP_MAX_UPLOAD_BYTES` | `209715200` | 单次上传字节上限（200 MB） |
| `PUMP_MAX_BUCKETS` | `100000` | 单次查询桶数上限，防止桶宽过小 |
