"""공유 오디오 유틸리티 — 오디오 세그먼트 추출 등"""
import asyncio
import subprocess
from pathlib import Path


async def extract_audio_segment(
    audio_path: str, start_sec: float, duration_sec: float,
    out_wav: Path, sr: int = 48000, pre_silence_ms: int = 0
) -> float:
    """음원에서 특정 구간을 mono WAV로 추출. 실제 길이 반환.

    pre_silence_ms: 앞에 무음 패딩 추가 (립싱크 모델의 첫 프레임 싱크 보정용)
    """
    if pre_silence_ms > 0:
        # 무음 패딩 + 오디오 세그먼트 결합
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"anullsrc=r={sr}:cl=mono:d={pre_silence_ms / 1000:.3f}",
            "-ss", str(start_sec), "-t", str(duration_sec), "-i", audio_path,
            "-filter_complex", f"[0][1]concat=n=2:v=0:a=1[out]",
            "-map", "[out]", "-ar", str(sr), "-ac", "1",
            str(out_wav)]
    else:
        cmd = [
            "ffmpeg", "-y", "-i", audio_path,
            "-ss", str(start_sec), "-t", str(duration_sec),
            "-ar", str(sr), "-ac", "1",
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
