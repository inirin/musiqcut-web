"""Gemini API Imagen 4 이미지 생성 — 멀티 API 키 로테이션"""
import asyncio
import sys
from io import BytesIO
from pathlib import Path
from typing import Callable, Optional

from PIL import Image

from backend.utils.file_manager import image_path
from backend.utils.gemini_client import gemini_generate_images, get_api_keys

TARGET_WIDTH = 576
TARGET_HEIGHT = 1024


def _resize_to_target(img_bytes: bytes) -> bytes:
    """Imagen 출력을 576×1024 (9:16)으로 리사이즈+크롭."""
    img = Image.open(BytesIO(img_bytes))
    # 너비 기준 리사이즈 후 높이 크롭/패딩
    ratio = TARGET_WIDTH / img.width
    new_h = int(img.height * ratio)
    img = img.resize((TARGET_WIDTH, new_h), Image.LANCZOS)

    if new_h >= TARGET_HEIGHT:
        top = (new_h - TARGET_HEIGHT) // 2
        img = img.crop((0, top, TARGET_WIDTH, top + TARGET_HEIGHT))
    else:
        canvas = Image.new("RGB", (TARGET_WIDTH, TARGET_HEIGHT), (0, 0, 0))
        paste_y = (TARGET_HEIGHT - new_h) // 2
        canvas.paste(img, (0, paste_y))
        img = canvas

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def generate_images(
    project_id: str,
    scenes: list,
    progress_cb: Optional[Callable] = None,
) -> list[str]:
    """Imagen 4로 장면 이미지 생성 (멀티 키 로테이션)."""
    keys = get_api_keys()
    if not keys:
        raise ValueError("API 키가 설정되지 않았습니다.")

    print(f"[STEP3] Imagen API 키 {len(keys)}개 사용 가능", file=sys.stderr)
    paths = []

    for i, scene in enumerate(scenes):
        out = image_path(project_id, scene.scene_no)

        if out.exists() and out.stat().st_size > 1000:
            print(f"[STEP3] 장면 {scene.scene_no} 이미지 이미 존재, 건너뜀",
                  file=sys.stderr)
            paths.append(str(out))
            if progress_cb:
                await progress_cb(current=i + 1, total=len(scenes))
            continue

        prompt = scene.image_prompt
        print(f"[STEP3] Imagen 4 생성 (장면 {scene.scene_no}): "
              f"'{prompt[:80]}'", file=sys.stderr)

        img_data = await gemini_generate_images(prompt)
        img_data = await asyncio.to_thread(_resize_to_target, img_data)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(img_data)
        print(f"[STEP3] 장면 {scene.scene_no} 이미지 생성 완료 ({len(img_data)} bytes)",
              file=sys.stderr)

        paths.append(str(out))
        if progress_cb:
            await progress_cb(current=i + 1, total=len(scenes))

        if i < len(scenes) - 1:
            await asyncio.sleep(2)

    return paths
