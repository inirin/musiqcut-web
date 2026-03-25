"""API 키 상태 확인, 검증, 저장"""
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
import httpx
from pathlib import Path
from google import genai
from backend.config import settings

router = APIRouter()


class SaveKeyRequest(BaseModel):
    gemini_api_key: Optional[str] = None
    suno_api_key: Optional[str] = None
    imagen_api_keys: Optional[str] = None  # 쉼표 구분


def _update_env(updates: dict[str, str]):
    """기존 .env 파일을 읽어서 값만 업데이트하고 저장"""
    env_path = Path(".env")
    if not env_path.exists():
        import shutil
        shutil.copy(".env.example", ".env")

    lines = env_path.read_text(encoding="utf-8").splitlines()
    updated_keys = set()

    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            new_lines.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in updates:
            new_lines.append(f"{key}={updates[key]}")
            updated_keys.add(key)
        else:
            new_lines.append(line)

    # .env에 없던 키면 맨 뒤에 추가
    for key, val in updates.items():
        if key not in updated_keys:
            new_lines.append(f"{key}={val}")

    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


@router.post("/save")
async def save_keys(body: SaveKeyRequest):
    updates = {}
    if body.gemini_api_key is not None:
        updates["GEMINI_API_KEY"] = body.gemini_api_key
    if body.suno_api_key is not None:
        updates["SUNO_API_KEY"] = body.suno_api_key
    if body.imagen_api_keys is not None:
        updates["IMAGEN_API_KEYS"] = body.imagen_api_keys

    if not updates:
        return {"ok": False, "error": "저장할 값이 없습니다"}

    try:
        _update_env(updates)
        # 런타임 설정도 즉시 반영
        for k, v in updates.items():
            attr = k.lower()
            if hasattr(settings, attr):
                object.__setattr__(settings, attr, v)
        return {"ok": True, "saved": list(updates.keys())}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/status")
async def keys_status():
    imagen_keys = [k.strip() for k in settings.imagen_api_keys.split(",") if k.strip()] if settings.imagen_api_keys else []
    return {
        "suno": bool(settings.suno_api_key),
        "suno_masked": settings.suno_api_key or "",
        "gemini": bool(settings.gemini_api_key),
        "gemini_masked": settings.gemini_api_key or "",
        "imagen_count": len(imagen_keys) if imagen_keys else (1 if settings.gemini_api_key else 0),
        "imagen_keys_masked": imagen_keys,
        "missing": settings.missing_keys()
    }


@router.post("/test/gemini")
async def test_gemini():
    if not settings.gemini_api_key:
        return {"ok": False, "error": "GEMINI_API_KEY 미설정"}
    try:
        client = genai.Client(api_key=settings.gemini_api_key)
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents="Say hello in one word"
        )
        return {"ok": True, "response": resp.text[:50]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/test/suno")
async def test_suno():
    if not settings.suno_api_key:
        return {"ok": False, "error": "SUNO_API_KEY 미설정. suno.com → 계정 → API Keys에서 발급하세요."}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.sunoapi.org/api/v1/generate/record-info",
                headers={"Authorization": f"Bearer {settings.suno_api_key}"},
                params={"taskId": "test"}
            )
            if resp.status_code == 401:
                return {"ok": False, "error": "API 키가 유효하지 않습니다"}
            return {"ok": True, "message": "API 키 확인됨"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
