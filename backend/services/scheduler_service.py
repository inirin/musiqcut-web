"""자동 작품 생성 스케줄링 + 피드백 분석 스케줄"""
import asyncio
import json
import random
import sys
import uuid
from datetime import datetime, timezone
import aiosqlite
from backend.database import DB_PATH

# 랜덤 테마 풀
THEME_POOL = [
    # 역사
    "고대 이집트 파라오의 마지막 여행 - 사후 세계로 떠나는 파라오의 영혼",
    "삼국시대 백제의 마지막 공주 - 나라가 멸망하는 날 홀로 남겨진 공주",
    "바이킹 전사의 귀환 - 긴 항해를 마치고 고향으로 돌아오는 전사",
    "실크로드 상인의 노래 - 사막을 건너며 고향을 그리워하는 상인",
    "로마 검투사의 마지막 경기 - 자유를 얻기 위한 마지막 싸움",
    # 판타지/SF
    "잠든 용의 꿈 - 천년 동안 잠든 용이 꾸는 아름다운 꿈",
    "AI가 처음 감정을 느낀 날 - 로봇이 석양을 보며 처음 슬픔을 느끼다",
    "시간여행자의 편지 - 미래에서 과거의 자신에게 보내는 편지",
    "해저도시의 음유시인 - 바다 속 도시에서 노래하는 마지막 시인",
    "별을 먹는 고래 - 우주를 떠다니며 별을 삼키는 거대한 고래",
    # 감성/일상
    "비 오는 날 버스 정류장 - 우산 없이 비를 맞으며 누군가를 기다리는 사람",
    "할아버지의 낡은 기타 - 먼지 쌓인 기타를 발견하고 연주하는 손자",
    "첫눈 오는 날 고백 - 첫눈이 내리는 밤, 용기를 내어 고백하는 순간",
    "새벽 4시 편의점 - 밤새 일한 뒤 편의점에서 따뜻한 음식을 먹는 순간",
    "이사 가는 날 - 추억이 담긴 빈 방을 마지막으로 돌아보는 순간",
    # 자연/동물
    "북극곰의 마지막 빙하 - 녹아가는 빙하 위에 홀로 남은 북극곰",
    "봄을 기다리는 벚나무 - 긴 겨울을 견디고 드디어 꽃을 피우는 나무",
    "철새의 긴 여행 - 수천km를 날아 돌아오는 철새의 여정",
]

MOOD_POOL = [
    "auto",
    "비극적이고 장엄한",
    "따뜻하고 잔잔한",
    "신비롭고 몽환적인",
    "설레고 로맨틱한",
    "고독하고 쓸쓸한",
    "에너지 넘치는",
    "잔잔하면서 슬픈",
]

_gen_task = None
_gen_enabled = False
_fb_task = None
_fb_enabled = False


async def _get_schedule_config(schedule_type: str = "generation") -> dict:
    """DB에서 스케줄 설정 로드."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT * FROM auto_schedule WHERE schedule_type=?", (schedule_type,))
        if rows:
            return dict(rows[0])
    return {"schedule_type": schedule_type, "enabled": 0, "interval_hours": 2.0, "last_run_at": None}


async def save_schedule_config(enabled: bool, interval_hours: float, schedule_type: str = "generation"):
    """스케줄 설정 저장."""
    async with aiosqlite.connect(DB_PATH) as db:
        existing = await db.execute_fetchall(
            "SELECT id FROM auto_schedule WHERE schedule_type=?", (schedule_type,))
        if existing:
            await db.execute(
                "UPDATE auto_schedule SET enabled=?, interval_hours=?, updated_at=? WHERE schedule_type=?",
                (int(enabled), interval_hours, datetime.utcnow().isoformat(), schedule_type))
        else:
            await db.execute(
                "INSERT INTO auto_schedule (schedule_type, enabled, interval_hours) VALUES (?,?,?)",
                (schedule_type, int(enabled), interval_hours))
        await db.commit()


async def _run_auto_generation():
    """랜덤 테마로 작품 생성."""
    from backend.services.pipeline_service import run_pipeline
    from backend.utils.progress import ProgressEmitter, register_emitter

    theme = random.choice(THEME_POOL)
    mood = random.choice(MOOD_POOL)
    project_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO projects (id, theme, mood, length, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (project_id, theme, mood, "short", "pending", now, now))
        await db.commit()

    emitter = ProgressEmitter(project_id)
    register_emitter(project_id, emitter)

    print(f"[Scheduler] 자동 생성 시작: {theme[:30]}... (id={project_id[:8]})",
          file=sys.stderr)

    try:
        await run_pipeline(project_id, theme, mood, emitter, length="short")
    except Exception as e:
        print(f"[Scheduler] 자동 생성 실패: {e}", file=sys.stderr)

    # 마지막 실행 시간 업데이트
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE auto_schedule SET last_run_at=?",
            (datetime.utcnow().isoformat(),))
        await db.commit()


async def _run_auto_feedback_process():
    """미처리 피드백이 충분하면 자동 분석."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        count = await db.execute_fetchall(
            "SELECT COUNT(*) as cnt FROM feedback WHERE processed=0")
        cnt = count[0]["cnt"] if count else 0

    if cnt < 3:
        print(f"[Scheduler] 미처리 피드백 {cnt}개 < 3개, 분석 스킵", file=sys.stderr)
        return

    from backend.routers.feedback import process_feedback
    print(f"[Scheduler] 미처리 피드백 {cnt}개 → 자동 분석 실행", file=sys.stderr)
    await process_feedback()


