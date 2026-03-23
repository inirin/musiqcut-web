"""EchoMimicV3 Flash — ComfyUI API를 통한 오디오 드리븐 립싱크 영상 생성"""
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

from backend.utils.file_manager import clip_path, image_path

COMFYUI_DIR = Path(__file__).resolve().parent.parent.parent / "vendor" / "ComfyUI"
COMFYUI_URL = "http://127.0.0.1:8189"

# ── 모델 경로 ──
ECHO_MODELS = COMFYUI_DIR / "models" / "echo_mimic"
VAE_MODEL = r"split_files\vae\wan_2.1_vae.safetensors"
CLIP_MODEL = "umt5-xxl-encoder-Q5_K_M.gguf"
CLIP_VISION = r"split_files\clip_vision\clip_vision_h.safetensors"

# ── 생성 설정 ──
WIDTH = 512
HEIGHT = 768
FPS = 25.0
CFG = 6.0            # guidance_scale
STEPS = 8            # inference steps
AUDIO_SR = 16000     # 오디오 샘플레이트

# ── 최종 출력 (Wan I2V와 동일) ──
OUTPUT_WIDTH = 512
OUTPUT_HEIGHT = 768


def is_available() -> bool:
    """EchoMimicV3 Flash 사용 가능 여부 확인."""
    transformer = ECHO_MODELS / "transformer"
    flash_pro = ECHO_MODELS / "echomimicv3-flash-pro" / "diffusion_pytorch_model.safetensors"
    wav2vec = ECHO_MODELS / "chinese-wav2vec2-base"
    return (
        transformer.exists()
        and flash_pro.exists()
        and wav2vec.exists()
    )


def _build_echomimic_workflow(
    image_name: str,
    audio_name: str,
    prompt: str,
    seed: int,
    video_length: int = 125,
    output_prefix: str = "echomimic_out",
) -> dict:
    """EchoMimicV3 Flash ComfyUI 워크플로우 구성."""
    return {
        # ── 모델 로드 ──
        "load_model": {
            "class_type": "Echo_LoadModel",
            "inputs": {
                "vae": VAE_MODEL,
                "lora": "None",
                "denoising": True,
                "infer_mode": "audio_drived",
                "lowvram": False,
                "teacache_offload": True,
                "block_offload": True,
                "use_mmgp": "None",
                "version": "V3_flash",
            },
        },

        # ── 텍스트/이미지 인코더 ──
        "clip_loader": {
            "class_type": "CLIPLoaderGGUF",
            "inputs": {
                "clip_name": CLIP_MODEL,
                "type": "wan",
            },
        },
        "cv_loader": {
            "class_type": "CLIPVisionLoader",
            "inputs": {
                "clip_name": CLIP_VISION,
            },
        },

        # ── 입력 로드 ──
        "load_image": {
            "class_type": "LoadImage",
            "inputs": {
                "image": image_name,
            },
        },
        "load_audio": {
            "class_type": "LoadAudio",
            "inputs": {
                "audio": audio_name,
            },
        },

        # ── 데이터 준비 ──
        "predata": {
            "class_type": "Echo_Predata",
            "inputs": {
                "info": ["load_model", 1],
                "image": ["load_image", 0],
                "audio": ["load_audio", 0],
                "prompt": prompt,
                "negative_prompt": (
                    "Gesture is bad. Gesture is unclear. "
                    "Strange and twisted hands. Bad hands. Bad fingers. "
                    "Unclear and blurry hands."
                ),
                "pose_dir": "pose_01",
                "width": WIDTH,
                "height": HEIGHT,
                "fps": FPS,
                "facemask_ratio": 0.1,
                "facecrop_ratio": 0.5,
                "length": video_length,
                "partial_video_length": 97,
                "draw_mouse": False,
                "motion_sync_": False,
                "clip": ["clip_loader", 0],
                "clip_vision": ["cv_loader", 0],
            },
        },

        # ── 샘플링 ──
        "sampler": {
            "class_type": "Echo_Sampler",
            "inputs": {
                "model": ["load_model", 0],
                "emb": ["predata", 0],
                "seed": seed,
                "cfg": CFG,
                "steps": STEPS,
                "sample_rate": AUDIO_SR,
                "context_frames": 16,
                "context_overlap": 6,
                "save_video": False,
            },
        },

        # ── 프레임 저장 ──
        "save": {
            "class_type": "SaveAnimatedWEBP",
            "inputs": {
                "images": ["sampler", 0],
                "filename_prefix": output_prefix,
                "fps": FPS,
                "lossless": False,
                "quality": 85,
                "method": "default",
            },
        },
    }


async def _queue_and_wait(workflow: dict, timeout: int = 1200) -> dict:
    """ComfyUI에 워크플로우 제출 후 완료 대기."""
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
                                f"ComfyUI EchoMimic: {json.dumps(m[1])[:300]}")
                    raise RuntimeError("ComfyUI EchoMimic execution error")
        except urllib.error.HTTPError:
            pass

    raise TimeoutError(f"EchoMimicV3 추론 타임아웃 ({timeout}초)")


