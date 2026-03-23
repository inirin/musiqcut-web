"""로컬 FFmpeg 기반 영상 합성 — 비디오 클립 concat + 오디오 오버레이"""
import asyncio
import json
import subprocess
from pathlib import Path
from backend.utils.file_manager import video_path, project_dir

TARGET_FPS = 25  # 모든 클립을 이 FPS로 통일


async def _get_duration(file_path: str) -> float:
    """ffprobe로 파일 길이(초) 반환."""
    cmd = [
        'ffprobe', '-v', 'quiet', '-print_format', 'json',
        '-show_format', file_path,
    ]
    result = await asyncio.to_thread(
        subprocess.run, cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return 0.0
    data = json.loads(result.stdout)
    return float(data.get("format", {}).get("duration", 0))


async def render_video(
    project_id: str,
    clip_paths: list[str],
    audio_path: str,
) -> str:
    """비디오 클립들을 연결하고 오디오 합성."""
    out_path = video_path(project_id)
    proj = project_dir(project_id)

    # 1. FFmpeg concat 리스트 생성
    concat_file = proj / "concat.txt"
    lines_txt = []
    for p in clip_paths:
        abs_path = Path(p).resolve().as_posix()
        lines_txt.append(f"file '{abs_path}'")
    concat_file.write_text('\n'.join(lines_txt), encoding='utf-8')

    # 2. 클립 연결 — FPS 통일 (서로 다른 FPS 클립 안전 처리)
    concat_tmp = proj / "video" / "concat_tmp.mp4"
    cmd_concat = [
        'ffmpeg', '-y',
        '-f', 'concat', '-safe', '0', '-i', str(concat_file),
        '-vf', f'fps={TARGET_FPS}',
        '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
        '-an',
        str(concat_tmp),
    ]
    result = await asyncio.to_thread(
        subprocess.run, cmd_concat, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg concat 오류:\n{result.stderr[-500:]}")

    # 3. 비디오/오디오 길이 비교 → 부족하면 마지막 프레임 freeze
    video_dur = await _get_duration(str(concat_tmp))
    audio_dur = await _get_duration(audio_path)

    if audio_dur > video_dur + 0.04:
        # 마지막 프레임으로 나머지 채움 (tpad filter)
        padded = proj / "video" / "padded_tmp.mp4"
        pad_dur = audio_dur - video_dur
        cmd_pad = [
            'ffmpeg', '-y',
            '-i', str(concat_tmp),
            '-vf', f'tpad=stop_mode=clone:stop_duration={pad_dur:.2f}',
            '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
            str(padded),
        ]
        result = await asyncio.to_thread(
            subprocess.run, cmd_pad, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg pad 오류:\n{result.stderr[-500:]}")
        concat_tmp.unlink(missing_ok=True)
        concat_tmp = padded

    # 4. 오디오 합성 → 최종 영상
    cmd_final = [
        'ffmpeg', '-y',
        '-i', str(concat_tmp),
        '-i', audio_path,
        '-c:v', 'copy',
        '-c:a', 'aac', '-b:a', '192k',
        '-shortest',
        '-movflags', '+faststart',
        str(out_path),
    ]
    result = await asyncio.to_thread(
        subprocess.run, cmd_final, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg 최종 합성 오류:\n{result.stderr[-500:]}")

    # 임시 파일 정리
    concat_tmp.unlink(missing_ok=True)

    return str(out_path)
