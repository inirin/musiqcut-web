"""Wan 2.2 I2V — 네이티브 ComfyUI 노드 + FP8 + Lightning LoRA"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Callable, Optional
from PIL import Image

from backend.utils.file_manager import clip_path, image_path

COMFYUI_DIR = Path(__file__).resolve().parent.parent.parent / "vendor" / "ComfyUI"
COMFYUI_URL = "http://127.0.0.1:8189"

# GGUF Q5_K_M 모델 (unet 디렉토리) — 16GB VRAM 최적
HIGH_NOISE = "wan2.2_i2v_high_noise_14B_Q5_K_M.gguf"
LOW_NOISE = "wan2.2_i2v_low_noise_14B_Q5_K_M.gguf"
VAE_MODEL = r"split_files\vae\wan_2.1_vae.safetensors"
CLIP_MODEL = "umt5-xxl-encoder-Q5_K_M.gguf"
CLIP_VISION = r"split_files\clip_vision\clip_vision_h.safetensors"

# Lightning LoRA
LORA_HIGH = "wan22_i2v_lightning_high.safetensors"
LORA_LOW = "wan22_i2v_lightning_low.safetensors"

# 커뮤니티 검증 설정 — 네이티브 노드 + Lightning LoRA
TOTAL_STEPS = 8
SWITCH_STEP = 3       # HIGH 3 + LOW 5 (40/60 분배, 커뮤니티 권장)
BLOCKS_TO_SWAP = 40   # 14B 모델 전체 40블록 swap (16GB VRAM 필수)
SCHEDULER = "simple"
SAMPLER = "euler"
CFG = 1.0
SHIFT = 8.0
WIDTH = 576           # 세로 9:16
HEIGHT = 1024
FPS = 16              # Wan I2V 네이티브 FPS (ComfyUI 생성용)
OUTPUT_FPS = 24       # 최종 출력 FPS (LTX와 통일)
DEFAULT_FRAMES = 81   # 기본 프레임 수 (duration 미지정 시)

CLIP_DURATION = 5  # 장면 수 계산 기준 (초)

FALLBACK_PROMPTS = [
    "smooth subtle motion, soft lighting, cinematic",
    "gentle camera pan, still character, ambient light",
]


def is_available() -> bool:
    unet_dir = COMFYUI_DIR / "models" / "unet"
    return (unet_dir / HIGH_NOISE).exists() and (unet_dir / LOW_NOISE).exists()


def get_clip_duration() -> float:
    return CLIP_DURATION


def _calc_frames(duration: float) -> int:
    """장면 길이(초)에 맞는 프레임 수 계산. Wan I2V는 4n+1 프레임만 허용."""
    n = max(1, round(duration * FPS / 4))
    return n * 4 + 1  # 예: 5초 → round(80/4)=20 → 81, 7초 → round(112/4)=28 → 113


def _build_native_workflow(image_name: str, prompt: str, seed: int,
                           output_prefix: str, num_frames: int = DEFAULT_FRAMES,
                           neg_prompt: str = "blurry, distorted, low quality, static, watermark") -> dict:
    """네이티브 ComfyUI 노드 워크플로우 — 720p, 2-stage HIGH→LOW."""
    return {
        # ── 모델 로드 ──
        # HIGH model + LoRA + shift + block swap
        "unet_high": {"class_type": "UnetLoaderGGUF", "inputs": {
            "unet_name": HIGH_NOISE}},
        "lora_high": {"class_type": "LoraLoaderModelOnly", "inputs": {
            "model": ["unet_high", 0],
            "lora_name": LORA_HIGH,
            "strength_model": 1.0}},
        "shift_high": {"class_type": "ModelSamplingSD3", "inputs": {
            "model": ["lora_high", 0],
            "shift": SHIFT}},
        "bs_high": {"class_type": "wanBlockSwap", "inputs": {
            "model": ["shift_high", 0],
            "blocks_to_swap": BLOCKS_TO_SWAP}},

        # LOW model + LoRA + shift + block swap
        "unet_low": {"class_type": "UnetLoaderGGUF", "inputs": {
            "unet_name": LOW_NOISE}},
        "lora_low": {"class_type": "LoraLoaderModelOnly", "inputs": {
            "model": ["unet_low", 0],
            "lora_name": LORA_LOW,
            "strength_model": 1.0}},
        "shift_low": {"class_type": "ModelSamplingSD3", "inputs": {
            "model": ["lora_low", 0],
            "shift": SHIFT}},
        "bs_low": {"class_type": "wanBlockSwap", "inputs": {
            "model": ["shift_low", 0],
            "blocks_to_swap": BLOCKS_TO_SWAP}},

        # ── 텍스트/이미지 인코딩 ──
        "clip_loader": {"class_type": "CLIPLoaderGGUF", "inputs": {
            "clip_name": CLIP_MODEL,
            "type": "wan"}},
        "pos": {"class_type": "CLIPTextEncode", "inputs": {
            "text": prompt,
            "clip": ["clip_loader", 0]}},
        "neg": {"class_type": "CLIPTextEncode", "inputs": {
            "text": neg_prompt,
            "clip": ["clip_loader", 0]}},

        "vae": {"class_type": "VAELoader", "inputs": {
            "vae_name": VAE_MODEL}},
        "img": {"class_type": "LoadImage", "inputs": {
            "image": image_name}},
        "cv_loader": {"class_type": "CLIPVisionLoader", "inputs": {
            "clip_name": CLIP_VISION}},
        "cv_enc": {"class_type": "CLIPVisionEncode", "inputs": {
            "clip_vision": ["cv_loader", 0],
            "image": ["img", 0],
            "crop": "center"}},

        # ── I2V 조건화 ──
        "i2v": {"class_type": "WanImageToVideo", "inputs": {
            "positive": ["pos", 0],
            "negative": ["neg", 0],
            "vae": ["vae", 0],
            "width": WIDTH,
            "height": HEIGHT,
            "length": num_frames,
            "batch_size": 1,
            "clip_vision_output": ["cv_enc", 0],
            "start_image": ["img", 0]}},

        # ── Stage 1: HIGH noise (steps 0→3) ──
        "sampler_high": {"class_type": "KSamplerAdvanced", "inputs": {
            "model": ["bs_high", 0],
            "add_noise": "enable",
            "noise_seed": seed,
            "steps": TOTAL_STEPS,
            "cfg": CFG,
            "sampler_name": SAMPLER,
            "scheduler": SCHEDULER,
            "positive": ["i2v", 0],
            "negative": ["i2v", 1],
            "latent_image": ["i2v", 2],
            "start_at_step": 0,
            "end_at_step": SWITCH_STEP,
            "return_with_leftover_noise": "enable"}},

        # ── Stage 2: LOW noise (steps 3→8) ──
        "sampler_low": {"class_type": "KSamplerAdvanced", "inputs": {
            "model": ["bs_low", 0],
            "add_noise": "disable",
            "noise_seed": seed,
            "steps": TOTAL_STEPS,
            "cfg": CFG,
            "sampler_name": SAMPLER,
            "scheduler": SCHEDULER,
            "positive": ["i2v", 0],
            "negative": ["i2v", 1],
            "latent_image": ["sampler_high", 0],
            "start_at_step": SWITCH_STEP,
            "end_at_step": 10000,
            "return_with_leftover_noise": "disable"}},

        # ── 디코딩 + 저장 ──
        "decode": {"class_type": "VAEDecode", "inputs": {
            "samples": ["sampler_low", 0],
            "vae": ["vae", 0]}},
        "save": {"class_type": "SaveAnimatedWEBP", "inputs": {
            "images": ["decode", 0],
            "filename_prefix": output_prefix,
            "fps": 16, "lossless": False,
            "quality": 85, "method": "default"}},
    }


async def _queue_and_wait(workflow: dict, timeout: int = 1800) -> dict:
    data = json.dumps({"prompt": workflow}).encode("utf-8")
    req = urllib.request.Request(
        f"{COMFYUI_URL}/prompt", data=data,
        headers={"Content-Type": "application/json"})
    resp = await asyncio.to_thread(urllib.request.urlopen, req)
    result = json.loads(resp.read())
    prompt_id = result["prompt_id"]

    start = time.time()
    while time.time() - start < timeout:
        await asyncio.sleep(5)
        try:
            resp = await asyncio.to_thread(
                urllib.request.urlopen,
                f"{COMFYUI_URL}/history/{prompt_id}")
            history = json.loads(resp.read())
            if prompt_id in history:
                status = history[prompt_id].get("status", {})
                if status.get("completed") or history[prompt_id].get("outputs"):
                    return history[prompt_id]
                if status.get("status_str") == "error":
                    for m in status.get("messages", []):
                        if m[0] == "execution_error":
                            raise RuntimeError(
                                f"ComfyUI: {json.dumps(m[1])[:300]}")
                    raise RuntimeError("ComfyUI execution error")
        except urllib.error.HTTPError:
            pass

    raise TimeoutError(f"ComfyUI 추론 타임아웃 ({timeout}초)")


async def _convert_webp_to_mp4(prefix: str, out_path: Path):
    output_dir = COMFYUI_DIR / "output"
    webps = sorted(output_dir.glob(f"{prefix}*.webp"),
                   key=lambda f: f.stat().st_mtime, reverse=True)
    if not webps:
        raise FileNotFoundError(f"ComfyUI 출력 WEBP 없음: {prefix}*")

    webp_file = webps[0]
    img = Image.open(str(webp_file))
    frames = []
    try:
        while True:
            frames.append(img.copy())
            img.seek(img.tell() + 1)
    except EOFError:
        pass

    import tempfile
    tmp = tempfile.mkdtemp()
    for i, f in enumerate(frames):
        f.save(os.path.join(tmp, f"frame_{i:04d}.png"))

    cmd = [
        "ffmpeg", "-y", "-framerate", str(FPS),
        "-i", os.path.join(tmp, "frame_%04d.png"),
        "-vf", f"fps={OUTPUT_FPS}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(out_path)]
    result = await asyncio.to_thread(
        subprocess.run, cmd, capture_output=True, text=True)
    shutil.rmtree(tmp, ignore_errors=True)
    webp_file.unlink(missing_ok=True)

    if result.returncode != 0:
        raise RuntimeError(f"WEBP→MP4 변환 실패: {result.stderr[-300:]}")


async def _ffmpeg_still_video(image_file: str, out_path: Path,
                              duration: float = 5.0):
    cmd = [
        "ffmpeg", "-y", "-loop", "1", "-i", image_file,
        "-c:v", "libx264", "-t", str(duration), "-pix_fmt", "yuv420p",
        "-vf", f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
               f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2",
        "-r", str(OUTPUT_FPS), str(out_path)]
    result = await asyncio.to_thread(
        subprocess.run, cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg 정지 영상 생성 실패: {result.stderr[-300:]}")


async def generate_video_clips(
    project_id: str,
    scenes: list,
    image_paths: list[str],
    progress_cb: Optional[Callable] = None,
) -> list[str]:
    input_dir = COMFYUI_DIR / "input"
    input_dir.mkdir(parents=True, exist_ok=True)

    clip_paths_out = []
    for i, scene in enumerate(scenes):
        out = clip_path(project_id, scene.scene_no)

        if out.exists() and out.stat().st_size > 1000:
            print(f"[STEP4] 장면 {scene.scene_no} 클립 이미 존재, 건너뜀",
                  file=sys.stderr)
            clip_paths_out.append(str(out))
            if progress_cb:
                await progress_cb(current=i + 1, total=len(scenes))
            continue

        src_img = str(image_path(project_id, scene.scene_no))
        img_name = f"scene_{project_id[:8]}_{scene.scene_no:02d}.png"
        # 소스 이미지를 Wan 해상도로 리사이즈하여 ComfyUI input에 복사
        img = Image.open(src_img)
        if img.size != (WIDTH, HEIGHT):
            img = img.resize((WIDTH, HEIGHT), Image.LANCZOS)
        img.save(str(input_dir / img_name))

        shot_type = getattr(scene, 'shot_type', 'medium')
        if shot_type == 'wide':
            # 와이드샷: 물리법칙 준수 + 자연스러운 다채로움
            main_prompt = (
                f'Cinematic scene, gentle camera movement, '
                f'all motion must obey real-world physics, '
                f'only things that naturally move in reality should move '
                f'(e.g. water flows, clouds drift, leaves fall, fire flickers, wind blows), '
                f'rigid objects stay still, no people appearing, '
                f'{getattr(scene, "image_prompt", scene.description)}'
            )
        else:
            # 클로즈업/미디엄: 캐릭터 모션 (립싱크 아닌 클립 — 입 닫힌 채)
            main_prompt = (
                f'Animated character with mouth firmly closed the entire time, '
                f'lips sealed shut, never opens mouth, '
                f'natural body sway, expressive eyes, gentle head movement, '
                f'cinematic lighting, {getattr(scene, "image_prompt", scene.description)}'
            )
        prompts_to_try = [main_prompt]
        prompts_to_try.extend(FALLBACK_PROMPTS)

        last_err = None
        success = False
        for attempt_idx, attempt_prompt in enumerate(prompts_to_try):
            try:
                print(f"[STEP4] 시도 {attempt_idx+1} (장면 {scene.scene_no}): "
                      f"'{attempt_prompt[:80]}'", file=sys.stderr)
                prefix = f"wan_i2v_{project_id[:8]}_{scene.scene_no:02d}"
                seed = int(time.time()) % 2**32 + scene.scene_no
                print(f"[STEP4]   Native 720p + Lightning "
                      f"({TOTAL_STEPS} steps: {SWITCH_STEP} HIGH + "
                      f"{TOTAL_STEPS - SWITCH_STEP} LOW)", file=sys.stderr)
                scene_dur = getattr(scene, 'duration', 0) or CLIP_DURATION
                frames = _calc_frames(scene_dur)
                neg = ("blurry, distorted, low quality, watermark, "
                       "person appearing, human emerging, "
                       "physically impossible motion, defying gravity, "
                       "inanimate objects moving on their own "
                       "(e.g. clothes standing up, furniture sliding, rocks floating)"
                       if shot_type == 'wide' else
                       "blurry, distorted, low quality, watermark, "
                       "talking, singing, lip sync, mouth opening and closing as if speaking")
                wf = _build_native_workflow(
                    img_name, attempt_prompt, seed, prefix, frames,
                    neg_prompt=neg)
                await _queue_and_wait(wf, timeout=1800)
                await _convert_webp_to_mp4(prefix, out)
                success = True
                break
            except Exception as e:
                last_err = e
                print(f"[STEP4] 시도 {attempt_idx+1} 실패 "
                      f"(장면 {scene.scene_no}): {e}", file=sys.stderr)

        if not success:
            print(f"[STEP4] 모든 시도 실패 (장면 {scene.scene_no}), "
                  f"정지 이미지 영상으로 대체", file=sys.stderr)
            fallback_dur = getattr(scene, 'duration', 0) or CLIP_DURATION
            await _ffmpeg_still_video(src_img, out, duration=fallback_dur)

        clip_paths_out.append(str(out))
        if progress_cb:
            await progress_cb(current=i + 1, total=len(scenes))

    return clip_paths_out
