"""API 路由聚合。"""
from __future__ import annotations

from fastapi import APIRouter

from app.api import bidders, documents, jobs, projects, reports, rules, runs, scores

api_router = APIRouter()
api_router.include_router(projects.router)
api_router.include_router(documents.router)
api_router.include_router(rules.router)
api_router.include_router(bidders.router)
api_router.include_router(runs.router)
api_router.include_router(scores.router)
api_router.include_router(reports.router)
api_router.include_router(jobs.router)
