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
from backend.utils.theme_pool import THEME_POOL, MOOD_POOL  # fallback용

_gen_task = None
_gen_enabled = False


async def _get_schedule_config(schedule_type: str = "generation") -> dict:
    """DB에서 스케줄 설정 로드."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT * FROM auto_schedule WHERE schedule_type=?", (schedule_type,))
        if rows:
            return dict(rows[0])
    return {"schedule_type": schedule_type, "enabled": 0, "interval_hours": 2.0}


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


async def _get_last_auto_created_at() -> str | None:
    """마지막 자동 생성 작품의 created_at 조회."""
    async with aiosqlite.connect(DB_PATH) as db:
        row = await db.execute_fetchall(
            "SELECT created_at FROM projects WHERE source='auto' "
            "ORDER BY created_at DESC LIMIT 1")
        if row:
            return row[0][0]
    return None


def _fetch_google_trends() -> list[str]:
    """Google Trends RSS에서 한국 실시간 트렌드 키워드를 가져옴."""
    try:
        import urllib.request as _ur
        import xml.etree.ElementTree as _ET

        url = "https://trends.google.co.kr/trending/rss?geo=KR"
        req = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = _ur.urlopen(req, timeout=10)
        root = _ET.fromstring(resp.read())
        items = root.findall(".//item")
        trends = [item.find("title").text for item in items[:10] if item.find("title") is not None]
        if trends:
            print(f"[Scheduler] Google Trends: {', '.join(trends[:5])}...", file=sys.stderr)
        return trends
    except Exception as e:
        print(f"[Scheduler] Google Trends 조회 실패: {e}", file=sys.stderr)
        return []


async def _generate_random_theme() -> tuple[str, str]:
    """Google Trends + Gemini로 트렌드 기반 테마 생성."""
    try:
        from backend.utils.gemini_client import gemini_generate
        import json as _json

        # 실시간 트렌드 가져오기
        trends = await asyncio.to_thread(_fetch_google_trends)
        trends_text = "\n".join(f"- {t}" for t in trends) if trends else "(조회 실패)"

        # 기존 작품 테마를 가져와서 중복 방지
        existing = []
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                rows = await db.execute_fetchall(
                    "SELECT theme FROM projects ORDER BY created_at DESC LIMIT 10")
                existing = [r[0][:30] for r in rows]
        except Exception:
            pass

        avoid = "\n".join(f"- {t}" for t in existing) if existing else "(없음)"

        prompt = f"""당신은 뮤지컬 애니메이션 숏폼 콘텐츠 기획자입니다.
아래 실시간 트렌드 키워드 중 하나를 골라, 그 키워드와 **자연스럽게 연결되는** 작품 테마를 만드세요.

규칙:
- 트렌드 키워드에서 1개를 선택하고, inspired_by에 어떤 키워드인지 명시
- 키워드와의 연결고리가 누가 봐도 느껴져야 함 (너무 추상적 변환 금지)
- 키워드 자체를 제목에 넣어도 되고, 관련 인물/사건/감정을 테마로 풀어도 됨
- 예: "스페이스X" → "화성행 편도 티켓 - 돌아올 수 없는 우주비행사의 마지막 메시지"
- 예: "김효진" → "빛나는 무대 뒤의 그림자 - 배우가 조명이 꺼진 뒤 홀로 남겨질 때"
- 예: "블리자드" → "눈보라 속의 등대지기 - 세상에서 가장 외로운 겨울밤 당직"

실시간 트렌드 키워드:
{trends_text}

장르는 자유: 역사, 판타지, SF, 로맨스, 코미디, 풍자, 호러, 다크 판타지, 성장드라마, 서사극, 슬라이스 오브 라이프, 모험, 미스터리, 뮤지컬, 문학 각색, 신화 재해석 등

최근 생성된 테마 (중복 피할 것):
{avoid}

반드시 아래 JSON 형식으로만 응답하세요:
{{"theme": "테마 제목 - 한 줄 설명 (한국어)", "mood": "분위기 (한국어)", "inspired_by": "선택한 트렌드 키워드"}}"""

        response = await gemini_generate(
            model="gemini-2.5-flash",
            contents=prompt
        )
        text = response.text.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        data = _json.loads(text)
        theme = data.get("theme", "").strip()
        mood = data.get("mood", "auto").strip()
        inspired = data.get("inspired_by", "")
        if theme:
            print(f"[Scheduler] Gemini 테마 생성: {theme[:40]} / {mood}"
                  f" (트렌드: {inspired})" if inspired else "",
                  file=sys.stderr)
            return theme, mood
    except Exception as e:
        print(f"[Scheduler] Gemini 테마 생성 실패, fallback: {e}", file=sys.stderr)
    return "", ""


async def _find_interrupted_project() -> dict | None:
    """재시도할 실패 작품 찾기 (자동 생성 작품 중 가장 최근 failed 1개)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT id, theme, mood, length FROM projects "
            "WHERE status='failed' AND source='auto' "
            "AND (error_msg IS NULL OR error_msg NOT LIKE '%보컬 감지 실패%') "
            "ORDER BY updated_at DESC LIMIT 1")
        if rows:
            return dict(rows[0])
    return None


