"""Instagram Graph API — OAuth 2.0 + Reels 업로드 (resumable upload)"""
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
import httpx

from backend.config import settings
from backend.database import DB_PATH

IG_AUTH_URL = "https://api.instagram.com/oauth/authorize"
IG_TOKEN_URL = "https://api.instagram.com/oauth/access_token"
GRAPH_API = "https://graph.instagram.com"

SCOPES = "instagram_business_basic,instagram_business_content_publish,instagram_business_manage_messages"


def get_auth_url(state: str = "") -> str:
    """Instagram Direct Login OAuth URL 생성."""
    from urllib.parse import urlencode
    params = {
        "client_id": settings.instagram_app_id,
        "redirect_uri": settings.instagram_redirect_uri,
        "scope": SCOPES,
        "response_type": "code",
        "state": state,
        "enable_fb_login": "0",
        "force_authentication": "1",
    }
    return f"{IG_AUTH_URL}?{urlencode(params)}"


async def exchange_code(code: str) -> dict:
    """인증 코드 → 단기 토큰 → 장기 토큰 교환 (Instagram Direct Login)."""
    async with httpx.AsyncClient() as client:
        # 단기 토큰 (POST)
        resp = await client.post(IG_TOKEN_URL, data={
            "client_id": settings.instagram_app_id,
            "client_secret": settings.instagram_app_secret,
            "grant_type": "authorization_code",
            "redirect_uri": settings.instagram_redirect_uri,
            "code": code,
        })
        resp.raise_for_status()
        short_data = resp.json()
        short_token = short_data["access_token"]
        user_id = str(short_data.get("user_id", ""))

        # 장기 토큰 교환
        resp2 = await client.get(f"{GRAPH_API}/access_token", params={
            "grant_type": "ig_exchange_token",
            "client_secret": settings.instagram_app_secret,
            "access_token": short_token,
        })
        resp2.raise_for_status()
        data = resp2.json()
        return {
            "access_token": data["access_token"],
            "user_id": user_id,
            "expires_in": data.get("expires_in", 5184000),  # ~60일
        }


async def get_ig_account(access_token: str, user_id: str = "") -> dict:
    """Instagram Direct Login으로 인증된 계정 정보 조회."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{GRAPH_API}/me", params={
            "fields": "user_id,username",
            "access_token": access_token,
        })
        resp.raise_for_status()
        data = resp.json()
        ig_id = str(data.get("user_id", user_id))
        username = data.get("username", ig_id)

        return {
            "ig_user_id": ig_id,
            "username": username,
        }


async def refresh_access_token(access_token: str) -> dict:
    """장기 토큰 갱신 (만료 전 갱신 가능, Instagram Direct Login)."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{GRAPH_API}/refresh_access_token", params={
            "grant_type": "ig_refresh_token",
            "access_token": access_token,
        })
        resp.raise_for_status()
        data = resp.json()
        return {
            "access_token": data["access_token"],
            "expires_in": data.get("expires_in", 5184000),
        }


async def ensure_valid_token(account: dict) -> str:
    """토큰 만료 확인 후 필요 시 갱신."""
    expires_at = account.get("token_expires_at")
    if expires_at:
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
        if datetime.utcnow() < expires_at - timedelta(days=7):
            return account["access_token"]

    token_data = await refresh_access_token(account["access_token"])
    new_token = token_data["access_token"]
    new_expires = datetime.utcnow() + timedelta(seconds=token_data["expires_in"])

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE platform_accounts SET access_token=?, token_expires_at=?, updated_at=? "
            "WHERE id=?",
            (new_token, new_expires.isoformat(), datetime.utcnow().isoformat(), account["id"])
        )
        await db.commit()

    print("[Instagram] 토큰 갱신 완료", file=sys.stderr)
    return new_token


async def upload_reels(
    access_token: str,
    ig_user_id: str,
    video_path: str,
    caption: str,
) -> dict:
    """Instagram Reels 업로드 (video_url 방식)."""
    # 임시 공개 URL 생성 (Cloudflare Access Bypass 경로, 10분 유효)
    from backend.routers.upload import create_temp_video_url
    video_url = create_temp_video_url(video_path)

    async with httpx.AsyncClient(timeout=300) as client:
        # 1) 미디어 컨테이너 생성 (video_url 방식)
        resp = await client.post(f"{GRAPH_API}/{ig_user_id}/media", data={
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption,
            "share_to_feed": "true",
            "is_made_with_ai": "true",
            "access_token": access_token,
        })
        if resp.status_code != 200:
            print(f"[Instagram] 컨테이너 생성 실패: {resp.status_code} {resp.text}", file=sys.stderr)
            resp.raise_for_status()
        creation_id = resp.json()["id"]
        print(f"[Instagram] 컨테이너 생성: {creation_id}, video_url: {video_url}", file=sys.stderr)

        # 2) 처리 상태 폴링
        for _ in range(60):  # 최대 5분
            await asyncio.sleep(5)
            status_resp = await client.get(f"{GRAPH_API}/{creation_id}", params={
                "fields": "status_code,status",
                "access_token": access_token,
            })
            status_resp.raise_for_status()
            status = status_resp.json()
            code = status.get("status_code")
            print(f"[Instagram] 처리 상태: {code}", file=sys.stderr)
            if code == "FINISHED":
                break
            elif code == "ERROR":
                raise RuntimeError(f"Instagram 처리 실패: {status.get('status')}")
        else:
            raise RuntimeError("Instagram 처리 시간 초과")

        # 3) 퍼블리시
        pub_resp = await client.post(f"{GRAPH_API}/{ig_user_id}/media_publish", data={
            "creation_id": creation_id,
            "access_token": access_token,
        })
        if pub_resp.status_code != 200:
            print(f"[Instagram] 퍼블리시 실패: {pub_resp.status_code} {pub_resp.text}", file=sys.stderr)
            pub_resp.raise_for_status()
        media_id = pub_resp.json()["id"]

    url = f"https://www.instagram.com/reel/{media_id}/"
    print(f"[Instagram] 업로드 완료: {url}", file=sys.stderr)
    return {"video_id": media_id, "url": url}
