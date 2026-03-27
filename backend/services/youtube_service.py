"""YouTube Data API v3 — OAuth 2.0 + Shorts 업로드 (httpx 기반, SDK 없음)"""
import asyncio
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
import httpx

from backend.config import settings
from backend.database import DB_PATH

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
YT_API_BASE = "https://www.googleapis.com/youtube/v3"
YT_UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"

SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"


def get_auth_url(state: str = "") -> str:
    """Google OAuth 인증 URL 생성."""
    params = {
        "client_id": settings.youtube_client_id,
        "redirect_uri": settings.youtube_redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    qs = "&".join(f"{k}={httpx.QueryParams({k: v})}" for k, v in params.items())
    # 직접 빌드
    from urllib.parse import urlencode
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


async def exchange_code(code: str) -> dict:
    """인증 코드를 access/refresh 토큰으로 교환."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(GOOGLE_TOKEN_URL, data={
            "code": code,
            "client_id": settings.youtube_client_id,
            "client_secret": settings.youtube_client_secret,
            "redirect_uri": settings.youtube_redirect_uri,
            "grant_type": "authorization_code",
        })
        resp.raise_for_status()
        return resp.json()


async def refresh_access_token(refresh_token: str) -> dict:
    """refresh_token으로 새 access_token 발급."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(GOOGLE_TOKEN_URL, data={
            "refresh_token": refresh_token,
            "client_id": settings.youtube_client_id,
            "client_secret": settings.youtube_client_secret,
            "grant_type": "refresh_token",
        })
        resp.raise_for_status()
        return resp.json()


async def get_channel_info(access_token: str) -> dict:
    """연결된 YouTube 채널 정보 조회."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{YT_API_BASE}/channels", params={
            "part": "snippet",
            "mine": "true",
        }, headers={"Authorization": f"Bearer {access_token}"})
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [])
        if not items:
            raise ValueError("채널을 찾을 수 없습니다")
        ch = items[0]
        return {
            "channel_id": ch["id"],
            "channel_title": ch["snippet"]["title"],
        }


async def ensure_valid_token(account: dict) -> str:
    """토큰 만료 확인 후 필요 시 갱신. 유효한 access_token 반환."""
    expires_at = account.get("token_expires_at")
    if expires_at:
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
        if datetime.utcnow() < expires_at - timedelta(minutes=5):
            return account["access_token"]

    # 토큰 갱신
    token_data = await refresh_access_token(account["refresh_token"])
    new_token = token_data["access_token"]
    new_expires = datetime.utcnow() + timedelta(seconds=token_data.get("expires_in", 3600))

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE platform_accounts SET access_token=?, token_expires_at=?, updated_at=? "
            "WHERE id=?",
            (new_token, new_expires.isoformat(), datetime.utcnow().isoformat(), account["id"])
        )
        await db.commit()

    print(f"[YouTube] 토큰 갱신 완료", file=sys.stderr)
    return new_token


async def upload_shorts(
    access_token: str,
    video_path: str,
    title: str,
    description: str,
    tags: list[str] = None,
) -> dict:
    """YouTube Shorts 업로드 (resumable upload). video_id와 URL 반환."""
    file_path = Path(video_path)
    file_size = file_path.stat().st_size

    # 1) Resumable upload 세션 시작
    metadata = {
        "snippet": {
            "title": title[:100],
            "description": description,
            "tags": tags or [],
            "categoryId": "10",  # Music
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
            "shorts": {"isShort": True},
        },
    }

    async with httpx.AsyncClient(timeout=300) as client:
        init_resp = await client.post(
            YT_UPLOAD_URL,
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Length": str(file_size),
                "X-Upload-Content-Type": "video/mp4",
            },
            content=json.dumps(metadata),
        )
        init_resp.raise_for_status()
        upload_url = init_resp.headers["Location"]

        # 2) 영상 파일 업로드
        with open(file_path, "rb") as f:
            video_data = f.read()

        upload_resp = await client.put(
            upload_url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "video/mp4",
                "Content-Length": str(file_size),
            },
            content=video_data,
        )
        upload_resp.raise_for_status()
        result = upload_resp.json()

    video_id = result["id"]
    url = f"https://youtube.com/shorts/{video_id}"
    print(f"[YouTube] 업로드 완료: {url}", file=sys.stderr)

    # 3) 첫 프레임 썸네일 설정
    try:
        await _set_first_frame_thumbnail(access_token, video_id, video_path)
    except Exception as e:
        print(f"[YouTube] 썸네일 설정 실패 (무시): {e}", file=sys.stderr)

    return {"video_id": video_id, "url": url}


async def _set_first_frame_thumbnail(access_token: str, video_id: str, video_path: str):
    """영상 첫 프레임을 1280x720으로 변환하여 YouTube 썸네일로 설정."""
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        thumb_path = tmp.name

    # 첫 프레임 추출 → 720x1280 (9:16)
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vframes", "1",
        "-vf", "scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280",
        "-q:v", "2",
        thumb_path,
    ]
    result = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"썸네일 추출 실패: {result.stderr[-200:]}")

    thumb_data = Path(thumb_path).read_bytes()
    Path(thumb_path).unlink(missing_ok=True)

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"https://www.googleapis.com/upload/youtube/v3/thumbnails/set",
            params={"videoId": video_id},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "image/jpeg",
            },
            content=thumb_data,
        )
        resp.raise_for_status()
    print(f"[YouTube] 첫 프레임 썸네일 설정 완료 ({len(thumb_data)} bytes, 720x1280)", file=sys.stderr)
