"""업로드 API — OAuth 연동 + 수동/자동 업로드"""
import asyncio
import sys
from datetime import datetime, timedelta

import aiosqlite
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from backend.database import DB_PATH
from backend.services import youtube_service, instagram_service, tiktok_service, upload_service

router = APIRouter()

# ── 임시 공개 비디오 URL (Instagram 업로드용) ──────────
import secrets
from pathlib import Path
from fastapi.responses import FileResponse

_temp_video_tokens: dict[str, tuple[str, float]] = {}  # token → (file_path, expires_ts)


@router.get("/public-video/{token}")
async def serve_public_video(token: str):
    """임시 토큰으로 비디오 파일 서빙 (Cloudflare Access Bypass 경로)."""
    import time
    from fastapi.responses import JSONResponse
    entry = _temp_video_tokens.get(token)
    if not entry:
        return JSONResponse({"error": "Invalid or expired token"}, status_code=404)
    file_path, expires_ts = entry
    if time.time() > expires_ts:
        _temp_video_tokens.pop(token, None)
        return JSONResponse({"error": "Token expired"}, status_code=410)
    if not Path(file_path).exists():
        return JSONResponse({"error": "File not found"}, status_code=404)
    return FileResponse(file_path, media_type="video/mp4")


def create_temp_video_url(video_path: str, ttl_sec: int = 600) -> str:
    """임시 공개 URL 생성 (기본 10분 유효)."""
    import time
    token = secrets.token_urlsafe(32)
    _temp_video_tokens[token] = (video_path, time.time() + ttl_sec)
    return f"https://musiqcut.com/api/upload/public-video/{token}"


@router.post("/create-temp-url")
async def create_temp_url_endpoint(body: dict):
    """외부 프로세스에서 임시 공개 URL 생성 요청 (웹서버 메모리에 토큰 등록)."""
    video_path = body.get("video_path", "")
    if not video_path or not Path(video_path).exists():
        return {"error": "Invalid video path"}
    url = create_temp_video_url(video_path)
    return {"url": url}


# ── OAuth ────────────────────────────────────────

@router.get("/youtube/auth-url")
async def youtube_auth_url():
    """YouTube OAuth 인증 URL 반환."""
    from backend.config import settings
    if not settings.youtube_client_id:
        return {"ok": False, "error": "YouTube Client ID가 설정되지 않았습니다"}
    url = youtube_service.get_auth_url()
    return {"ok": True, "url": url}


@router.get("/youtube/callback")
async def youtube_callback(code: str = "", error: str = ""):
    """YouTube OAuth 콜백 — 토큰 교환 후 팝업 닫기."""
    if error or not code:
        return HTMLResponse(f"""<html><body><script>
            window.opener && window.opener.postMessage({{type:'youtube-auth',ok:false,error:'{error or "인증 취소"}'}}, '*');
            window.close();
        </script></body></html>""")

    try:
        # 토큰 교환
        token_data = await youtube_service.exchange_code(code)
        access_token = token_data["access_token"]
        refresh_token = token_data.get("refresh_token", "")
        expires_in = token_data.get("expires_in", 3600)
        expires_at = (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat()

        # 채널 정보 조회
        channel = await youtube_service.get_channel_info(access_token)

        # DB 저장 (기존 계정 있으면 업데이트)
        async with aiosqlite.connect(DB_PATH) as db:
            existing = await (await db.execute(
                "SELECT id FROM platform_accounts WHERE platform='youtube'"
            )).fetchone()
            if existing:
                await db.execute(
                    "UPDATE platform_accounts SET channel_id=?, channel_title=?, "
                    "access_token=?, refresh_token=?, token_expires_at=?, "
                    "scope=?, updated_at=? WHERE platform='youtube'",
                    (channel["channel_id"], channel["channel_title"],
                     access_token, refresh_token, expires_at,
                     youtube_service.SCOPES, datetime.utcnow().isoformat())
                )
            else:
                await db.execute(
                    "INSERT INTO platform_accounts "
                    "(platform, channel_id, channel_title, access_token, refresh_token, "
                    "token_expires_at, scope) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("youtube", channel["channel_id"], channel["channel_title"],
                     access_token, refresh_token, expires_at, youtube_service.SCOPES)
                )
            await db.commit()

        # auto_schedule에 upload 타입 없으면 생성
        async with aiosqlite.connect(DB_PATH) as db:
            row = await (await db.execute(
                "SELECT id FROM auto_schedule WHERE schedule_type='upload'"
            )).fetchone()
            if not row:
                await db.execute(
                    "INSERT INTO auto_schedule (schedule_type, enabled) VALUES ('upload', 0)"
                )
                await db.commit()

        print(f"[YouTube] OAuth 연동 완료: {channel['channel_title']}", file=sys.stderr)

        ch_title = channel["channel_title"].replace("'", "\\'")
        return HTMLResponse(f"""<html><body>
        <p style="font-family:sans-serif;text-align:center;margin-top:40px">
            YouTube 연결 완료: <b>{ch_title}</b><br>
            <small>이 창은 자동으로 닫힙니다...</small>
        </p>
        <script>
            if (window.opener) {{
                window.opener.postMessage({{type:'youtube-auth',ok:true,channel:'{ch_title}'}}, '*');
                setTimeout(() => window.close(), 500);
            }} else {{
                // 같은 창에서 열린 경우 → 메인 페이지로 이동
                setTimeout(() => location.href = '/#settings', 1500);
            }}
        </script></body></html>""")

    except Exception as e:
        print(f"[YouTube] OAuth 실패: {e}", file=sys.stderr)
        return HTMLResponse(f"""<html><body>
        <p style="font-family:sans-serif;text-align:center;margin-top:40px;color:red">
            YouTube 연결 실패<br><small>이 창은 자동으로 닫힙니다...</small>
        </p>
        <script>
            if (window.opener) {{
                window.opener.postMessage({{type:'youtube-auth',ok:false,error:'인증 실패'}}, '*');
                setTimeout(() => window.close(), 500);
            }} else {{
                setTimeout(() => location.href = '/#settings', 1500);
            }}
        </script></body></html>""")


