"""피드백 수집 + 프롬프트 자동 개선"""
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


# ── 스케줄 (구체적 경로 먼저) ──

@router.get("/schedules")
async def get_all_schedules():
    from backend.services.scheduler_service import _get_schedule_config
    gen = await _get_schedule_config("generation")
    fb = await _get_schedule_config("feedback")
    return {"generation": gen, "feedback": fb}


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


# ── 피드백 분석 + 프롬프트 개선 ──

@router.post("/process")
async def process_feedback():
    """미처리 피드백 분석 → 프롬프트 개선."""
    from backend.services.feedback_service import analyze_and_improve

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT * FROM feedback WHERE processed=0 ORDER BY created_at")
        feedbacks = [dict(r) for r in rows]

    if not feedbacks:
        return {"ok": True, "message": "처리할 피드백이 없습니다.", "improvements": []}

    project_ids = list(set(f["project_id"] for f in feedbacks))
    projects_info = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        for pid in project_ids[:10]:
            row = await db.execute_fetchall(
                "SELECT id, title, theme, mood FROM projects WHERE id=?", (pid,))
            if row:
                projects_info.append(dict(row[0]))

    improvements = await analyze_and_improve(feedbacks, projects_info)

    async with aiosqlite.connect(DB_PATH) as db:
        ids = [f["id"] for f in feedbacks]
        await db.execute(
            f"UPDATE feedback SET processed=1 WHERE id IN ({','.join('?' * len(ids))})",
            ids)
        await db.commit()

    return {"ok": True, "improvements": improvements}


@router.get("/prompt-history")
async def prompt_history():
    """프롬프트 개선 이력."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT * FROM prompt_improvements ORDER BY created_at DESC LIMIT 50")
    return [dict(r) for r in rows]


@router.post("/rollback")
async def rollback():
    """마지막 프롬프트 개선 되돌리기."""
    from backend.services.feedback_service import rollback_overrides
    if rollback_overrides():
        return {"ok": True, "message": "이전 상태로 롤백 완료"}
    return {"ok": False, "error": "롤백할 이력이 없습니다"}


@router.get("/current-rules")
async def current_rules():
    """현재 적용 중인 피드백 규칙 조회."""
    from backend.services.feedback_service import load_overrides
    return load_overrides()


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
