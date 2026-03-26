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


def _fetch_trends() -> list[str]:
    """Google Trends + 네이버 뉴스 RSS에서 트렌드/뉴스 수집."""
    import urllib.request as _ur
    import xml.etree.ElementTree as _ET

    all_items = []

    # 1) Google Trends 한국
    try:
        ns = {"ht": "https://trends.google.com/trending/rss"}
        url = "https://trends.google.co.kr/trending/rss?geo=KR"
        req = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = _ur.urlopen(req, timeout=10)
        root = _ET.fromstring(resp.read())
        for item in root.findall(".//item")[:5]:
            title = item.find("title")
            if title is None or not title.text:
                continue
            keyword = title.text.strip()
            news_titles = []
            for ni in item.findall("ht:news_item", ns):
                nt = ni.find("ht:news_item_title", ns)
                if nt is not None and nt.text:
                    news_titles.append(nt.text.strip())
            if news_titles:
                headlines = " / ".join(nt[:50] for nt in news_titles[:3])
                all_items.append(f"[트렌드] {keyword}: {headlines}")
            else:
                all_items.append(f"[트렌드] {keyword}")
    except Exception:
        pass

    # 2) 한국 주요 언론사 RSS (각 2개씩)
    news_feeds = [
        ("동아일보", "https://rss.donga.com/total.xml"),
        ("한겨레", "https://www.hani.co.kr/rss/"),
        ("조선일보", "https://www.chosun.com/arc/outboundfeeds/rss/?outputType=xml"),
        ("연합뉴스", "https://www.yna.co.kr/rss/news.xml"),
    ]
    for label, url in news_feeds:
        try:
            req = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            resp = _ur.urlopen(req, timeout=5)
            root = _ET.fromstring(resp.read())
            for item in root.findall(".//item")[:2]:
                title = item.find("title")
                if title is not None and title.text:
                    all_items.append(f"[{label}] {title.text.strip()[:80]}")
        except Exception:
            pass

    if all_items:
        print(f"[Scheduler] 트렌드+뉴스 ({len(all_items)}개): {all_items[0][:40]}...",
              file=sys.stderr)
    return all_items


async def _generate_random_theme() -> tuple[str, str]:
    """Google Trends + Gemini (Google Search grounding) 기반 테마 생성."""
    try:
        from backend.utils.gemini_client import get_api_keys
        from google import genai
        from google.genai import types
        import json as _json

        # 실시간 트렌드 가져오기
        trends = await asyncio.to_thread(_fetch_trends)
        random.shuffle(trends)  # 순서 섞어서 상위 편중 방지
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

STEP 1: 아래 트렌드/뉴스를 읽고, Google 검색으로 각 항목의 **인과관계**를 파악하세요.
- 뉴스 자체(관객 수, 주가 등)가 아니라, 그 뉴스가 화제인 **근본 이유/소재/스토리**가 무엇인지 파악
- 예: "왕사남 천만 돌파" → 진짜 트렌드는 "단종과 엄흥도의 비극적 사극 이야기"
- 예: "AI 압축 기술 반도체 충격" → 진짜 트렌드는 "AI가 기존 산업 질서를 뒤엎는 혁신의 시대"
- 예: "학폭 재판 노쇼" → 진짜 트렌드는 "학교 폭력 피해자가 정의를 찾지 못하는 현실"

STEP 2: **대중이 가장 공감할 수 있는** 소재/스토리를 골라 뮤지컬 애니메이션 테마로 만드세요.
(선택 기준: 이야기의 풍부함 + 대중적 공감도 + 감정적 울림. 마니아적/전문적 소재보다 누구나 공감하는 소재 우선)

