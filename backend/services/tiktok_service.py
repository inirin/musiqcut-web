"""TikTok Content Posting API — OAuth 2.0 + 동영상 업로드 (chunked)"""
import asyncio
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
import httpx

from backend.config import settings
from backend.database import DB_PATH

TT_AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TT_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
TT_API = "https://open.tiktokapis.com/v2"

SCOPES = "video.upload,video.publish"
CHUNK_SIZE = 10 * 1024 * 1024  # 10MB


def get_auth_url(state: str = "tiktok") -> str:
    """TikTok OAuth 인증 URL 생성."""
    from urllib.parse import urlencode
    params = {
        "client_key": settings.tiktok_client_key,
        "redirect_uri": settings.tiktok_redirect_uri,
        "scope": SCOPES,
        "response_type": "code",
        "state": state,
    }
    return f"{TT_AUTH_URL}?{urlencode(params)}"


async def exchange_code(code: str) -> dict:
    """인증 코드 → 토큰 교환."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(TT_TOKEN_URL, data={
            "client_key": settings.tiktok_client_key,
            "client_secret": settings.tiktok_client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": settings.tiktok_redirect_uri,
        }, headers={"Content-Type": "application/x-www-form-urlencoded"})
        resp.raise_for_status()
        data = resp.json()
        if "error" in data and data.get("error", {}).get("code") != "ok":
            raise RuntimeError(f"TikTok 토큰 교환 실패: {data}")
        return {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token", ""),
            "expires_in": data.get("expires_in", 86400),
            "open_id": data.get("open_id", ""),
        }


async def refresh_access_token(refresh_token: str) -> dict:
    """refresh_token으로 새 access_token 발급."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(TT_TOKEN_URL, data={
            "client_key": settings.tiktok_client_key,
            "client_secret": settings.tiktok_client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }, headers={"Content-Type": "application/x-www-form-urlencoded"})
        resp.raise_for_status()
        data = resp.json()
        return {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token", refresh_token),
            "expires_in": data.get("expires_in", 86400),
        }


async def get_user_info(access_token: str) -> dict:
    """TikTok 사용자 정보 조회."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{TT_API}/user/info/", params={
            "fields": "display_name,avatar_url",
        }, headers={"Authorization": f"Bearer {access_token}"})
        resp.raise_for_status()
        data = resp.json().get("data", {}).get("user", {})
        return {
            "display_name": data.get("display_name", "TikTok User"),
        }


async def ensure_valid_token(account: dict) -> str:
    """토큰 만료 확인 후 필요 시 갱신."""
    expires_at = account.get("token_expires_at")
    if expires_at:
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
        if datetime.utcnow() < expires_at - timedelta(minutes=30):
            return account["access_token"]

    if not account.get("refresh_token"):
        return account["access_token"]

    token_data = await refresh_access_token(account["refresh_token"])
    new_token = token_data["access_token"]
    new_refresh = token_data.get("refresh_token", account["refresh_token"])
    new_expires = datetime.utcnow() + timedelta(seconds=token_data.get("expires_in", 86400))

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE platform_accounts SET access_token=?, refresh_token=?, "
            "token_expires_at=?, updated_at=? WHERE id=?",
            (new_token, new_refresh, new_expires.isoformat(),
             datetime.utcnow().isoformat(), account["id"])
        )
        await db.commit()

    print("[TikTok] 토큰 갱신 완료", file=sys.stderr)
    return new_token


async def upload_video(
    access_token: str,
    video_path: str,
    title: str,
) -> dict:
    """TikTok 동영상 업로드 (Direct Post, chunked upload)."""
    file_path = Path(video_path)
    file_size = file_path.stat().st_size
    total_chunks = max(1, math.ceil(file_size / CHUNK_SIZE))

    async with httpx.AsyncClient(timeout=300) as client:
        # 1) 업로드 초기화
        init_resp = await client.post(
            f"{TT_API}/post/publish/video/init/",
            json={
                "post_info": {
                    "title": title[:150],
                    "privacy_level": "PUBLIC_TO_EVERYONE",
                    "disable_duet": False,
                    "disable_comment": False,
                    "disable_stitch": False,
                },
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": file_size,
                    "chunk_size": CHUNK_SIZE,
                    "total_chunk_count": total_chunks,
                },
            },
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
        )
        if init_resp.status_code != 200:
            print(f"[TikTok] 초기화 실패: {init_resp.status_code} {init_resp.text}", file=sys.stderr)
            init_resp.raise_for_status()
        init_data = init_resp.json()

        if init_data.get("error", {}).get("code") != "ok":
            raise RuntimeError(f"TikTok 초기화 실패: {init_data}")

        publish_id = init_data["data"]["publish_id"]
        upload_url = init_data["data"]["upload_url"]

        # 2) 청크 업로드
        with open(file_path, "rb") as f:
            for chunk_idx in range(total_chunks):
                chunk_data = f.read(CHUNK_SIZE)
                start = chunk_idx * CHUNK_SIZE
                end = start + len(chunk_data) - 1

                chunk_resp = await client.put(
                    upload_url,
                    headers={
                        "Content-Type": "video/mp4",
                        "Content-Range": f"bytes {start}-{end}/{file_size}",
                        "Content-Length": str(len(chunk_data)),
                    },
                    content=chunk_data,
                )
                chunk_resp.raise_for_status()

        # 3) 게시 상태 폴링
        video_id = None
        for _ in range(60):  # 최대 5분
            await asyncio.sleep(5)
            status_resp = await client.post(
                f"{TT_API}/post/publish/status/fetch/",
                json={"publish_id": publish_id},
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json; charset=UTF-8",
                },
            )
            status_resp.raise_for_status()
            status_data = status_resp.json()
            status = status_data.get("data", {}).get("status")

            if status == "PUBLISH_COMPLETE":
                post_ids = status_data["data"].get("publicaly_available_post_id", [])
                video_id = post_ids[0] if post_ids else publish_id
                break
            elif status == "FAILED":
                reason = status_data.get("data", {}).get("fail_reason", "알 수 없음")
                raise RuntimeError(f"TikTok 게시 실패: {reason}")
        else:
            raise RuntimeError("TikTok 처리 시간 초과")

    url = f"https://www.tiktok.com/@/video/{video_id}"
    print(f"[TikTok] 업로드 완료: {url}", file=sys.stderr)
    return {"video_id": video_id, "url": url}
