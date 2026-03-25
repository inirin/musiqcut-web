"""피드백 수집 + 자동 생성 스케줄 관리"""
import json
import aiosqlite
from datetime import datetime
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
from backend.database import DB_PATH

router = APIRouter()


class FeedbackCreate(BaseModel):
    project_id: str
    step_no: Optional[int] = None
    scene_no: Optional[int] = None
    feedback_type: str  # 'like' / 'dislike' / 'comment'
    content: Optional[str] = None


class FeedbackUpdate(BaseModel):
    content: str


@router.post("")
async def submit_feedback(body: FeedbackCreate):
    """피드백 제출."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO feedback (project_id, step_no, scene_no, feedback_type, content) "
            "VALUES (?,?,?,?,?)",
            (body.project_id, body.step_no, body.scene_no,
             body.feedback_type, body.content))
        await db.commit()
    return {"ok": True}


@router.get("")
async def list_feedback(project_id: Optional[str] = None):
    """피드백 목록 조회."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if project_id:
            rows = await db.execute_fetchall(
                "SELECT * FROM feedback WHERE project_id=? ORDER BY created_at ASC",
                (project_id,))
        else:
            rows = await db.execute_fetchall(
                "SELECT * FROM feedback ORDER BY created_at ASC LIMIT 100")
    return [dict(r) for r in rows]


# ── 자동 생성 스케줄 ──

@router.get("/schedules")
async def get_all_schedules():
    from backend.services.scheduler_service import _get_schedule_config
    gen = await _get_schedule_config("generation")
    # 현재 생성 중인 작품 + 마지막 생성 작품 시간
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # 생성 중
        row = await db.execute_fetchall(
            "SELECT id, title, theme FROM projects "
            "WHERE status='running' ORDER BY updated_at DESC LIMIT 1")
        if row:
            r = row[0]
            gen["running_project"] = {
                "id": r["id"],
                "title": r["title"] or r["theme"][:30],
            }
        # 마지막 자동 생성된 작품의 생성 시점
        row = await db.execute_fetchall(
            "SELECT created_at FROM projects WHERE source='auto' ORDER BY created_at DESC LIMIT 1")
        if row:
            gen["last_created_at"] = row[0]["created_at"]
    return {"generation": gen}


@router.get("/schedule")
async def get_schedule(schedule_type: str = "generation"):
    from backend.services.scheduler_service import _get_schedule_config
    return await _get_schedule_config(schedule_type)


@router.post("/schedule")
async def set_schedule(schedule_type: str = "generation",
                       enabled: bool = True, interval_hours: float = 2.0):
    from backend.services.scheduler_service import (
        save_schedule_config, start_scheduler, stop_scheduler)
    await save_schedule_config(enabled, interval_hours, schedule_type)
    if enabled:
        start_scheduler(schedule_type)
    else:
        stop_scheduler(schedule_type)
    return {"ok": True, "schedule_type": schedule_type,
            "enabled": enabled, "interval_hours": interval_hours}


# ── 개별 피드백 수정/삭제 (동적 경로 — 맨 아래!) ──

@router.post("/{feedback_id}")
async def update_feedback(feedback_id: int, body: FeedbackUpdate):
    """피드백 수정."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE feedback SET content=? WHERE id=?",
            (body.content, feedback_id))
        await db.commit()
    return {"ok": True}


@router.delete("/{feedback_id}")
async def delete_feedback(feedback_id: int):
    """피드백 삭제."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM feedback WHERE id=?", (feedback_id,))
        await db.commit()
    return {"ok": True}
