"""Wan 2.2 S2V — Speech-to-Video (Kijai WanVideoWrapper + GGUF, 16GB VRAM)"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional

from PIL import Image

from backend.utils.file_manager import clip_path, image_path

COMFYUI_DIR = Path(__file__).resolve().parent.parent.parent / "vendor" / "ComfyUI"
COMFYUI_URL = "http://127.0.0.1:8189"

# ── 모델 파일 ──
S2V_MODEL = "Wan2.2-S2V-14B-Q4_K_M.gguf"
VAE_MODEL = r"split_files\vae\wan_2.1_vae.safetensors"
CLIP_MODEL = "umt5-xxl-encoder-Q5_K_M.gguf"  # CLIPLoaderGGUF로 로드
AUDIO_ENCODER = "wav2vec2_large_english_fp16.safetensors"

# ── 생성 설정 (16GB VRAM, 480p) ──
WIDTH = 480           # 16GB VRAM 최적 480p (9:16 세로)
HEIGHT = 832
FPS = 16              # Wan S2V 네이티브 FPS
OUTPUT_FPS = 24       # 최종 출력 FPS
STEPS = 10            # 속도 최적화 (20→10, ~2배 빠름)
CFG = 4.5             # S2V 공식 기본값 근처
SHIFT = 3.0           # S2V 공식 기본값 (8→3, 립싱크 표현력 극대화)
BLOCKS_TO_SWAP = 25   # 16GB VRAM → CPU 오프로드
AUDIO_SCALE = 1.2     # 노래 표현력 강화 (1.0→1.2)

CLIP_DURATION = 5     # 기본 클립 길이 (초)


def is_available() -> bool:
    unet_dir = COMFYUI_DIR / "models" / "unet"
    audio_dir = COMFYUI_DIR / "models" / "audio_encoders"
    return (unet_dir / S2V_MODEL).exists() and (audio_dir / AUDIO_ENCODER).exists()


def get_clip_duration() -> float:
    return CLIP_DURATION


def _calc_frames(duration: float) -> int:
    """S2V 프레임 수 계산. 4n+1 프레임."""
    n = max(1, round(duration * FPS / 4))
    return n * 4 + 1


def _build_s2v_workflow(
    image_name: str, audio_name: str, prompt: str,
    seed: int, num_frames: int = 81,
    output_prefix: str = "wan_s2v_out",
    neg_prompt: str = "色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，最差质量，低质量，JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，画得不好的脸部，畸形的，毁容的，形态畸形的肢体，手指融合，静止不动的画面，杂乱的背景，三条腿，背景人很多，倒着走, ((realistic)), ((photograph)), ((photorealistic)), blurry, distorted, low quality, watermark"
) -> dict:
    """Wan 2.2 S2V 워크플로우 (Kijai WanVideoWrapper, GGUF Q4_K_M, 16GB)."""
    # overbaked first frame 대응: 4프레임 여분 생성 → 디코드 후 제거
    gen_frames = num_frames + 4
    return {
        # ── 모델 로드 (S2V GGUF + BlockSwap) ──
        "model": {"class_type": "WanVideoModelLoader", "inputs": {
            "model": S2V_MODEL,
            "base_precision": "bf16",
            "quantization": "disabled",
            "load_device": "offload_device",
            "attention_mode": "sageattn"}},

        "block_swap_args": {"class_type": "WanVideoBlockSwap", "inputs": {
            "blocks_to_swap": BLOCKS_TO_SWAP,
            "offload_txt_in": False,
            "offload_img_emb": False,
            "offload_txt_emb": True,
            "offload_modulation": 0,
            "offload_head": 1,
            "aggressive": False}},

        "block_swap": {"class_type": "WanVideoSetBlockSwap", "inputs": {
            "model": ["model", 0],
            "block_swap_args": ["block_swap_args", 0]}},

        # ── VAE 로드 ──
        "vae": {"class_type": "WanVideoVAELoader", "inputs": {
            "model_name": VAE_MODEL,
            "precision": "bf16"}},

        # ── 텍스트 인코딩 (CLIPLoaderGGUF + TextEmbedBridge) ──
        "clip_loader": {"class_type": "CLIPLoaderGGUF", "inputs": {
            "clip_name": CLIP_MODEL,
            "type": "wan"}},
        "pos": {"class_type": "CLIPTextEncode", "inputs": {
            "text": prompt,
            "clip": ["clip_loader", 0]}},
        "neg": {"class_type": "CLIPTextEncode", "inputs": {
            "text": neg_prompt,
            "clip": ["clip_loader", 0]}},
        "text_bridge": {"class_type": "WanVideoTextEmbedBridge", "inputs": {
            "positive": ["pos", 0],
            "negative": ["neg", 0]}},

        # ── 오디오 인코딩 (Wav2Vec2) ──
        "audio_loader": {"class_type": "AudioEncoderLoader", "inputs": {
            "audio_encoder_name": AUDIO_ENCODER}},
        "audio_load": {"class_type": "LoadAudio", "inputs": {
            "audio": audio_name}},
        "audio_enc": {"class_type": "AudioEncoderEncode", "inputs": {
            "audio_encoder": ["audio_loader", 0],
            "audio": ["audio_load", 0]}},

        # ── 이미지 로드 ──
        "img": {"class_type": "LoadImage", "inputs": {
            "image": image_name}},

        # ── 레퍼런스 이미지 인코딩 (VAE) ──
        "ref_encode": {"class_type": "WanVideoEncode", "inputs": {
            "vae": ["vae", 0],
            "image": ["img", 0],
            "enable_vae_tiling": True,
            "tile_x": 272,
            "tile_y": 272,
            "tile_stride_x": 144,
            "tile_stride_y": 128}},

        # ── 빈 임베딩 (S2V 시작점) ──
        "empty_embeds": {"class_type": "WanVideoEmptyEmbeds", "inputs": {
            "width": WIDTH,
            "height": HEIGHT,
            "num_frames": gen_frames}},

        # ── S2V 임베딩 결합 (이미지 + 오디오) ──
        "s2v_embeds": {"class_type": "WanVideoAddS2VEmbeds", "inputs": {
            "embeds": ["empty_embeds", 0],
            "frame_window_size": gen_frames,
            "audio_scale": AUDIO_SCALE,
            "pose_start_percent": 0.0,
            "pose_end_percent": 1.0,
            "audio_encoder_output": ["audio_enc", 0],
            "ref_latent": ["ref_encode", 0],
            "vae": ["vae", 0],
            "enable_framepack": False}},

        # ── 샘플러 (WanVideoSampler에 shift 내장) ──
        "sampler": {"class_type": "WanVideoSampler", "inputs": {
            "model": ["block_swap", 0],
            "image_embeds": ["s2v_embeds", 0],
            "text_embeds": ["text_bridge", 0],
            "steps": STEPS,
            "cfg": CFG,
            "shift": SHIFT,
            "seed": seed,
            "force_offload": True,
            "scheduler": "unipc",
            "riflex_freq_index": 0}},

        # ── 디코딩 ──
        "decode": {"class_type": "WanVideoDecode", "inputs": {
            "vae": ["vae", 0],
            "samples": ["sampler", 0],
            "enable_vae_tiling": True,
            "tile_x": 272,
            "tile_y": 272,
            "tile_stride_x": 144,
            "tile_stride_y": 128}},

        # ── 첫 4프레임 제거 (overbaked first frame VAE 아티팩트) ──
        "trim": {"class_type": "ImageFromBatch", "inputs": {
            "image": ["decode", 0],
            "batch_index": 4,
            "length": num_frames}},

        # ── 저장 ──
        "save": {"class_type": "SaveAnimatedWEBP", "inputs": {
            "images": ["trim", 0],
            "filename_prefix": output_prefix,
            "fps": FPS, "lossless": False,
            "quality": 85, "method": "default"}},
    }


async def _queue_and_wait(workflow: dict, timeout: int = 3600,
                          abort_check=None) -> dict:
    """ComfyUI에 워크플로우 큐잉하고 완료 대기. S2V는 느리므로 1시간 타임아웃."""
    data = json.dumps({"prompt": workflow}).encode("utf-8")
    req = urllib.request.Request(
        f"{COMFYUI_URL}/prompt", data=data,
        headers={"Content-Type": "application/json"})
    resp = await asyncio.to_thread(urllib.request.urlopen, req)
    result = json.loads(resp.read())
    prompt_id = result["prompt_id"]

    start = time.time()
    while time.time() - start < timeout:
        # 0.5초 단위로 abort 체크 (총 10초 대기)
        for _ in range(20):
            await asyncio.sleep(0.5)
            if abort_check and abort_check():
                try:
                    _req = urllib.request.Request(
                        f"{COMFYUI_URL}/interrupt", data=b"",
                        headers={"Content-Type": "application/json"},
                        method="POST")
                    urllib.request.urlopen(_req)
                except Exception:
                    pass
                from backend.services.wan_video_service import _AbortedError
                raise _AbortedError("파이프라인 중단 요청")
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
                                f"ComfyUI S2V: {json.dumps(m[1])[:300]}")
                    raise RuntimeError("ComfyUI S2V execution error")
        except urllib.error.HTTPError:
            pass

    raise TimeoutError(f"S2V 타임아웃 ({timeout}초)")


async def _convert_webp_to_mp4(prefix: str, out_path: Path,
                                audio_path: Optional[str] = None):
    """WEBP → MP4 변환, 선택적으로 오디오 합성."""
    output_dir = COMFYUI_DIR / "output"
    webps = sorted(output_dir.glob(f"{prefix}*.webp"),
                   key=lambda f: f.stat().st_mtime, reverse=True)
    if not webps:
        raise FileNotFoundError(f"ComfyUI S2V 출력 WEBP 없음: {prefix}*")

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

    if audio_path:
        cmd = [
            "ffmpeg", "-y", "-framerate", str(FPS),
            "-i", os.path.join(tmp, "frame_%04d.png"),
            "-i", audio_path,
            "-vf", f"fps={OUTPUT_FPS}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest",
            str(out_path)]
    else:
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
        raise RuntimeError(f"S2V WEBP→MP4 변환 실패: {result.stderr[-300:]}")


async def generate_lipsync_clip(
    project_id: str,
    scene_no: int,
    vocals_path: str,
    scene_start_sec: float = 0.0,
    clip_duration: float = 5.0,
    prompt: str = "Anime character singing expressively",
    has_vocal: bool = True,
    is_vocalist: bool = True,
    shot_type: str = "medium",
    abort_check=None,
) -> str:
    """Wan 2.2 S2V로 립싱크 클립 생성. vocals_path는 분리된 보컬 파일."""
    from backend.utils.audio_utils import extract_audio_segment

    out = clip_path(project_id, scene_no)
    if out.exists() and out.stat().st_size > 1000:
        print(f"[S2V] 장면 {scene_no} 클립 이미 존재, 건너뜀", file=sys.stderr)
        return str(out)

    prefix = f"s2v_{project_id[:8]}_{scene_no:02d}"
    output_dir = COMFYUI_DIR / "output"

    # ComfyUI webp가 이미 있으면 변환만 수행 (서버 재시작으로 변환 전에 죽은 경우)
    existing_webps = sorted(output_dir.glob(f"{prefix}*.webp"),
                            key=lambda f: f.stat().st_mtime, reverse=True)
    if existing_webps:
        print(f"[S2V] 장면 {scene_no} 기존 webp 발견, 변환만 수행", file=sys.stderr)
        out.parent.mkdir(parents=True, exist_ok=True)
        # 48kHz 오디오 준비 (변환에 필요)
        input_dir = COMFYUI_DIR / "input"
        audio_48k = input_dir / f"s2v_48k_{project_id[:8]}_{scene_no:02d}.wav"
        if not audio_48k.exists():
            from backend.utils.audio_utils import extract_audio_segment
            await extract_audio_segment(
                vocals_path, scene_start_sec, clip_duration, audio_48k,
                sr=48000, pre_silence_ms=50)
        await _convert_webp_to_mp4(prefix, out, audio_path=str(audio_48k))
        audio_48k.unlink(missing_ok=True)
        print(f"[S2V] 장면 {scene_no} 클립 변환 완료 ({out.stat().st_size} bytes)",
              file=sys.stderr)
        return str(out)

    input_dir = COMFYUI_DIR / "input"
    input_dir.mkdir(parents=True, exist_ok=True)

    # 이미지 복사 (Imagen 576×1024 → S2V도 동일 해상도 사용)
    src_img = str(image_path(project_id, scene_no))
    img_name = f"s2v_{project_id[:8]}_{scene_no:02d}.png"
    img = Image.open(src_img)
    if img.size != (WIDTH, HEIGHT):
        img = img.resize((WIDTH, HEIGHT), Image.LANCZOS)
    img.save(str(input_dir / img_name))

    # 오디오: 원본 세그먼트(합성용) + 16kHz mono(S2V 입력용)
    audio_name = f"s2v_audio_{project_id[:8]}_{scene_no:02d}.wav"
    audio_out = input_dir / audio_name
    audio_48k = input_dir / f"s2v_48k_{project_id[:8]}_{scene_no:02d}.wav"

    # 원본 48kHz 세그먼트 추출 (최종 합성용으로 보존)
    actual_dur = await extract_audio_segment(
        vocals_path, scene_start_sec, clip_duration, audio_48k,
        sr=48000, pre_silence_ms=50)

    # 16kHz mono 변환 (S2V Wav2Vec2 입력용)
    conv_result = await asyncio.to_thread(
        subprocess.run,
        ["ffmpeg", "-y", "-i", str(audio_48k),
         "-ar", "16000", "-ac", "1", "-acodec", "pcm_s16le",
         str(audio_out)],
        capture_output=True, text=True)

    if conv_result.returncode != 0:
        import shutil as _shutil
        _shutil.copy2(str(audio_48k), str(audio_out))

    num_frames = _calc_frames(actual_dur)
    num_frames = min(num_frames, 161)  # 최대 ~10초 (16fps × 10)

    # S2V 프롬프트 (모션 지시만 + image_prompt 스타일 존중)
    if has_vocal and is_vocalist:
        s2v_prompt = (
            f"The character sings expressively with natural lip movements, "
            f"subtle head motion, emotional facial expressions. "
            f"{prompt}")
    else:
        s2v_prompt = (
            f"The character moves subtly with gentle body motion. "
            f"{prompt}")

    seed = int(time.time()) % 2**32 + scene_no

    print(f"[S2V] 장면 {scene_no}: {s2v_prompt[:80]}...", file=sys.stderr)
    print(f"[S2V] {num_frames}프레임, shift={SHIFT}, steps={STEPS}", file=sys.stderr)

    wf = _build_s2v_workflow(
        img_name, audio_name, s2v_prompt, seed,
        num_frames=num_frames, output_prefix=prefix)

    await _queue_and_wait(wf, timeout=3600, abort_check=abort_check)
    out.parent.mkdir(parents=True, exist_ok=True)

    # 48kHz 오디오로 합성 (립싱크는 S2V가 처리, 최종 오디오는 고음질)
    await _convert_webp_to_mp4(prefix, out, audio_path=str(audio_48k))

    # 임시 파일 정리
    audio_48k.unlink(missing_ok=True)

    print(f"[S2V] 장면 {scene_no} 클립 생성 완료 ({out.stat().st_size} bytes)",
          file=sys.stderr)
    return str(out)