async def _find_output_video(prefix: str) -> Path:
    """ComfyUI output 디렉토리에서 생성된 영상 파일 찾기."""
    output_dir = COMFYUI_DIR / "output"
    for ext in ["mp4", "webp"]:
        files = sorted(
            output_dir.glob(f"{prefix}*.{ext}"),
            key=lambda f: f.stat().st_mtime, reverse=True)
        if files:
            return files[0]
    raise FileNotFoundError(f"EchoMimicV3 출력 파일 없음: {prefix}*")


async def _convert_to_mp4(source: Path, out_path: Path):
    """WEBP → MP4 변환."""
    from PIL import Image as PILImage
    import tempfile

    out_path.parent.mkdir(parents=True, exist_ok=True)

    if source.suffix == ".mp4":
        shutil.copy2(str(source), str(out_path))
        source.unlink(missing_ok=True)
        return

    # WEBP 애니메이션 → MP4
    img = PILImage.open(str(source))
    frames = []
    try:
        while True:
            frames.append(img.copy())
            img.seek(img.tell() + 1)
    except EOFError:
        pass

    tmp = tempfile.mkdtemp()
    for i, f in enumerate(frames):
        f.save(os.path.join(tmp, f"frame_{i:04d}.png"))

    cmd = [
        "ffmpeg", "-y", "-framerate", str(int(FPS)),
        "-i", os.path.join(tmp, "frame_%04d.png"),
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(out_path)]
    result = await asyncio.to_thread(
        subprocess.run, cmd, capture_output=True, text=True)
    shutil.rmtree(tmp, ignore_errors=True)
    source.unlink(missing_ok=True)

    if result.returncode != 0:
        raise RuntimeError(f"EchoMimic WEBP→MP4 변환 실패: {result.stderr[-300:]}")


async def _extract_audio_segment(
    audio_path: str, start_sec: float, duration_sec: float, out_wav: Path
) -> float:
    """음원에서 특정 구간을 16kHz mono WAV로 추출. 실제 길이 반환."""
    cmd = [
        "ffmpeg", "-y", "-i", audio_path,
        "-ss", str(start_sec), "-t", str(duration_sec),
        "-ar", str(AUDIO_SR), "-ac", "1",
        str(out_wav)]
    result = await asyncio.to_thread(
        subprocess.run, cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"오디오 세그먼트 추출 실패: {result.stderr[-200:]}")

    # 실제 길이 확인
    dur_cmd = [
        "ffprobe", "-v", "quiet", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", str(out_wav)]
    dur_result = await asyncio.to_thread(
        subprocess.run, dur_cmd, capture_output=True, text=True)
    return float(dur_result.stdout.strip()) if dur_result.returncode == 0 else duration_sec


async def generate_lipsync_clip(
    project_id: str,
    scene_no: int,
    audio_path: str,
    scene_start_sec: float = 0.0,
    clip_duration: float = 5.0,
    prompt: str = "Animated character speaking with expressive face",
) -> str:
    """단일 장면에 대해 EchoMimicV3 Flash로 립싱크 클립 생성.

    Returns:
        생성된 클립 파일 경로 (512×768 MP4)
    """
    input_dir = COMFYUI_DIR / "input"
    input_dir.mkdir(parents=True, exist_ok=True)

    # 이미지를 ComfyUI input으로 복사
    src_img = str(image_path(project_id, scene_no))
    img_name = f"echo_{project_id[:8]}_{scene_no:02d}.png"
    shutil.copy2(src_img, str(input_dir / img_name))

    # 오디오 세그먼트 추출 (16kHz mono WAV)
    audio_name = f"echo_{project_id[:8]}_{scene_no:02d}.wav"
    audio_dest = input_dir / audio_name
    actual_dur = await _extract_audio_segment(
        audio_path, scene_start_sec, clip_duration, audio_dest)

    video_length = min(int(actual_dur * FPS), 250)  # 최대 10초
    if video_length < 5:
        video_length = int(clip_duration * FPS)  # 폴백

    print(f"[EchoMimic] 장면 {scene_no}: {actual_dur:.1f}초 오디오 → "
          f"{video_length} 프레임", file=sys.stderr)

    seed = int(time.time()) % 2**32 + scene_no
    prefix = f"echomimic_{project_id[:8]}_{scene_no:02d}"

    wf = _build_echomimic_workflow(
        img_name, audio_name, prompt, seed, video_length, prefix)

    await _queue_and_wait(wf, timeout=1200)

    # 출력 파일 찾기 + pillarbox MP4 변환
    out = clip_path(project_id, scene_no)
    source = await _find_output_video(prefix)
    await _convert_to_mp4(source, out)

    print(f"[EchoMimic] 장면 {scene_no} 립싱크 클립 생성 완료: {out}",
          file=sys.stderr)
    return str(out)
