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


async def _notify_slot_pending_db_only(project_id: str, step_no: int, scene_no: int):
    """DB 슬롯만 pending으로 업데이트 (emitter 없을 때)."""
    try:
        slot_key = "clip_slots" if step_no == 4 else "images"
        async with aiosqlite.connect(DB_PATH) as db:
            row = await (await db.execute(
                "SELECT output_data FROM pipeline_steps WHERE project_id=? AND step_no=? ORDER BY id DESC LIMIT 1",
                (project_id, step_no))).fetchone()
            if row and row[0]:
                data = json.loads(row[0])
                slots = data.get(slot_key, [])
                idx = scene_no - 1
                if 0 <= idx < len(slots):
                    slots[idx]["status"] = "pending"
                    slots[idx].pop("url", None)
                    await db.execute(
                        "UPDATE pipeline_steps SET output_data=? WHERE project_id=? AND step_no=? AND id=(SELECT MAX(id) FROM pipeline_steps WHERE project_id=? AND step_no=?)",
                        (json.dumps(data, ensure_ascii=False), project_id, step_no, project_id, step_no))
                    await db.commit()
    except Exception as e:
        print(f"[Regen] DB 슬롯 업데이트 실패: {e}", file=__import__('sys').stderr)


async def _notify_slot_pending(project_id: str, emitter: ProgressEmitter,
                               step_no: int, scene_no: int):
    """재생성 시 DB 슬롯을 pending으로 업데이트 + WebSocket 알림."""
    slot_key = "clip_slots" if step_no == 4 else "images"
    step_label = "클립" if step_no == 4 else "이미지"
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            row = await (await db.execute(
                "SELECT output_data FROM pipeline_steps "
                "WHERE project_id=? AND step_no=? ORDER BY id DESC LIMIT 1",
                (project_id, step_no)
            )).fetchone()
            if row and row[0]:
                data = json.loads(row[0])
                slots = data.get(slot_key, [])
                idx = scene_no - 1
                if 0 <= idx < len(slots):
                    slots[idx]["status"] = "pending"
                    slots[idx].pop("url", None)
                    await db.execute(
                        "UPDATE pipeline_steps SET output_data=? "
                        "WHERE project_id=? AND step_no=? AND id=(SELECT MAX(id) FROM pipeline_steps WHERE project_id=? AND step_no=?)",
                        (json.dumps(data, ensure_ascii=False), project_id, step_no, project_id, step_no))
                    await db.commit()
                # emitter에 변경 알림
                await emitter.update(step_no, "running",
                    f"{step_label} 재생성 대기 중... (장면 {scene_no})", data)
    except Exception as e:
        print(f"[Regen] Step {step_no} 슬롯 업데이트 실패: {e}",
              file=__import__('sys').stderr)

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


@router.post("/abort/{project_id}")
async def abort_pipeline_endpoint(project_id: str):
    """진행 중인 파이프라인 중단."""
    emitter = get_emitter(project_id)
    if not emitter or emitter.done:
        return {"ok": False, "error": "실행 중인 파이프라인이 없습니다."}
    emitter._abort = True
    return {"ok": True, "message": "중단 요청이 전송되었습니다."}


@router.get("/random-theme")
async def random_theme():
    """Gemini로 랜덤 테마/분위기 생성."""
    from backend.services.scheduler_service import _generate_random_theme
    theme, mood = await _generate_random_theme()
    if theme:
        return {"ok": True, "theme": theme, "mood": mood}
    return {"ok": False, "error": "테마 생성 실패"}


