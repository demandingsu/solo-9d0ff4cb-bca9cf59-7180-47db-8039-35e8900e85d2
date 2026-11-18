"""自测：跨日合并、坏文件拒收、上传/发布/查询交错的版本一致性。

运行：python -m pytest tests/ -v
"""
from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

# ---------------- 测试数据 ----------------

STOPS_V1 = "stop_id,stop_name\nS1,中央车站\nS2,东城口\n"
TRIPS_V1 = (
    "trip_id,route_id,service_date\n"
    "T1,R1,2026-11-18\n"
    "T2,R1,2026-11-19\n"
    "T3,R2,2026-11-18\n"
)
# T1: S1 23:50 -> S2 25:10（次日 01:10）
# T2: S1 00:20 -> S2 00:40
# T3: S1 24:30（次日 00:30）
STOP_TIMES_V1 = (
    "trip_id,stop_id,stop_sequence,departure_time\n"
    "T1,S1,1,23:50\n"
    "T1,S2,2,25:10\n"
    "T2,S1,1,00:20\n"
    "T2,S2,2,00:40\n"
    "T3,S1,1,24:30\n"
)
# V2 仅把 T2 在 S1 的发车从 00:20 改为 00:25，用于区分两版查询结果
STOP_TIMES_V2 = STOP_TIMES_V1.replace("T2,S1,1,00:20", "T2,S1,1,00:25")

FROM_MIDNIGHT = "2026-11-19T00:00:00Z"  # 窗口 [00:00, 01:00)


def expected_s1(t2_time: str):
    """S1 站点从 2026-11-19T00:00Z 起一小时的期望结果。"""
    return [
        {
            "route_id": "R1",
            "trip_id": "T2",
            "service_date": "2026-11-19",
            "scheduled_departure": t2_time,
            "departure_time": f"2026-11-19T{t2_time}:00Z",
        },
        {
            "route_id": "R2",
            "trip_id": "T3",
            "service_date": "2026-11-18",
            "scheduled_departure": "24:30",
            "departure_time": "2026-11-19T00:30:00Z",
        },
    ]


EXPECTED_V1 = expected_s1("00:20")
EXPECTED_V2 = expected_s1("00:25")


# ---------------- 辅助 ----------------


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def upload(client, stops=STOPS_V1, trips=TRIPS_V1, stop_times=STOP_TIMES_V1):
    return client.post(
        "/versions",
        files={
            "stops": ("stops.csv", stops.encode(), "text/csv"),
            "trips": ("trips.csv", trips.encode(), "text/csv"),
            "stop_times": ("stop_times.csv", stop_times.encode(), "text/csv"),
        },
    )


def publish(client, version_id):
    return client.post(f"/versions/{version_id}/publish")


def upload_and_publish(client, stop_times=STOP_TIMES_V1):
    r = upload(client, stop_times=stop_times)
    assert r.status_code == 201, r.text
    vid = r.json()["version_id"]
    r = publish(client, vid)
    assert r.status_code == 200, r.text
    return vid


def query_s1(client, from_=FROM_MIDNIGHT):
    return client.get("/stops/S1/departures", params={"from": from_})


# ---------------- 基础流程 ----------------


def test_no_version_yet(client):
    r = query_s1(client)
    assert r.status_code == 409
    r = client.get("/versions/current")
    assert r.status_code == 409


def test_upload_publish_query(client):
    vid = upload_and_publish(client)
    r = client.get("/versions/current")
    assert r.status_code == 200 and r.json()["id"] == vid
    assert r.json()["status"] == "active"

    r = query_s1(client)
    assert r.status_code == 200
    body = r.json()
    assert body["version_id"] == vid
    assert body["stop_name"] == "中央车站"
    assert body["from"] == "2026-11-19T00:00:00Z"
    assert body["to"] == "2026-11-19T01:00:00Z"
    assert body["departures"] == EXPECTED_V1


def test_upload_does_not_change_serving_until_publish(client):
    v1 = upload_and_publish(client)
    r = upload(client, stop_times=STOP_TIMES_V2)  # 只暂存，不发布
    assert r.status_code == 201
    body = query_s1(client).json()
    assert body["version_id"] == v1
    assert body["departures"] == EXPECTED_V1  # 仍是旧版数据


