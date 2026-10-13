"""FastAPI 应用工厂。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import config
from app.db.session import init_db
from app.errors import AppError

log = logging.getLogger("bid_eval")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    config.upload_dir().mkdir(parents=True, exist_ok=True)
    config.render_dir().mkdir(parents=True, exist_ok=True)
    # 通用评分规则库：启动期强校验（坏模板 fail-fast，不让坏模板流入运行时）
    from app.rules.library.loader import load_templates

    load_templates()
    # 任务 worker 在 jobs/queue.py 就绪后接入（M0 后半段）
    from app.jobs.queue import start_worker, stop_worker

    await start_worker()
    try:
        yield
    finally:
        await stop_worker()


def create_app() -> FastAPI:
    app = FastAPI(title="RPF Agent", version="0.1.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(AppError)
    async def _app_error_handler(_req: Request, exc: AppError):
        return JSONResponse(
            status_code=exc.http_status,
            content={"code": exc.code, "message": exc.message, "detail": exc.detail},
        )

    @app.get("/api/health")
    def health() -> dict:
        from sqlalchemy import text

        from app.db.session import get_engine

        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return {
            "status": "ok",
            "db": "ok",
            "llm_provider": config.llm_provider(),
            "llm_model": config.llm_model(),
            "prompt_version": config.llm_prompt_version(),
        }

    from app.api.router import api_router

    app.include_router(api_router, prefix="/api")
    return app


app = create_app()
