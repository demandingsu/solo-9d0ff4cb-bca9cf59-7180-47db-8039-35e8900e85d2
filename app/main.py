"""公交发车表数据平台 —— FastAPI 入口。

接口一览：
  POST /versions                    一次上传 stops/trips/stop_times 三份 CSV，校验后暂存新版本
  POST /versions/{id}/publish       整版发布（原子切换，失败则旧版继续供数）
  GET  /versions                    版本历史
  GET  /versions/current            当前发布版本
  GET  /stops/{stop_id}/departures  查询站点未来一小时发车（合并跨午夜班次）
  GET  /docs                        在线 API 文档（Swagger UI，静态资源本地内置，离线可用）
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from .csv_import import parse_and_validate
from .db import (
    Database,
    NoActiveVersion,
    StopNotFound,
    VersionNotFound,
    VersionNotPublishable,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(db_path: str | None = None) -> FastAPI:
    db = Database(db_path or os.environ.get("BUSFEED_DB", "./data/busfeed.db"))

    app = FastAPI(
        title="公交发车表数据平台",
        version="1.0.0",
        description=(
            "接收运营方每日三份 CSV（站点 / 班次 / 停站时刻），整版校验、整版发布，"
            "并为电子站牌提供按站点查询未来一小时发车的接口。\n\n"
            "**CSV 表头约定**\n"
            "- `stops.csv`: `stop_id,stop_name`\n"
            "- `trips.csv`: `trip_id,route_id,service_date`（UTC 运营日，YYYY-MM-DD）\n"
            "- `stop_times.csv`: `trip_id,stop_id,stop_sequence,departure_time`（00:00~29:59）"
        ),
        docs_url=None,  # /docs 由下方自定义，使用本地内置静态资源
    )
    app.state.db = db

    # ---- 在线文档：优先使用本地内置的 Swagger UI 资源，离线环境也能打开 /docs ----
    if (STATIC_DIR / "swagger-ui-bundle.js").exists() and (STATIC_DIR / "swagger-ui.css").exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/docs", include_in_schema=False)
        def docs_local():
            return get_swagger_ui_html(
                openapi_url="/openapi.json",
                title="公交发车表数据平台 · API 文档",
                swagger_js_url="/static/swagger-ui-bundle.js",
                swagger_css_url="/static/swagger-ui.css",
            )
    else:  # 本地资源缺失时回退到 CDN

        @app.get("/docs", include_in_schema=False)
        def docs_cdn():
            return get_swagger_ui_html(
                openapi_url="/openapi.json",
                title="公交发车表数据平台 · API 文档",
            )

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/docs")

    @app.get("/health", summary="健康检查")
    def health():
        return {"status": "ok"}

    # ---------------- 上传 ----------------

    @app.post(
        "/versions",
        status_code=201,
        summary="一次上传三份 CSV，校验通过后暂存为新版本",
        responses={422: {"description": "文件校验未通过（旧版本不受影响）"}},
    )
    async def upload_version(
        stops: UploadFile = File(..., description="stops.csv：stop_id,stop_name"),
        trips: UploadFile = File(..., description="trips.csv：trip_id,route_id,service_date"),
        stop_times: UploadFile = File(
            ..., description="stop_times.csv：trip_id,stop_id,stop_sequence,departure_time"
        ),
    ):
        parsed, errors = parse_and_validate(
            await stops.read(), await trips.read(), await stop_times.read()
        )
        if parsed is None:
            vid = db.record_invalid_version(errors)
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "文件校验未通过，未生成可用版本；当前发布版本继续供数",
                    "version_id": vid,
                    "errors": errors,
                },
            )
        vid = db.insert_staged_version(parsed)
        return {
            "version_id": vid,
            "status": "staged",
            "message": f"校验通过，版本 {vid} 已暂存；调用 POST /versions/{vid}/publish 后生效",
            "stops_count": len(parsed.stops),
            "trips_count": len(parsed.trips),
            "stop_times_count": len(parsed.stop_times),
        }

    # ---------------- 发布 ----------------

    @app.post(
        "/versions/{version_id}/publish",
        summary="整版发布指定版本（原子切换；失败时旧版继续供数）",
        responses={404: {"description": "版本不存在"}, 409: {"description": "版本状态不允许发布"}},
    )
    def publish_version(version_id: int):
        try:
            return db.publish(version_id)
        except VersionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except VersionNotPublishable as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.get("/versions", summary="版本历史（含校验失败记录）")
    def list_versions():
        return {"versions": db.list_versions()}

    @app.get(
        "/versions/current",
        summary="当前发布版本",
        responses={409: {"description": "尚无已发布版本"}},
    )
    def current_version():
        version = db.get_current_version()
        if version is None:
            raise HTTPException(status_code=409, detail="当前没有已发布的版本")
        return version

    # ---------------- 查询 ----------------

    @app.get(
        "/stops/{stop_id}/departures",
        summary="查询站点从指定 UTC 时刻起一小时内的发车",
        responses={
            404: {"description": "当前版本中不存在该站点"},
            409: {"description": "尚无已发布版本"},
        },
    )
    def stop_departures(
        stop_id: str,
        from_time: datetime | None = Query(
            default=None,
            alias="from",
            description="起始时刻（ISO 8601，建议带时区，如 2026-11-19T00:00:00Z；"
            "缺省为服务器当前 UTC 时间；不带时区时按 UTC 处理）",
        ),
    ):
        dt = from_time or datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        try:
            return db.get_stop_departures(stop_id, dt)
        except NoActiveVersion:
            raise HTTPException(status_code=409, detail="当前没有已发布的版本，无法查询")
        except StopNotFound:
            raise HTTPException(
                status_code=404, detail=f"当前发布版本中不存在站点 '{stop_id}'"
            )

    return app


app = create_app()