async def _run_auto_generation():
    """중단된 프로젝트 resume 또는 랜덤 테마로 새 작품 생성."""
    from backend.services.pipeline_service import run_pipeline
    from backend.utils.progress import ProgressEmitter, register_emitter

    # 서버 재시작으로 중단된 프로젝트가 있으면 resume
    interrupted = await _find_interrupted_project()
    if interrupted:
        project_id = interrupted["id"]
        theme = interrupted["theme"]
        mood = interrupted["mood"]
        length = interrupted.get("length", "short")

        # 마지막 완료 스텝 다음부터 재시작
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT MAX(step_no) as max_step FROM pipeline_steps "
                "WHERE project_id=? AND status='done'", (project_id,))
            result = await cursor.fetchone()
            resume_from = (result["max_step"] or 0) + 1
            # 상태 복원
            await db.execute(
                "UPDATE projects SET status='pending', error_msg=NULL WHERE id=?",
                (project_id,))
            await db.commit()

        emitter = ProgressEmitter(project_id)
        register_emitter(project_id, emitter)
        print(f"[Scheduler] 중단된 작품 resume: step {resume_from}부터 (id={project_id[:8]})",
              file=sys.stderr)

        success = False
        fail_reason = ""
        try:
            await run_pipeline(project_id, theme, mood, emitter,
                               resume_from=resume_from, length=length)
            success = True
        except Exception as e:
            fail_reason = str(e)[:100]
            print(f"[Scheduler] resume 실패: {e}", file=sys.stderr)
    else:
        # 새 작품 생성 — Gemini로 테마 생성, 실패 시 고정 풀 fallback
        theme, mood = await _generate_random_theme()
        if not theme:
            theme = random.choice(THEME_POOL)
            mood = random.choice(MOOD_POOL)
        project_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO projects (id, theme, mood, length, status, source, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (project_id, theme, mood, "short", "pending", "auto", now, now))
            await db.commit()

        emitter = ProgressEmitter(project_id)
        register_emitter(project_id, emitter)
        print(f"[Scheduler] 자동 생성 시작: {theme[:30]}... (id={project_id[:8]})",
              file=sys.stderr)

        success = False
        fail_reason = ""
        try:
            await run_pipeline(project_id, theme, mood, emitter, length="short")
            success = True
        except Exception as e:
            fail_reason = str(e)[:100]
            print(f"[Scheduler] 자동 생성 실패: {e}", file=sys.stderr)

    # 결과 기록
    now = datetime.utcnow().isoformat()
    if success:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE auto_schedule SET last_success_at=? "
                "WHERE schedule_type='generation'", (now,))
            await db.commit()
    else:
        await _record_failure("파이프라인 오류 발생")


async def _record_failure(reason: str = ""):
    """자동 생성 실패/스킵 시간 기록."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE auto_schedule SET last_failure_at=?, last_failure_reason=? "
            "WHERE schedule_type='generation'",
            (datetime.utcnow().isoformat(), reason or None))
        await db.commit()
    if reason:
        print(f"[Scheduler] 실패 기록: {reason}", file=sys.stderr)


_STARTUP_GRACE_SEC = 60  # 서버 시작 후 1분간 자동 생성 보류 (재시작 시 즉시 생성 방지)


async def _generation_loop():
    """작품 자동 생성 루프."""
    global _gen_enabled
    # 중단된 작품이 있으면 즉시 resume (grace period 없이)
    interrupted = await _find_interrupted_project()
    if interrupted:
        print(f"[Scheduler] 중단된 작품 발견, 즉시 resume (id={interrupted['id'][:8]})",
              file=sys.stderr)
        await _run_auto_generation()
    else:
        print(f"[Scheduler] 서버 시작 대기 ({_STARTUP_GRACE_SEC}초)...", file=sys.stderr)
        await asyncio.sleep(_STARTUP_GRACE_SEC)
    while _gen_enabled:
        config = await _get_schedule_config("generation")
        if not config.get("enabled"):
            _gen_enabled = False
            break
        interval = config.get("interval_hours", 2.0) * 3600

        # 마지막 자동 생성 이후 간격 미충족 시 남은 시간만 대기
        last_created = await _get_last_auto_created_at()
        if last_created:
            try:
                last_dt = datetime.fromisoformat(last_created).replace(tzinfo=timezone.utc)
                elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
                remaining = interval - elapsed
                if remaining > 0:
                    print(f"[Scheduler] 최근 생성 {elapsed/60:.0f}분 전 → {remaining/60:.0f}분 후 생성",
                          file=sys.stderr)
                    await asyncio.sleep(remaining)
                    continue
            except Exception:
                pass

        from backend.routers.pipeline import _pipeline_lock
        if _pipeline_lock.locked():
            print("[Scheduler] 파이프라인 실행 중, 60초 후 재시도", file=sys.stderr)
            await _record_failure("다른 작품 생성 중")
            await asyncio.sleep(60)
            continue

        try:
            await _run_auto_generation()
        except Exception as e:
            print(f"[Scheduler] 작품 생성 오류: {e}", file=sys.stderr)

        print(f"[Scheduler] 다음 생성: {interval/3600:.1f}시간 후", file=sys.stderr)
        await asyncio.sleep(interval)


def start_scheduler(schedule_type: str = "generation"):
    global _gen_task, _gen_enabled
    if schedule_type != "generation":
        return
    if _gen_task and not _gen_task.done():
        return
    _gen_enabled = True
    _gen_task = asyncio.create_task(_generation_loop())
    print("[Scheduler] 작품 생성 스케줄러 시작", file=sys.stderr)


def stop_scheduler(schedule_type: str = "generation"):
    global _gen_enabled
    if schedule_type != "generation":
        return
    _gen_enabled = False
    print("[Scheduler] 작품 생성 스케줄러 중지", file=sys.stderr)
