import uuid
import shutil
from datetime import datetime
from fastapi import APIRouter, HTTPException
import aiosqlite
from backend.database import DB_PATH
from backend.models.project import ProjectCreate
from backend.utils.file_manager import project_dir

router = APIRouter()


@router.get("")
async def list_projects():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM projects ORDER BY created_at DESC LIMIT 50"
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


@router.get("/{project_id}")
async def get_project(project_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM projects WHERE id=?", (project_id,)
        )
        row = await cursor.fetchone()
    if not row:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다")
    result = dict(row)
    # 오디오 파일이 있으면 곡 길이 계산
    from backend.utils.file_manager import music_path
    audio = music_path(project_id)
    if audio.exists():
        try:
            from backend.services.suno_service import measure_audio_duration
            dur = await measure_audio_duration(str(audio))
            result["actual_duration"] = round(dur, 1)
        except Exception:
            pass
    return result


@router.get("/{project_id}/steps")
async def get_project_steps(project_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM pipeline_steps WHERE project_id=? ORDER BY step_no",
            (project_id,)
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


@router.post("")
async def create_project(body: ProjectCreate):
    project_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO projects (id, theme, mood, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (project_id, body.theme, body.mood, "pending", now, now)
        )
        await db.commit()
    return {"id": project_id}


@router.delete("/{project_id}")
async def delete_project(project_id: str):
    # 파일 삭제
    from pathlib import Path
    pdir = Path(project_dir(project_id))
    if pdir.exists():
        shutil.rmtree(pdir, ignore_errors=True)
    # DB 삭제 (피드백 포함)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM feedback WHERE project_id=?",
                         (project_id,))
        await db.execute("DELETE FROM pipeline_steps WHERE project_id=?",
                         (project_id,))
        await db.execute("DELETE FROM projects WHERE id=?", (project_id,))
        await db.commit()
    return {"ok": True}