async def _generation_loop():
    """작품 자동 생성 루프."""
    global _gen_enabled
    while _gen_enabled:
        config = await _get_schedule_config("generation")
        if not config.get("enabled"):
            _gen_enabled = False
            break
        interval = config.get("interval_hours", 2.0) * 3600

        # 마지막 실행 이후 간격 미충족 시 남은 시간만 대기
        last_run = config.get("last_run_at")
        if last_run:
            from datetime import datetime, timezone
            try:
                last_dt = datetime.fromisoformat(last_run).replace(tzinfo=timezone.utc)
                elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
                remaining = interval - elapsed
                if remaining > 0:
                    print(f"[Scheduler] 최근 실행 {elapsed/60:.0f}분 전 → {remaining/60:.0f}분 후 생성",
                          file=sys.stderr)
                    await asyncio.sleep(remaining)
                    continue
            except Exception:
                pass

        from backend.routers.pipeline import _pipeline_lock
        if _pipeline_lock.locked():
            await asyncio.sleep(60)
            continue

        # DB에 running 프로젝트가 있으면 스킵 (서버 재시작 후 lock 초기화 대비)
        async with aiosqlite.connect(DB_PATH) as db:
            rows = await db.execute_fetchall(
                "SELECT id FROM projects WHERE status='running' LIMIT 1")
            if rows:
                print(f"[Scheduler] DB에 running 프로젝트 존재 ({rows[0][0][:8]}...), 스킵",
                      file=sys.stderr)
                await asyncio.sleep(60)
                continue

        try:
            await _run_auto_generation()
        except Exception as e:
            print(f"[Scheduler] 작품 생성 오류: {e}", file=sys.stderr)

        print(f"[Scheduler] 다음 생성: {interval/3600:.1f}시간 후", file=sys.stderr)
        await asyncio.sleep(interval)


async def _feedback_loop():
    """피드백 자동 분석 루프."""
    global _fb_enabled
    while _fb_enabled:
        config = await _get_schedule_config("feedback")
        if not config.get("enabled"):
            _fb_enabled = False
            break
        interval = config.get("interval_hours", 12.0) * 3600

        last_run = config.get("last_run_at")
        if last_run:
            from datetime import datetime, timezone
            try:
                last_dt = datetime.fromisoformat(last_run).replace(tzinfo=timezone.utc)
                elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
                remaining = interval - elapsed
                if remaining > 0:
                    print(f"[Scheduler] 최근 분석 {elapsed/60:.0f}분 전 → {remaining/60:.0f}분 후 실행",
                          file=sys.stderr)
                    await asyncio.sleep(remaining)
                    continue
            except Exception:
                pass

        try:
            await _run_auto_feedback_process()
        except Exception as e:
            print(f"[Scheduler] 피드백 분석 오류: {e}", file=sys.stderr)

        print(f"[Scheduler] 다음 분석: {interval/3600:.1f}시간 후", file=sys.stderr)
        await asyncio.sleep(interval)


def start_scheduler(schedule_type: str = "generation"):
    global _gen_task, _gen_enabled, _fb_task, _fb_enabled
    if schedule_type == "generation":
        if _gen_task and not _gen_task.done():
            return
        _gen_enabled = True
        _gen_task = asyncio.create_task(_generation_loop())
        print("[Scheduler] 작품 생성 스케줄러 시작", file=sys.stderr)
    elif schedule_type == "feedback":
        if _fb_task and not _fb_task.done():
            return
        _fb_enabled = True
        _fb_task = asyncio.create_task(_feedback_loop())
        print("[Scheduler] 피드백 분석 스케줄러 시작", file=sys.stderr)


def stop_scheduler(schedule_type: str = "generation"):
    global _gen_enabled, _fb_enabled
    if schedule_type == "generation":
        _gen_enabled = False
        print("[Scheduler] 작품 생성 스케줄러 중지", file=sys.stderr)
    elif schedule_type == "feedback":
        _fb_enabled = False
        print("[Scheduler] 피드백 분석 스케줄러 중지", file=sys.stderr)