# ── 계정 관리 ────────────────────────────────────

@router.get("/youtube/account")
@router.get("/account")
async def get_account():
    """연동된 YouTube 계정 정보."""
    account = await upload_service.get_account("youtube")
    if not account:
        return {"connected": False}
    return {
        "connected": True,
        "platform": "youtube",
        "channel_id": account["channel_id"],
        "channel_title": account["channel_title"],
        "connected_at": account["connected_at"],
    }


@router.delete("/account")
async def disconnect_account():
    """YouTube 계정 연결 해제."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM platform_accounts WHERE platform='youtube'")
        await db.commit()
    return {"ok": True}


# ── 업로드 ────────────────────────────────────────

@router.post("/{project_id}/upload")
async def upload_project(project_id: str):
    """수동 업로드 (비동기 실행)."""
    # 즉시 응답 + 백그라운드 실행
    account = await upload_service.get_account("youtube")
    if not account:
        return {"ok": False, "error": "YouTube 계정이 연결되지 않았습니다"}

    async def _run():
        await upload_service.create_and_execute_upload(project_id, "youtube")

    asyncio.create_task(_run())
    return {"ok": True, "message": "업로드를 시작합니다"}


@router.get("/{project_id}/status")
async def upload_status(project_id: str):
    """프로젝트의 업로드 상태 조회."""
    uploads = await upload_service.get_upload_status(project_id)
    return {"uploads": uploads}


# ── 이력 ──────────────────────────────────────────

@router.get("/history")
async def upload_history(limit: int = 50):
    """전체 업로드 이력."""
    history = await upload_service.get_upload_history(limit)
    return {"uploads": history}


@router.delete("/record/{upload_id}")
async def delete_upload_record(upload_id: int):
    """업로드 이력 삭제."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM uploads WHERE id=?", (upload_id,))
        await db.commit()
    return {"ok": True}


# ── 자동 업로드 설정 ──────────────────────────────

@router.get("/auto-upload")
async def get_auto_upload():
    """플랫폼별 자동 업로드 설정 조회."""
    result = {}
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        for platform in ("youtube", "instagram", "tiktok"):
            stype = f"upload_{platform}"
            row = await (await db.execute(
                "SELECT enabled FROM auto_schedule WHERE schedule_type=?", (stype,)
            )).fetchone()
            result[platform] = bool(row["enabled"]) if row else False
    return result


