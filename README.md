# 公交发车表数据平台

公交数据交接员的每日交接工具：接收运营方三份 CSV（站点 / 班次 / 停站时刻），**一次上传、整版校验、整版发布**，并向电子站牌提供**按站点查询未来一小时发车**的 API。

技术栈：FastAPI + SQLite（WAL），单容器即可运行。

## 快速开始

### Docker Compose（推荐）

```bash
docker compose up --build
```

服务监听 `http://localhost:8000`，SQLite 数据持久化在命名卷 `busfeed-data`。

### 本地开发

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload        # 数据库默认写入 ./data/busfeed.db（可用 BUSFEED_DB 覆盖）
```

### 在线文档

打开 <http://localhost:8000/docs>（Swagger UI，静态资源已内置，**离线可用**）。
每个接口都可点 "Try it out" 直接操作；`sample_data/` 里有可上传的示例 CSV。

## 一分钟上手

```bash
# 1. 一次上传三份文件，校验通过后暂存（此时不影响线上供数）
curl -F "stops=@sample_data/stops.csv" \
     -F "trips=@sample_data/trips.csv" \
     -F "stop_times=@sample_data/stop_times.csv" \
     http://localhost:8000/versions
# => {"version_id": 1, "status": "staged", ...}

# 2. 整版发布（原子切换；发布失败时旧版本继续供数）
curl -X POST http://localhost:8000/versions/1/publish

# 3. 查询站点 S1 从某 UTC 时刻起一小时内的发车
curl "http://localhost:8000/stops/S1/departures?from=2026-11-19T00:00:00Z"
```

响应示例（含线路、班次、实际发车 UTC 时刻、所用版本）：

```json
{
  "stop_id": "S1", "stop_name": "中央车站",
  "from": "2026-11-19T00:00:00Z", "to": "2026-11-19T01:00:00Z",
  "version_id": 1,
  "departures": [
    {"route_id": "R1", "trip_id": "T2", "service_date": "2026-11-19",
     "scheduled_departure": "00:20", "departure_time": "2026-11-19T00:20:00Z"},
    {"route_id": "R2", "trip_id": "T3", "service_date": "2026-11-18",
     "scheduled_departure": "24:30", "departure_time": "2026-11-19T00:30:00Z"}
  ]
}
```

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/versions` | 一次上传三份 CSV（multipart 字段名 `stops`/`trips`/`stop_times`），校验通过则暂存为新版本；校验失败返回 422 及全部错误，旧版本不受影响 |
| POST | `/versions/{id}/publish` | 整版发布：单事务内完成旧版下线 + 新版上线；失败整体回滚，旧版继续供数 |
| GET | `/versions` | 版本历史（含校验失败的留痕） |
| GET | `/versions/current` | 当前发布版本 |
| GET | `/stops/{stop_id}/departures?from=<ISO8601>` | 查询未来一小时发车；`from` 缺省为服务器当前 UTC 时间，不带时区按 UTC 处理 |
| GET | `/health` | 健康检查 |

## CSV 格式与发布前校验

| 文件 | 必需列 | 说明 |
|---|---|---|
| `stops.csv` | `stop_id,stop_name` | 站点编号、名称 |
| `trips.csv` | `trip_id,route_id,service_date` | 班次编号、线路、UTC 运营日期（YYYY-MM-DD） |
| `stop_times.csv` | `trip_id,stop_id,stop_sequence,departure_time` | 停站顺序（非负整数）与发车时刻 `00:00`~`29:59`（≥24:00 表示次日凌晨） |

发布前逐项核对，**任一规则不满足则整版拒收**（返回全部错误，一次最多 50 条）：

1. **编号唯一**：`stop_id`、`trip_id` 各自不得重复；
2. **引用完整**：`stop_times` 的班次、站点必须存在；
3. **同班次严格递增**：`(trip_id, stop_sequence)` 不重复，按停站顺序排列后发车时刻严格递增；
4. 时刻格式 `H:MM`/`HH:MM` 且在 00:00~29:59；日期为合法 YYYY-MM-DD；文件须为 UTF-8（兼容 BOM）。

## 查询语义

- 窗口为 `[from, from+1h)`，左闭右开；
- 合并两个运营日：**`from` 当日 + 前一运营日**。前一运营日 `24:00`~`29:59` 的班次实际发生在当日凌晨，会被换算成实际 UTC 时刻返回；次日运营日的班次不在合并范围内（其凌晨班次应由运营方以当日 `24:xx` 形式排班）；
- 结果按实际发车时间升序，响应中的 `version_id` 即本次查询所用版本。

## 版本一致性设计（上传 / 发布 / 查询交错）

- 全部版本数据共存一库，`meta` 表只有一个 `active_version_id` 指针；
- **发布**是单个事务：旧版置 `superseded` → 新版置 `active` → 翻转指针，任一步失败整体回滚，旧版继续供数；
- **查询**是单个只读事务：先读指针、再读该版本数据，同一快照保证一次查询绝不混用两版；
- **上传**只写入 `staged` 数据，不触碰指针，因此与查询任意交错也不影响供数；
- 进程内以一把锁串行化所有事务（单写者模型），并开启 SQLite WAL。

## 测试

```bash
python -m pytest tests/ -v
```

覆盖：跨午夜合并与窗口边界、12 类坏文件拒收且旧版继续供数、发布与多线程查询交错时的版本完整性（每个响应必须整体属于某一版本）、上传与查询交错、文档可用性。

## 已知限制

- 单进程单写者设计，适合交接员 + 站牌查询的负载；如需多副本水平扩展，应迁移到支持多写的数据库（事务边界已在 `app/db.py` 中封装，SQL 为标准语法）；
- 版本历史线性前进，已下线版本不允许回滚发布（如需回滚，重新上传该版文件生成新版本再发布）；
- 校验失败的暂存数据不落业务表，仅在 `versions` 表留痕错误清单。
