"""三份 CSV 文件（站点 / 班次 / 停站时刻）的解析与发布前校验。

校验规则（对应需求）：
  1. 编号唯一：stops.stop_id 唯一，trips.trip_id 唯一；
  2. 引用完整：stop_times.trip_id 必须存在于 trips，stop_times.stop_id 必须存在于 stops；
  3. 同班次内 (trip_id, stop_sequence) 不重复，且按停站顺序排列后发车时刻严格递增；
  4. 发车时刻格式 HH:MM（也接受 H:MM），范围 00:00 ~ 29:59（跨午夜时刻）；
  5. 运营日期为合法的 YYYY-MM-DD（UTC 运营日）。

所有校验错误一次性收集（上限 MAX_ERRORS 条），便于交接员一次修完。
"""
from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from dataclasses import dataclass, field

MAX_ERRORS = 50

# 00:00 ~ 29:59；小时允许 1~2 位数字，分钟必须两位
_TIME_RE = re.compile(r"^(\d{1,2}):([0-5]\d)$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SEQ_RE = re.compile(r"^\d{1,9}$")

REQUIRED_COLUMNS = {
    "stops.csv": ["stop_id", "stop_name"],
    "trips.csv": ["trip_id", "route_id", "service_date"],
    "stop_times.csv": ["trip_id", "stop_id", "stop_sequence", "departure_time"],
}


@dataclass
class ParsedData:
    """校验通过后的内存数据，等待整版写入。"""

    stops: list[tuple[str, str]] = field(default_factory=list)  # (stop_id, stop_name)
    trips: list[tuple[str, str, str]] = field(default_factory=list)  # (trip_id, route_id, service_date)
    stop_times: list[tuple[str, str, int, int]] = field(default_factory=list)  # (trip_id, stop_id, seq, minutes)


def _err(errors: list[str], msg: str) -> None:
    if len(errors) < MAX_ERRORS:
        errors.append(msg)


def _read_rows(label: str, raw: bytes, required: list[str], errors: list[str]):
    """读取 CSV，返回 [(行号, {列: 值}), ...]；结构性错误时返回 None。"""
    try:
        text = raw.decode("utf-8-sig")  # 兼容带 BOM 的 Excel 导出
    except UnicodeDecodeError:
        _err(errors, f"{label}: 文件不是有效的 UTF-8 编码")
        return None
    try:
        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames is None:
            _err(errors, f"{label}: 缺少表头行")
            return None
        reader.fieldnames = [(f or "").strip() for f in reader.fieldnames]
        missing = [c for c in required if c not in reader.fieldnames]
        if missing:
            _err(errors, f"{label}: 缺少必需列: {', '.join(missing)}")
            return None
        rows = []
        for lineno, row in enumerate(reader, start=2):  # 表头是第 1 行
            if None in row or None in row.values():
                _err(errors, f"{label}: 第 {lineno} 行的列数与表头不一致")
                continue
            rows.append((lineno, {k: (v or "").strip() for k, v in row.items()}))
        return rows
    except csv.Error as exc:
        _err(errors, f"{label}: CSV 解析失败: {exc}")
        return None


def _parse_time(value: str):
    """'HH:MM' -> 距运营日 00:00 的分钟数；非法返回 None。"""
    m = _TIME_RE.match(value)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 29:
        return None
    return hour * 60 + minute


def _fmt_time(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse_and_validate(
    stops_raw: bytes, trips_raw: bytes, stop_times_raw: bytes
) -> tuple[ParsedData | None, list[str]]:
    """解析并校验三份文件。返回 (数据, 错误列表)；有错误时数据为 None。"""
    errors: list[str] = []
    stops_rows = _read_rows("stops.csv", stops_raw, REQUIRED_COLUMNS["stops.csv"], errors)
    trips_rows = _read_rows("trips.csv", trips_raw, REQUIRED_COLUMNS["trips.csv"], errors)
    st_rows = _read_rows("stop_times.csv", stop_times_raw, REQUIRED_COLUMNS["stop_times.csv"], errors)
    if stops_rows is None or trips_rows is None or st_rows is None:
        return None, errors

    # ---- stops：编号唯一、名称非空 ----
    stops: list[tuple[str, str]] = []
    stop_ids: set[str] = set()
    for lineno, row in stops_rows:
        sid, name = row["stop_id"], row["stop_name"]
        if not sid:
            _err(errors, f"stops.csv: 第 {lineno} 行 stop_id 为空")
            continue
        if not name:
            _err(errors, f"stops.csv: 第 {lineno} 行 stop_name 为空")
            continue
        if sid in stop_ids:
            _err(errors, f"stops.csv: 站点编号 '{sid}' 重复（第 {lineno} 行）")
            continue
        stop_ids.add(sid)
        stops.append((sid, name))

    # ---- trips：编号唯一、线路非空、运营日期合法 ----
    trips: list[tuple[str, str, str]] = []
    trip_ids: set[str] = set()
    for lineno, row in trips_rows:
        tid, route, sdate = row["trip_id"], row["route_id"], row["service_date"]
        if not tid:
            _err(errors, f"trips.csv: 第 {lineno} 行 trip_id 为空")
            continue
        if not route:
            _err(errors, f"trips.csv: 第 {lineno} 行 route_id 为空")
            continue
        if not _DATE_RE.match(sdate):
            _err(errors, f"trips.csv: 第 {lineno} 行 service_date '{sdate}' 不是 YYYY-MM-DD 格式")
            continue
        try:
            from datetime import date

            date.fromisoformat(sdate)
        except ValueError:
            _err(errors, f"trips.csv: 第 {lineno} 行 service_date '{sdate}' 不是合法日期")
            continue
        if tid in trip_ids:
            _err(errors, f"trips.csv: 班次编号 '{tid}' 重复（第 {lineno} 行）")
            continue
        trip_ids.add(tid)
        trips.append((tid, route, sdate))

    # ---- stop_times：引用完整、顺序不重复、时刻合法且同班次严格递增 ----
    stop_times: list[tuple[str, str, int, int]] = []
    seen_seq: set[tuple[str, int]] = set()
    per_trip: dict[str, list[tuple[int, int]]] = defaultdict(list)  # trip -> [(seq, minutes)]
    for lineno, row in st_rows:
        tid, sid = row["trip_id"], row["stop_id"]
        seq_raw, time_raw = row["stop_sequence"], row["departure_time"]
        ok = True
        if tid not in trip_ids:
            _err(errors, f"stop_times.csv: 第 {lineno} 行引用了不存在的班次 '{tid}'")
            ok = False
        if sid not in stop_ids:
            _err(errors, f"stop_times.csv: 第 {lineno} 行引用了不存在的站点 '{sid}'")
            ok = False
        if not _SEQ_RE.match(seq_raw):
            _err(errors, f"stop_times.csv: 第 {lineno} 行 stop_sequence '{seq_raw}' 不是非负整数")
            ok = False
        minutes = _parse_time(time_raw)
        if minutes is None:
            _err(errors, f"stop_times.csv: 第 {lineno} 行 departure_time '{time_raw}' 不在 00:00~29:59 范围内")
            ok = False
        if not ok:
            continue
        seq = int(seq_raw)
        if (tid, seq) in seen_seq:
            _err(errors, f"stop_times.csv: 班次 '{tid}' 的停站顺序 {seq} 重复（第 {lineno} 行）")
            continue
        seen_seq.add((tid, seq))
        stop_times.append((tid, sid, seq, minutes))
        per_trip[tid].append((seq, minutes))

    for tid, items in per_trip.items():
        items.sort()
        for (seq_a, min_a), (seq_b, min_b) in zip(items, items[1:]):
            if min_b <= min_a:
                _err(
                    errors,
                    f"stop_times.csv: 班次 '{tid}' 的停站时刻未严格递增"
                    f"（顺序 {seq_a} 时刻 {_fmt_time(min_a)}，顺序 {seq_b} 时刻 {_fmt_time(min_b)}）",
                )

    if errors:
        return None, errors
    return ParsedData(stops=stops, trips=trips, stop_times=stop_times), []
