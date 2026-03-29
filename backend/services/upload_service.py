"""업로드 오케스트레이션 — 플랫폼별 업로드 관리"""
import json
import sys
from datetime import datetime

import aiosqlite

from backend.database import DB_PATH


async def get_account(platform: str = "youtube") -> dict | None:
    """연동된 플랫폼 계정 조회."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT * FROM platform_accounts WHERE platform=?", (platform,)
        )).fetchone()
        return dict(row) if row else None


async def get_upload_status(project_id: str) -> list[dict]:
    """프로젝트의 플랫폼별 업로드 상태 조회."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM uploads WHERE project_id=? ORDER BY created_at DESC",
            (project_id,)
        )).fetchall()
        return [dict(r) for r in rows]


async def get_upload_history(limit: int = 50) -> list[dict]:
    """전체 업로드 이력 (프로젝트 정보 포함)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT u.*, p.title as project_title, p.theme, p.source "
            "FROM uploads u JOIN projects p ON u.project_id = p.id "
            "ORDER BY u.created_at DESC LIMIT ?",
            (limit,)
        )).fetchall()
        return [dict(r) for r in rows]


def generate_metadata(title: str, theme: str, project_id: str = None) -> dict:
    """업로드용 제목/설명/태그 자동 생성."""
    # 테마에서 설명 부분 추출
    desc_part = theme
    for sep in [' - ', ' — ', ' – ']:
        if sep in theme:
            desc_part = theme.split(sep, 1)[1].strip()
            break

    upload_title = f"{title}\n{desc_part}"[:100]
    # YouTube는 제목 줄바꿈 미지원 → 하이픈 구분자 버전도 제공
    upload_title_flat = f"{title} - {desc_part}"[:100]

    # lyrics.json에서 해시태그 로드
    hashtags = []
    if project_id:
        try:
            from backend.utils.file_manager import lyrics_path
            lp = lyrics_path(project_id)
            if lp.exists():
                data = json.loads(lp.read_text(encoding="utf-8"))
                hashtags = data.get("hashtags", [])
        except Exception:
            pass

    # 해시태그가 없으면 기본 태그 사용
    if not hashtags:
        hashtags = ["#뮤직컷", "#AI뮤직비디오", "#Shorts", "#AI", "#MusicVideo"]

    hashtag_line = " ".join(hashtags)
    # 인스타/틱톡용 캡션: 제목 + 테마설명 + 해시태그
    caption = f"{title}\n{desc_part}\n \n{hashtag_line}"
    # YouTube tags (# 제거)
    tags = [h.lstrip("#") for h in hashtags]
    return {"title": upload_title, "title_flat": upload_title_flat, "caption": caption, "tags": tags}


async def create_and_execute_upload(
    project_id: str,
    platform: str = "youtube",
    custom_title: str = None,
    reupload: bool = False,
) -> dict:
    """업로드 생성 + 실행."""
    from backend.services import youtube_service, instagram_service, tiktok_service

    # 계정 확인
    account = await get_account(platform)
    if not account:
        return {"ok": False, "error": f"{platform} 계정이 연결되지 않았습니다"}

    # 프로젝트 정보
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        project = await (await db.execute(
            "SELECT * FROM projects WHERE id=?", (project_id,)
        )).fetchone()
        if not project:
            return {"ok": False, "error": "프로젝트를 찾을 수 없습니다"}
        if project["status"] != "done":
            return {"ok": False, "error": "완료된 프로젝트만 업로드할 수 있습니다"}
        if not project["video_path"]:
            return {"ok": False, "error": "영상 파일이 없습니다"}

    # 이미 업로드된 건 확인 (재업로드 시 기존 기록 삭제)
    async with aiosqlite.connect(DB_PATH) as db:
        existing = await (await db.execute(
            "SELECT id FROM uploads WHERE project_id=? AND platform=? AND status='done'",
            (project_id, platform)
        )).fetchone()
        if existing:
            if reupload:
                await db.execute("DELETE FROM uploads WHERE id=?", (existing[0],))
                await db.commit()
                print(f"[Upload] 재업로드: 기존 {platform} 기록 삭제", file=sys.stderr)
            else:
                return {"ok": False, "error": f"이미 {platform}에 업로드된 작품입니다"}

    # 메타데이터 생성
    meta = generate_metadata(project["title"], project["theme"], project_id)
    # 플랫폼별 메타데이터
    title = custom_title or meta["title_flat"]
    caption = meta["caption"]
    tags = meta["tags"]

    # uploads 레코드 생성
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO uploads (project_id, platform, title, description, tags, status) "
            "VALUES (?, ?, ?, ?, ?, 'uploading')",
            (project_id, platform, title, caption, json.dumps(tags))
        )
        upload_id = cursor.lastrowid
        await db.commit()

    # 업로드 실행 (플랫폼별 분기)
    try:
        if platform == "youtube":
            access_token = await youtube_service.ensure_valid_token(account)
            result = await youtube_service.upload_shorts(
                access_token, project["video_path"], title, tags
            )
        elif platform == "instagram":
            access_token = await instagram_service.ensure_valid_token(account)
            result = await instagram_service.upload_reels(
                access_token, account["channel_id"],
                project["video_path"], caption
            )
        elif platform == "tiktok":
            access_token = await tiktok_service.ensure_valid_token(account)
            result = await tiktok_service.upload_video(
                access_token, project["video_path"], caption[:150]
            )
        else:
            raise ValueError(f"지원하지 않는 플랫폼: {platform}")

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE uploads SET status='done', platform_video_id=?, platform_url=?, "
                "uploaded_at=? WHERE id=?",
                (result["video_id"], result["url"], datetime.utcnow().isoformat(), upload_id)
            )
            await db.commit()

        print(f"[Upload] {platform} 업로드 완료: {result['url']}", file=sys.stderr)
        return {"ok": True, "url": result["url"], "video_id": result["video_id"]}

    except Exception as e:
        error_msg = str(e)[:500]
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE uploads SET status='failed', error_msg=? WHERE id=?",
                (error_msg, upload_id)
            )
            await db.commit()
        print(f"[Upload] {platform} 업로드 실패: {error_msg}", file=sys.stderr)
        return {"ok": False, "error": error_msg}


async def auto_upload_if_configured(project_id: str):
    """자동 생성 작품 완료 시 활성화된 플랫폼으로 자동 업로드."""
    print(f"[Upload] 자동 업로드 확인: {project_id[:8]}", file=sys.stderr)
    import traceback as _tb
    for platform in ("youtube", "instagram", "tiktok"):
        print(f"[Upload] 자동 업로드 루프: {platform}", file=sys.stderr)
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            config = await (await db.execute(
                "SELECT enabled FROM auto_schedule WHERE schedule_type=?",
                (f"upload_{platform}",)
            )).fetchone()
            if not config or not config["enabled"]:
                continue

        account = await get_account(platform)
        if account:
            print(f"[Upload] {platform} 자동 업로드 실행", file=sys.stderr)
            try:
                result = await create_and_execute_upload(project_id, platform)
                print(f"[Upload] {platform} 자동 업로드 결과: {result.get('ok')} {result.get('url', result.get('error', ''))}", file=sys.stderr)
            except Exception as e:
                print(f"[Upload] {platform} 자동 업로드 에러: {e}", file=sys.stderr)
                _tb.print_exc(file=sys.stderr)
            # 플랫폼 간 rate limit 방지
            print(f"[Upload] {platform} 완료, 10초 대기...", file=sys.stderr)
            await asyncio.sleep(10)
            print(f"[Upload] {platform} 대기 완료", file=sys.stderr)
