"""Gemini API 공용 클라이언트 — 멀티 키 로테이션"""
import asyncio
import sys
from google import genai
from backend.config import settings

_current_key_idx = 0


def get_api_keys() -> list[str]:
    """사용 가능한 Gemini API 키 목록."""
    if settings.imagen_api_keys:
        keys = [k.strip() for k in settings.imagen_api_keys.split(",") if k.strip()]
        if keys:
            return keys
    if settings.gemini_api_key:
        return [settings.gemini_api_key]
    return []


async def gemini_generate(model: str, **kwargs):
    """멀티 키 로테이션으로 Gemini generate_content 호출."""
    global _current_key_idx
    keys = get_api_keys()
    if not keys:
        raise ValueError("Gemini API 키 미설정")
    errors = []
    for attempt in range(len(keys)):
        idx = (_current_key_idx + attempt) % len(keys)
        try:
            client = genai.Client(api_key=keys[idx])
            result = await asyncio.to_thread(
                client.models.generate_content, model=model, **kwargs)
            _current_key_idx = idx
            return result
        except Exception as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                print(f"[Gemini] 키{idx+1} 할당량 초과 → 다음 키", file=sys.stderr)
                errors.append(f"키{idx+1}: 할당량 초과")
                continue
            raise
    _current_key_idx = (_current_key_idx + 1) % len(keys)
    raise RuntimeError(f"모든 Gemini 키({len(keys)}개) 할당량 초과: {' / '.join(errors)}")


async def gemini_generate_images(prompt: str, **config_kwargs) -> bytes:
    """멀티 키 로테이션으로 Imagen 이미지 생성."""
    global _current_key_idx
    from google.genai import types
    keys = get_api_keys()
    if not keys:
        raise ValueError("API 키 미설정")
    errors = []
    for attempt in range(len(keys)):
        idx = (_current_key_idx + attempt) % len(keys)
        try:
            client = genai.Client(api_key=keys[idx])
            response = await asyncio.to_thread(
                client.models.generate_images,
                model="imagen-4.0-generate-001",
                prompt=prompt,
                config=types.GenerateImagesConfig(
                    number_of_images=1,
                    aspect_ratio="9:16",
                    safety_filter_level="BLOCK_LOW_AND_ABOVE",
                    **config_kwargs,
                )
            )
            if response.generated_images:
                _current_key_idx = idx
                return response.generated_images[0].image.image_bytes
            errors.append(f"키{idx+1}: 결과 없음")
        except Exception as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                print(f"[Imagen] 키{idx+1} 할당량 초과 → 다음 키", file=sys.stderr)
                errors.append(f"키{idx+1}: 할당량 초과")
                continue
            raise
    _current_key_idx = (_current_key_idx + 1) % len(keys)
    raise RuntimeError(f"모든 API 키({len(keys)}개) 할당량 초과: {' / '.join(errors)}")