@router.post("/auto-upload/{platform}")
async def toggle_platform_auto_upload(platform: str, enabled: bool = True):
    """플랫폼별 자동 업로드 ON/OFF."""
    stype = f"upload_{platform}"
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute(
            "SELECT id FROM auto_schedule WHERE schedule_type=?", (stype,)
        )).fetchone()
        if row:
            await db.execute(
                "UPDATE auto_schedule SET enabled=?, updated_at=? WHERE schedule_type=?",
                (1 if enabled else 0, datetime.utcnow().isoformat(), stype)
            )
        else:
            await db.execute(
                "INSERT INTO auto_schedule (schedule_type, enabled) VALUES (?, ?)",
                (stype, 1 if enabled else 0)
            )
        await db.commit()
    return {"ok": True, "platform": platform, "enabled": enabled}


# ── Instagram OAuth ──────────────────────────────

@router.get("/instagram/auth-url")
async def instagram_auth_url():
    """Instagram OAuth 인증 URL 반환."""
    from backend.config import settings
    if not settings.instagram_app_id:
        return {"ok": False, "error": "Instagram App ID가 설정되지 않았습니다"}
    return {"ok": True, "url": instagram_service.get_auth_url()}


@router.get("/instagram/callback")
async def instagram_callback(code: str = "", error: str = ""):
    """Instagram OAuth 콜백."""
    if error or not code:
        return _oauth_result_html("instagram", False, error or "인증 취소")

    try:
        token_data = await instagram_service.exchange_code(code)
        access_token = token_data["access_token"]
        expires_at = (datetime.utcnow() + timedelta(seconds=token_data["expires_in"])).isoformat()

        ig_account = await instagram_service.get_ig_account(access_token, token_data.get("user_id", ""))

        async with aiosqlite.connect(DB_PATH) as db:
            existing = await (await db.execute(
                "SELECT id FROM platform_accounts WHERE platform='instagram'"
            )).fetchone()
            if existing:
                await db.execute(
                    "UPDATE platform_accounts SET channel_id=?, channel_title=?, "
                    "access_token=?, token_expires_at=?, scope=?, updated_at=? "
                    "WHERE platform='instagram'",
                    (ig_account["ig_user_id"], ig_account["username"],
                     access_token, expires_at,
                     instagram_service.SCOPES, datetime.utcnow().isoformat())
                )
            else:
                await db.execute(
                    "INSERT INTO platform_accounts "
                    "(platform, channel_id, channel_title, access_token, token_expires_at, scope) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("instagram", ig_account["ig_user_id"], ig_account["username"],
                     access_token, expires_at, instagram_service.SCOPES)
                )
            await db.commit()

        print(f"[Instagram] OAuth 연동 완료: @{ig_account['username']}", file=sys.stderr)
        return _oauth_result_html("instagram", True, ig_account["username"])

    except Exception as e:
        print(f"[Instagram] OAuth 실패: {e}", file=sys.stderr)
        return _oauth_result_html("instagram", False, "인증 실패")


@router.get("/instagram/account")
async def instagram_account():
    """Instagram 계정 정보."""
    account = await upload_service.get_account("instagram")
    if not account:
        return {"connected": False}
    return {
        "connected": True,
        "platform": "instagram",
        "channel_id": account["channel_id"],
        "channel_title": account["channel_title"],
        "connected_at": account["connected_at"],
    }


