"""Instagram Graph API — OAuth 2.0 + Reels 업로드 (resumable upload)"""
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
import httpx

from backend.config import settings
from backend.database import DB_PATH

META_AUTH_URL = "https://www.facebook.com/v22.0/dialog/oauth"
META_TOKEN_URL = "https://graph.facebook.com/v22.0/oauth/access_token"
GRAPH_API = "https://graph.facebook.com/v22.0"

SCOPES = "instagram_basic,instagram_content_publish,pages_read_engagement"


def get_auth_url(state: str = "") -> str:
    """Meta OAuth 인증 URL 생성."""
    from urllib.parse import urlencode
    params = {
        "client_id": settings.instagram_app_id,
        "redirect_uri": settings.instagram_redirect_uri,
        "scope": SCOPES,
        "response_type": "code",
        "state": state,
    }
    return f"{META_AUTH_URL}?{urlencode(params)}"


async def exchange_code(code: str) -> dict:
    """인증 코드 → 단기 토큰 → 장기 토큰 교환."""
    async with httpx.AsyncClient() as client:
        # 단기 토큰
        resp = await client.get(META_TOKEN_URL, params={
            "client_id": settings.instagram_app_id,
            "client_secret": settings.instagram_app_secret,
            "redirect_uri": settings.instagram_redirect_uri,
            "code": code,
        })
        resp.raise_for_status()
        short_token = resp.json()["access_token"]

        # 장기 토큰 교환
        resp2 = await client.get(f"{GRAPH_API}/oauth/access_token", params={
            "grant_type": "fb_exchange_token",
            "client_id": settings.instagram_app_id,
            "client_secret": settings.instagram_app_secret,
            "fb_exchange_token": short_token,
        })
        resp2.raise_for_status()
        data = resp2.json()
        return {
            "access_token": data["access_token"],
            "expires_in": data.get("expires_in", 5184000),  # ~60일
        }


async def get_ig_account(access_token: str) -> dict:
    """Facebook 페이지에 연결된 Instagram 비즈니스 계정 조회."""
    async with httpx.AsyncClient() as client:
        # 내 페이지 목록
        resp = await client.get(f"{GRAPH_API}/me/accounts", params={
            "access_token": access_token,
        })
        resp.raise_for_status()
        pages = resp.json().get("data", [])
        if not pages:
            raise ValueError("연결된 Facebook 페이지가 없습니다")

        # 첫 페이지의 IG 계정
        page = pages[0]
        page_token = page["access_token"]
        resp2 = await client.get(f"{GRAPH_API}/{page['id']}", params={
            "fields": "instagram_business_account",
            "access_token": page_token,
        })
        resp2.raise_for_status()
        ig_data = resp2.json().get("instagram_business_account")
        if not ig_data:
            raise ValueError("Instagram 비즈니스 계정이 연결되지 않았습니다")

        ig_id = ig_data["id"]
        # IG 계정 이름 조회
        resp3 = await client.get(f"{GRAPH_API}/{ig_id}", params={
            "fields": "username",
            "access_token": access_token,
        })
        resp3.raise_for_status()
        username = resp3.json().get("username", ig_id)

        return {
            "ig_user_id": ig_id,
            "username": username,
            "page_access_token": page_token,
        }


async def refresh_access_token(access_token: str) -> dict:
    """장기 토큰 갱신 (만료 전 갱신 가능)."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{GRAPH_API}/oauth/access_token", params={
            "grant_type": "fb_exchange_token",
            "client_id": settings.instagram_app_id,
            "client_secret": settings.instagram_app_secret,
            "fb_exchange_token": access_token,
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
    """Instagram Reels 업로드 (resumable upload)."""
    file_path = Path(video_path)
    file_size = file_path.stat().st_size

    async with httpx.AsyncClient(timeout=300) as client:
        # 1) Resumable 컨테이너 생성
        resp = await client.post(f"{GRAPH_API}/{ig_user_id}/media", data={
            "media_type": "REELS",
            "upload_type": "resumable",
            "caption": caption,
            "share_to_feed": "true",
            "access_token": access_token,
        })
        resp.raise_for_status()
        data = resp.json()
        creation_id = data["id"]
        upload_uri = data.get("uri")

        if not upload_uri:
            raise RuntimeError(f"업로드 URI를 받지 못했습니다: {data}")

        # 2) 영상 바이너리 업로드
        with open(file_path, "rb") as f:
            video_data = f.read()

        upload_resp = await client.post(upload_uri, headers={
            "Authorization": f"OAuth {access_token}",
            "Content-Type": "application/octet-stream",
            "offset": "0",
            "file_size": str(file_size),
        }, content=video_data)
        upload_resp.raise_for_status()

        # 3) 처리 상태 폴링
        for _ in range(60):  # 최대 5분
            await asyncio.sleep(5)
            status_resp = await client.get(f"{GRAPH_API}/{creation_id}", params={
                "fields": "status_code,status",
                "access_token": access_token,
            })
            status_resp.raise_for_status()
            status = status_resp.json()
            code = status.get("status_code")
            if code == "FINISHED":
                break
            elif code == "ERROR":
                raise RuntimeError(f"Instagram 처리 실패: {status.get('status')}")
        else:
            raise RuntimeError("Instagram 처리 시간 초과")

        # 4) 퍼블리시
        pub_resp = await client.post(f"{GRAPH_API}/{ig_user_id}/media_publish", data={
            "creation_id": creation_id,
            "access_token": access_token,
        })
        pub_resp.raise_for_status()
        media_id = pub_resp.json()["id"]

    url = f"https://www.instagram.com/reel/{media_id}/"
    print(f"[Instagram] 업로드 완료: {url}", file=sys.stderr)
    return {"video_id": media_id, "url": url}
