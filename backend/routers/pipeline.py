import uuid
import asyncio
import json
import aiosqlite
from datetime import datetime
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from backend.database import DB_PATH
from backend.models.project import PipelineRunRequest
from backend.utils.progress import (
    ProgressEmitter, get_emitter, register_emitter,
)
from backend.services.pipeline_service import run_pipeline

router = APIRouter()

# 동시 실행 방지 — API rate limit / SQLite 동시 쓰기 보호
_pipeline_lock = asyncio.Lock()
_running_project_id: str | None = None


def _clear_running():
    global _running_project_id
    _running_project_id = None


@router.get("/status")
async def pipeline_status():
    """현재 파이프라인 실행 상태 조회."""
    return {"running": _pipeline_lock.locked(), "project_id": _running_project_id}


@router.get("/random-theme")
async def random_theme():
    """Gemini로 랜덤 테마/분위기 생성."""
    from backend.services.scheduler_service import _generate_random_theme
    theme, mood = await _generate_random_theme()
    if theme:
        return {"ok": True, "theme": theme, "mood": mood}
    return {"ok": False, "error": "테마 생성 실패"}


@router.post("/run")
async def run_pipeline_endpoint(body: PipelineRunRequest):
    global _running_project_id
    if _pipeline_lock.locked():
        return {"ok": False, "error": "다른 작업이 진행 중입니다. 완료 후 다시 시도해주세요."}

    project_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO projects (id, theme, mood, length, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (project_id, body.theme, body.mood, body.length, "pending", now, now)
        )
        await db.commit()

    _running_project_id = project_id
    emitter = ProgressEmitter(project_id)
    register_emitter(project_id, emitter)

    async def _run_with_lock():
        async with _pipeline_lock:
            try:
                await run_pipeline(
                    project_id, body.theme, body.mood, emitter,
                    length=body.length
                )
            finally:
                _clear_running()

    asyncio.create_task(_run_with_lock())
    return {"ok": True, "project_id": project_id}


@router.post("/resume/{project_id}")
async def resume_pipeline_endpoint(project_id: str, from_step: int = 0, reset: bool = False):
    """특정 STEP부터 재시도. reset=true면 해당 스텝 결과물 삭제 후 처음부터."""
    global _running_project_id
    if _pipeline_lock.locked():
        return {"ok": False, "error": "다른 작업이 진행 중입니다. 완료 후 다시 시도해주세요."}

    # reset=true면 해당 스텝의 캐시 파일 삭제
    if reset and from_step > 0:
        from backend.services.pipeline_service import _clean_step_files
        await _clean_step_files(project_id, from_step)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT theme, mood, length FROM projects WHERE id=?", (project_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return {"ok": False, "error": "프로젝트를 찾을 수 없습니다."}
        theme, mood, length = row["theme"], row["mood"], row["length"] or "short"

        if from_step > 0:
            await db.execute(
                "DELETE FROM pipeline_steps WHERE project_id=? AND step_no>=?",
                (project_id, from_step)
            )
            # 해당 스텝 이후 피드백도 삭제
            await db.execute(
                "DELETE FROM feedback WHERE project_id=? AND step_no>=?",
                (project_id, from_step)
            )
            await db.commit()
            resume_from = from_step
        else:
            cursor = await db.execute(
                "SELECT MAX(step_no) as max_step FROM pipeline_steps "
                "WHERE project_id=? AND status='done'",
                (project_id,)
            )
            result = await cursor.fetchone()
            resume_from = (result["max_step"] or 0) + 1

    _running_project_id = project_id
    emitter = ProgressEmitter(project_id)
    register_emitter(project_id, emitter)

    async def _run_with_lock():
        async with _pipeline_lock:
            try:
                await run_pipeline(
                    project_id, theme, mood, emitter,
                    resume_from=resume_from, length=length
                )
            finally:
                _clear_running()

    asyncio.create_task(_run_with_lock())
    return {"ok": True, "project_id": project_id}


@router.websocket("/ws/{project_id}")
async def pipeline_ws(ws: WebSocket, project_id: str):
    """파이프라인 진행 상황 WebSocket — 실시간 이벤트 수신."""
    await ws.accept()

    emitter = get_emitter(project_id)
    if emitter is None:
        # 파이프라인이 이미 끝났거나 없는 경우
        await ws.send_text(json.dumps({
            "type": "info", "message": "파이프라인이 실행 중이 아닙니다."
        }))
        await ws.close()
        return

    await emitter.register(ws)
    try:
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive_text(), timeout=60.0)
                # 클라이언트 ping에 pong 응답
                if msg == 'ping':
                    await ws.send_text(json.dumps({"type": "pong"}))
            except asyncio.TimeoutError:
                # 60초 무응답 → ping 전송
                try:
                    await ws.send_text(json.dumps({"type": "ping"}))
                except Exception:
                    break
            # emitter가 완료됐으면 종료
            if emitter.done:
                break
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        emitter.unregister(ws)