@router.delete("/instagram/account")
async def disconnect_instagram():
    """Instagram 연결 해제."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM platform_accounts WHERE platform='instagram'")
        await db.commit()
    return {"ok": True}


# ── TikTok OAuth ─────────────────────────────────

@router.get("/tiktok/auth-url")
async def tiktok_auth_url():
    """TikTok OAuth 인증 URL 반환."""
    from backend.config import settings
    if not settings.tiktok_client_key:
        return {"ok": False, "error": "TikTok Client Key가 설정되지 않았습니다"}
    return {"ok": True, "url": tiktok_service.get_auth_url()}


@router.get("/tiktok/callback")
async def tiktok_callback(code: str = "", error: str = ""):
    """TikTok OAuth 콜백."""
    if error or not code:
        return _oauth_result_html("tiktok", False, error or "인증 취소")

    try:
        token_data = await tiktok_service.exchange_code(code)
        access_token = token_data["access_token"]
        refresh_token = token_data.get("refresh_token", "")
        expires_at = (datetime.utcnow() + timedelta(seconds=token_data["expires_in"])).isoformat()

        user_info = await tiktok_service.get_user_info(access_token)

        async with aiosqlite.connect(DB_PATH) as db:
            existing = await (await db.execute(
                "SELECT id FROM platform_accounts WHERE platform='tiktok'"
            )).fetchone()
            if existing:
                await db.execute(
                    "UPDATE platform_accounts SET channel_id=?, channel_title=?, "
                    "access_token=?, refresh_token=?, token_expires_at=?, scope=?, updated_at=? "
                    "WHERE platform='tiktok'",
                    (token_data.get("open_id", ""), user_info["display_name"],
                     access_token, refresh_token, expires_at,
                     tiktok_service.SCOPES, datetime.utcnow().isoformat())
                )
            else:
                await db.execute(
                    "INSERT INTO platform_accounts "
                    "(platform, channel_id, channel_title, access_token, refresh_token, "
                    "token_expires_at, scope) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("tiktok", token_data.get("open_id", ""), user_info["display_name"],
                     access_token, refresh_token, expires_at, tiktok_service.SCOPES)
                )
            await db.commit()

        print(f"[TikTok] OAuth 연동 완료: {user_info['display_name']}", file=sys.stderr)
        return _oauth_result_html("tiktok", True, user_info["display_name"])

    except Exception as e:
        print(f"[TikTok] OAuth 실패: {e}", file=sys.stderr)
        return _oauth_result_html("tiktok", False, "인증 실패")


@router.get("/tiktok/account")
async def tiktok_account():
    """TikTok 계정 정보."""
    account = await upload_service.get_account("tiktok")
    if not account:
        return {"connected": False}
    return {
        "connected": True,
        "platform": "tiktok",
        "channel_id": account["channel_id"],
        "channel_title": account["channel_title"],
        "connected_at": account["connected_at"],
    }


@router.delete("/tiktok/account")
async def disconnect_tiktok():
    """TikTok 연결 해제."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM platform_accounts WHERE platform='tiktok'")
        await db.commit()
    return {"ok": True}


# ── 플랫폼별 업로드 ─────────────────────────────

@router.post("/{project_id}/upload/{platform}")
async def upload_to_platform(project_id: str, platform: str, reupload: bool = False):
    """특정 플랫폼에 업로드. reupload=true면 기존 기록 삭제 후 재업로드."""
    if platform not in ("youtube", "instagram", "tiktok"):
        return {"ok": False, "error": f"지원하지 않는 플랫폼: {platform}"}

    account = await upload_service.get_account(platform)
    if not account:
        return {"ok": False, "error": f"{platform} 계정이 연결되지 않았습니다"}

    async def _run():
        await upload_service.create_and_execute_upload(project_id, platform, reupload=reupload)

    asyncio.create_task(_run())
    return {"ok": True, "message": f"{platform} {'재' if reupload else ''}업로드를 시작합니다"}


# ── 전체 플랫폼 업로드 ──────────────────────────

@router.post("/{project_id}/upload-all")
async def upload_to_all(project_id: str):
    """연결된 모든 플랫폼에 업로드."""
    results = {}
    for platform in ("youtube", "instagram", "tiktok"):
        account = await upload_service.get_account(platform)
        if account:
            asyncio.create_task(
                upload_service.create_and_execute_upload(project_id, platform)
            )
            results[platform] = "시작됨"
        else:
            results[platform] = "미연결"
    return {"ok": True, "results": results}


# ── 공통 헬퍼 ────────────────────────────────────

def _oauth_result_html(platform: str, ok: bool, message: str) -> HTMLResponse:
    """OAuth 콜백 결과 HTML (팝업 닫기 또는 리디렉트)."""
    platform_name = {"youtube": "YouTube", "instagram": "Instagram", "tiktok": "TikTok"}.get(platform, platform)
    msg_safe = message.replace("'", "\\'")
    color = "inherit" if ok else "red"
    label = "연결 완료" if ok else "연결 실패"

    return HTMLResponse(f"""<html><body>
    <p style="font-family:sans-serif;text-align:center;margin-top:40px;color:{color}">
        {platform_name} {label}: <b>{message}</b><br>
        <small>이 창은 자동으로 닫힙니다...</small>
    </p>
    <script>
        if (window.opener) {{
            window.opener.postMessage({{type:'{platform}-auth',ok:{'true' if ok else 'false'},name:'{msg_safe}'}}, '*');
            setTimeout(() => window.close(), 500);
        }} else {{
            setTimeout(() => location.href = '/#settings', 1500);
        }}
    </script></body></html>""")