# ---------------- 跨午夜合并 ----------------


def test_cross_midnight_previous_service_day(client):
    """前一运营日 25:10 的班次应在次日凌晨窗口内出现，并换算成实际 UTC 时刻。"""
    upload_and_publish(client)
    r = client.get("/stops/S2/departures", params={"from": "2026-11-19T00:45:00Z"})
    assert r.status_code == 200
    deps = r.json()["departures"]
    assert len(deps) == 1
    assert deps[0]["trip_id"] == "T1"
    assert deps[0]["service_date"] == "2026-11-18"
    assert deps[0]["scheduled_departure"] == "25:10"
    assert deps[0]["departure_time"] == "2026-11-19T01:10:00Z"


def test_window_boundaries(client):
    """[from, from+1h) 左闭右开；合并范围 = from 当日 + 前一运营日（不含次日运营日）。"""
    upload_and_publish(client)
    r = client.get("/stops/S1/departures", params={"from": "2026-11-18T23:30:00Z"})
    deps = r.json()["departures"]
    got = [(d["trip_id"], d["departure_time"]) for d in deps]
    assert got == [
        ("T1", "2026-11-18T23:50:00Z"),  # 当日班次
        # T3 24:30 -> 2026-11-19T00:30:00Z 恰好等于窗口右端，被排除
        # T2 属于次日(11-19)运营日，不在"当日+前一运营日"的合并范围内
    ]


def test_naive_datetime_treated_as_utc(client):
    upload_and_publish(client)
    r = client.get("/stops/S1/departures", params={"from": "2026-11-19T00:00:00"})
    assert r.status_code == 200
    assert r.json()["departures"] == EXPECTED_V1


def test_unknown_stop_404(client):
    upload_and_publish(client)
    r = client.get("/stops/NOPE/departures", params={"from": FROM_MIDNIGHT})
    assert r.status_code == 404


# ---------------- 坏文件 ----------------

BAD_CASES = {
    "站点编号重复": (
        "stop_id,stop_name\nS1,甲\nS1,乙\n",
        TRIPS_V1,
        STOP_TIMES_V1,
        "重复",
    ),
    "班次编号重复": (
        STOPS_V1,
        "trip_id,route_id,service_date\nT1,R1,2026-11-18\nT1,R2,2026-11-18\n",
        STOP_TIMES_V1,
        "重复",
    ),
    "引用不存在的班次": (
        STOPS_V1,
        TRIPS_V1,
        "trip_id,stop_id,stop_sequence,departure_time\nT9,S1,1,10:00\n",
        "不存在的班次",
    ),
    "引用不存在的站点": (
        STOPS_V1,
        TRIPS_V1,
        "trip_id,stop_id,stop_sequence,departure_time\nT1,S9,1,10:00\n",
        "不存在的站点",
    ),
    "时刻未严格递增": (
        STOPS_V1,
        TRIPS_V1,
        "trip_id,stop_id,stop_sequence,departure_time\nT1,S1,1,10:00\nT1,S2,2,09:00\n",
        "严格递增",
    ),
    "时刻相等也不算递增": (
        STOPS_V1,
        TRIPS_V1,
        "trip_id,stop_id,stop_sequence,departure_time\nT1,S1,1,10:00\nT1,S2,2,10:00\n",
        "严格递增",
    ),
    "停站顺序重复": (
        STOPS_V1,
        TRIPS_V1,
        "trip_id,stop_id,stop_sequence,departure_time\nT1,S1,1,10:00\nT1,S2,1,11:00\n",
        "顺序 1 重复",
    ),
    "时刻超出29点59分": (
        STOPS_V1,
        TRIPS_V1,
        "trip_id,stop_id,stop_sequence,departure_time\nT1,S1,1,30:00\n",
        "00:00~29:59",
    ),
    "分钟非法": (
        STOPS_V1,
        TRIPS_V1,
        "trip_id,stop_id,stop_sequence,departure_time\nT1,S1,1,10:60\n",
        "00:00~29:59",
    ),
    "运营日期非法": (
        STOPS_V1,
        "trip_id,route_id,service_date\nT1,R1,2026-13-01\n",
        STOP_TIMES_V1,
        "合法日期",
    ),
    "缺少必需列": (
        STOPS_V1,
        TRIPS_V1,
        "trip_id,stop_id,stop_sequence\nT1,S1,1\n",
        "缺少必需列",
    ),
    "顺序不是整数": (
        STOPS_V1,
        TRIPS_V1,
        "trip_id,stop_id,stop_sequence,departure_time\nT1,S1,abc,10:00\n",
        "非负整数",
    ),
}