핵심 규칙:
1. 트렌드 1개를 선택하고 inspired_by에 명시
2. **고유명사(실존 인물/기업/브랜드) 사용 금지**
3. 뉴스의 세부 숫자(주가 %, 금액 등)에 집착하지 말고, **그 뉴스가 말하는 큰 흐름/트렌드/현상**을 테마에 담아라
4. 제목만 읽어도 "아, 요즘 화제인 그 이야기!" 하고 떠올릴 수 있어야 함
5. **장르는 트렌드의 톤에 맞게 선택** — 뉴스의 감정/성격이 장르를 결정해야 함:
   - 부조리/비리/위선 → 풍자극, 블랙코미디
   - 감동 실화/극복 → 성장 드라마, 뮤지컬 드라마
   - 기술혁신/과학 → SF, 판타지
   - 갈등/전쟁/외교 → 서사극, 스릴러
   - 연예/문화/축제 → 로맨스, 코미디, 뮤지컬
   - 사건사고/재난 → 비극, 호러, 다크 판타지
   - 스포츠/경쟁 → 스포츠 드라마, 성장극

좋은 예시 (트렌드/현상이 느껴짐):
- "AI 압축 기술이 반도체 시장 흔듦" → "3비트 마법사의 등장 - 새로운 기억술이 칩 왕국의 지도를 하룻밤에 바꿔버렸다"
  (AI 압축/반도체 충격 → 터보퀀트 트렌드 연상)
- "독도 영유권 주장, 교과서 왜곡" → "작은 섬의 큰 거짓말 - 이웃 나라가 교과서에서 역사를 지우는 날"
  (교과서 왜곡/섬 영유권 → 독도 트렌드 연상)
- "풍력발전기 화재, 80미터 추락" → "바닷바람 풍차의 비명 - 친환경 에너지의 상징이 불타오르던 밤"
  (풍차/화재/친환경 → 풍력 화재 트렌드 연상)
- "학폭 재판 노쇼" → "빈 피고인석의 노래 - 법정에 나타나지 않는 가해자를 기다리는 피해자의 이야기"
  (노쇼/법정/학폭 → 학폭 재판 트렌드 연상)

나쁜 예시 (추상적이거나 숫자에 집착):
- "기억 압축의 멜로디" — 어떤 트렌드인지 전혀 짐작 불가
- "3.71% 폭락의 노래" — 숫자만 있고 트렌드가 안 보임
- "춤추는 알고리즘의 파도" — 멋있지만 아무도 뉴스를 떠올릴 수 없음

실시간 트렌드:
{trends_text}

최근 생성된 테마 (중복 금지):
{avoid}

반드시 아래 JSON 형식으로만 응답하세요:
{{"theme": "테마 제목 - 한 줄 설명 (한국어)", "mood": "분위기 (한국어)", "inspired_by": "위 트렌드 목록에서 선택한 항목을 원본 그대로 복사 + 검색으로 파악한 맥락 추가. 형식: '원본 트렌드 텍스트 /// 맥락: 인물이면 성별/나이대/외형/대표작, 사건이면 장소/시기/핵심 상황'"}}"""

        # Google Search grounding으로 트렌드 맥락 파악 + 테마 생성
        keys = get_api_keys()
        if not keys:
            raise ValueError("Gemini API 키 미설정")
        client = genai.Client(api_key=keys[0])
        response = await asyncio.to_thread(
            client.models.generate_content,
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
            )
        )
        text = response.text.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        data = _json.loads(text)
        theme = data.get("theme", "").strip()
        mood = data.get("mood", "auto").strip()
        inspired = data.get("inspired_by", "").strip()
        if theme:
            if inspired:
                mood = f"{mood} [트렌드 힌트: {inspired}]"
            print(f"[Scheduler] Gemini 테마 생성: {theme[:60]}",
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
        try:
            await _run_auto_generation()
        except Exception as e:
            print(f"[Scheduler] resume 중 오류 (계속 진행): {e}", file=sys.stderr)
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

        # 루프 처음으로 → last_created_at 기준 interval 체크
        await asyncio.sleep(60)  # 1분 후 다시 체크


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