@router.post("/{project_id}/regenerate-scene/{scene_no}")
async def regenerate_scene_endpoint(project_id: str, scene_no: int, include_image: bool = True):
    """특정 장면 재생성 — 파일 삭제 후 Step 3/4부터 resume.
    진행 중이면 파일만 삭제 (abort 안 함, 루프가 자동 재생성)."""
    global _running_project_id

    from pathlib import Path
    from backend.utils.file_manager import image_path, clip_path

    # 해당 장면 파일 삭제
    deleted = []
    if include_image:
        img = image_path(project_id, scene_no)
        if img.exists():
            img.unlink()
            deleted.append(f"image scene_{scene_no:02d}")
        from_step = 3  # 이미지부터
    else:
        from_step = 4  # 클립만

    clip = clip_path(project_id, scene_no)
    if clip.exists():
        clip.unlink()
        deleted.append(f"clip_{scene_no:02d}")

    print(f"[Regen] 장면 {scene_no} 삭제: {deleted}", file=__import__('sys').stderr)

    # 파이프라인이 같은 프로젝트에서 실행 중이면 파일만 삭제하고 끝
    # (루프가 해당 장면에 도달하면 파일 없으니 자동 재생성)
    existing_emitter = get_emitter(project_id)
    if _pipeline_lock.locked() and existing_emitter and not existing_emitter.done:
        # DB 슬롯 pending + WebSocket 알림
        await _notify_slot_pending(project_id, existing_emitter, 4, scene_no)
        if include_image:
            await _notify_slot_pending(project_id, existing_emitter, 3, scene_no)

        print(f"[Regen] 파이프라인 실행 중 — 파일만 삭제, 루프에서 자동 재생성 예정",
              file=__import__('sys').stderr)
        return {"ok": True, "project_id": project_id, "scene_no": scene_no,
                "from_step": from_step, "deleted": deleted,
                "queued": True}

    # 파이프라인이 이 프로젝트에서 실행 중이지만 emitter가 없는 경우 (다른 프로젝트 실행 중)
    if _pipeline_lock.locked():
        # DB 슬롯만 업데이트 (WebSocket 없이)
        await _notify_slot_pending_db_only(project_id, 4, scene_no)
        print(f"[Regen] 다른 작품 파이프라인 실행 중 — 파일만 삭제, 대기",
              file=__import__('sys').stderr)
        return {"ok": True, "project_id": project_id, "scene_no": scene_no,
                "from_step": from_step, "deleted": deleted,
                "queued": True}

    # DB 클립 슬롯을 pending으로 업데이트 (프론트 표시용)
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            row = await (await db.execute(
                "SELECT output_data FROM pipeline_steps WHERE project_id=? AND step_no=4 ORDER BY id DESC LIMIT 1",
                (project_id,))).fetchone()
            if row and row[0]:
                data = json.loads(row[0])
                slots = data.get("clip_slots", [])
                idx = scene_no - 1
                if 0 <= idx < len(slots):
                    slots[idx]["status"] = "pending"
                    slots[idx].pop("url", None)
                    await db.execute(
                        "UPDATE pipeline_steps SET output_data=? WHERE project_id=? AND step_no=4 AND id=(SELECT MAX(id) FROM pipeline_steps WHERE project_id=? AND step_no=4)",
                        (json.dumps(data, ensure_ascii=False), project_id, project_id))
                    await db.commit()
    except Exception:
        pass

    # 비디오도 삭제 (Step 5 재합성 필요)
    video_dir = Path(f"storage/projects/{project_id}/video")
    if video_dir.exists():
        import shutil
        shutil.rmtree(str(video_dir), ignore_errors=True)

    # DB 스텝 초기화 — Step 5(영상 합성)만 삭제
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await db.execute("SELECT theme, mood, length FROM projects WHERE id=?", (project_id,))
        row = await row.fetchone()
        if not row:
            return {"ok": False, "error": "프로젝트를 찾을 수 없습니다."}
        theme, mood, length = row["theme"], row["mood"], row["length"] or "short"

        await db.execute(
            "DELETE FROM pipeline_steps WHERE project_id=? AND step_no=5",
            (project_id,))
        await db.commit()

    _running_project_id = project_id
    emitter = ProgressEmitter(project_id)
    register_emitter(project_id, emitter)

    async def _run():
        async with _pipeline_lock:
            try:
                from backend.services.pipeline_service import run_pipeline
                await run_pipeline(project_id, theme, mood, emitter,
                                   resume_from=from_step, length=length,
                                   skip_clean=True)
            finally:
                _clear_running()

    asyncio.create_task(_run())
    return {"ok": True, "project_id": project_id, "scene_no": scene_no,
            "from_step": from_step, "deleted": deleted,
            "queued": False}


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
    step5_only = (from_step == 5)

    if not step5_only and _pipeline_lock.locked():
        return {"ok": False, "error": "다른 작업이 진행 중입니다. 완료 후 다시 시도해주세요."}

    # reset=true면 해당 스텝의 캐시 파일 삭제
    if reset and from_step > 0:
        from backend.services.pipeline_service import _clean_step_files
        await _clean_step_files(project_id, from_step, reset=True)

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

    emitter = ProgressEmitter(project_id)
    register_emitter(project_id, emitter)

    if step5_only:
        # Step 5(FFmpeg 합성)는 GPU 미사용 → lock 없이 바로 실행
        async def _run_no_lock():
            try:
                await run_pipeline(
                    project_id, theme, mood, emitter,
                    resume_from=resume_from, length=length
                )
            finally:
                if _running_project_id == project_id:
                    _clear_running()

        asyncio.create_task(_run_no_lock())
    else:
        _running_project_id = project_id

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