@pytest.mark.parametrize("case", BAD_CASES.keys())
def test_bad_files_rejected_and_old_version_keeps_serving(client, case):
    v1 = upload_and_publish(client)
    stops, trips, stop_times, keyword = BAD_CASES[case]

    r = upload(client, stops=stops, trips=trips, stop_times=stop_times)
    assert r.status_code == 422, f"{case}: {r.text}"
    detail = r.json()["detail"]
    assert any(keyword in e for e in detail["errors"]), f"{case}: {detail['errors']}"

    # 坏文件留痕为 invalid 版本，且不可发布
    bad_vid = detail["version_id"]
    assert publish(client, bad_vid).status_code == 409

    # 旧版继续供数，数据不变
    assert client.get("/versions/current").json()["id"] == v1
    body = query_s1(client).json()
    assert body["version_id"] == v1
    assert body["departures"] == EXPECTED_V1


def test_publish_unknown_version_404(client):
    assert publish(client, 999).status_code == 404


def test_republish_active_version_409(client):
    v1 = upload_and_publish(client)
    assert publish(client, v1).status_code == 409


# ---------------- 交错操作：发布与查询并发 ----------------


def test_interleaved_publish_and_queries(client):
    """多线程查询与一次发布交错：每个响应都必须完整地属于某一个版本。"""
    v1 = upload_and_publish(client)
    r = upload(client, stop_times=STOP_TIMES_V2)
    v2 = r.json()["version_id"]

    # 发布前查询 => 旧版
    assert query_s1(client).json()["version_id"] == v1

    responses, failures = [], []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                resp = query_s1(client)
                assert resp.status_code == 200
                responses.append(resp.json())
            except Exception as exc:  # noqa: BLE001
                failures.append(repr(exc))

    threads = [threading.Thread(target=reader) for _ in range(8)]
    for t in threads:
        t.start()
    time.sleep(0.3)
    assert publish(client, v2).status_code == 200
    time.sleep(0.3)
    stop.set()
    for t in threads:
        t.join()

    assert not failures, failures
    assert len(responses) > 20  # 确认确实发生了并发读取
    seen = set()
    for body in responses:
        seen.add(body["version_id"])
        if body["version_id"] == v1:
            assert body["departures"] == EXPECTED_V1, "响应混用了两个版本的数据"
        elif body["version_id"] == v2:
            assert body["departures"] == EXPECTED_V2, "响应混用了两个版本的数据"
        else:
            pytest.fail(f"出现未知版本 {body['version_id']}")
    assert seen == {v1, v2}  # 发布前后两个版本都被观察到，且各自完整

    # 发布后查询 => 新版
    final = query_s1(client).json()
    assert final["version_id"] == v2
    assert final["departures"] == EXPECTED_V2


def test_interleaved_upload_and_queries(client):
    """上传（暂存）与查询交错：暂存绝不影响线上供数。"""
    v1 = upload_and_publish(client)
    stop = threading.Event()
    failures = []

    def reader():
        while not stop.is_set():
            try:
                body = query_s1(client).json()
                assert body["version_id"] == v1
                assert body["departures"] == EXPECTED_V1
            except Exception as exc:  # noqa: BLE001
                failures.append(repr(exc))

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    for _ in range(10):
        assert upload(client, stop_times=STOP_TIMES_V2).status_code == 201
    stop.set()
    for t in threads:
        t.join()
    assert not failures, failures
    assert client.get("/versions/current").json()["id"] == v1


# ---------------- 文档 ----------------


def test_docs_and_openapi_available(client):
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    # 本地内置的 Swagger 静态资源（离线可用）
    assert client.get("/static/swagger-ui-bundle.js").status_code == 200
    assert client.get("/static/swagger-ui.css").status_code == 200
