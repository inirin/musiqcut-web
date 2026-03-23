"""Suno AI 음악 생성 서비스 — sunoapi.org API 방식"""
import asyncio
import subprocess
import sys
import httpx
from pathlib import Path
from backend.config import settings
from backend.utils.file_manager import music_path

SUNO_BASE = "https://api.sunoapi.org"

# 숏폼 최대 허용 길이 (이 이상이면 fade-out 트림)
MAX_DURATION = {"short": 30}

# Suno 가사 시작/끝에 붙일 메타태그 (인트로 제거 + 곡 길이 제어)
LYRICS_PREFIX = {"short": "[Verse]\n"}
LYRICS_SUFFIX = {"short": "\n[End]"}


async def measure_audio_duration(file_path: str) -> float:
    """ffprobe로 오디오 파일의 실제 길이(초)를 측정."""
    cmd = [
        "ffprobe", "-v", "quiet", "-show_entries", "format=duration",
        "-of", "csv=p=0", file_path
    ]
    result = await asyncio.to_thread(
        subprocess.run, cmd, capture_output=True, text=True)
    if result.returncode == 0 and result.stdout.strip():
        return float(result.stdout.strip())
    raise RuntimeError(f"오디오 길이 측정 실패: {result.stderr}")


async def _trim_with_fadeout(file_path: str, max_sec: float,
                             fade_sec: float = 2.0):
    """곡이 max_sec을 초과하면 fade-out으로 트림."""
    duration = await measure_audio_duration(file_path)
    if duration <= max_sec:
        return  # 범위 내, 트림 불필요

    print(f"[STEP2] 곡 길이 {duration:.1f}초 > 최대 {max_sec}초, "
          f"fade-out 트림 적용", file=sys.stderr)

    trimmed = Path(file_path).with_suffix(".trimmed.mp3")
    cmd = [
        "ffmpeg", "-y", "-i", file_path,
        "-af", f"afade=t=out:st={max_sec - fade_sec}:d={fade_sec}",
        "-t", str(max_sec),
        str(trimmed),
    ]
    result = await asyncio.to_thread(
        subprocess.run, cmd, capture_output=True, text=True)
    if result.returncode == 0 and trimmed.exists():
        trimmed.replace(file_path)
    else:
        print(f"[STEP2] fade-out 트림 실패: {result.stderr[-200:]}",
              file=sys.stderr)


async def _trim_long_intro(file_path: str, max_intro_sec: float = 5.0):
    """Demucs 보컬 분리 → 보컬 트랙 Whisper → 인트로 트림.

    원본 mix에서는 보컬이 악기에 묻혀 Whisper가 감지 못할 수 있으므로
    반드시 보컬 분리 후 분석한다.
    """
    import shutil
    import tempfile

    try:
        # 1) 임시 디렉토리에 Demucs 보컬 분리
        tmp_demucs = tempfile.mkdtemp(prefix="intro_trim_")
        print(f"[STEP2] 인트로 분석: Demucs 보컬 분리 중...", file=sys.stderr)
        cmd_demucs = [
            sys.executable, "-m", "demucs",
            "--two-stems=vocals",
            "-o", tmp_demucs,
            str(Path(file_path).resolve()),
        ]
        proc = await asyncio.to_thread(
            subprocess.run, cmd_demucs, capture_output=True, text=True)

        stem_name = Path(file_path).stem
        vocals_path = Path(tmp_demucs) / "htdemucs" / stem_name / "vocals.wav"
        if proc.returncode != 0 or not vocals_path.exists():
            print(f"[STEP2] 보컬 분리 실패 — 인트로 트림 건너뜀", file=sys.stderr)
            shutil.rmtree(tmp_demucs, ignore_errors=True)
            return

        # 2) 보컬 트랙으로 Whisper 분석
        from faster_whisper import WhisperModel
        model = WhisperModel("base", device="cpu", compute_type="int8")
        segments, _ = model.transcribe(
            str(vocals_path), language="ko", vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=300))
        first_seg = None
        for seg in segments:
            first_seg = seg
            break

        shutil.rmtree(tmp_demucs, ignore_errors=True)

        if first_seg is None:
            print(f"[STEP2] 보컬 트랙에서도 보컬 미감지 — 트림 건너뜀",
                  file=sys.stderr)
            return

        vocal_start = first_seg.start
        print(f"[STEP2] 보컬 시작: {vocal_start:.1f}초", file=sys.stderr)

        if vocal_start <= max_intro_sec:
            print(f"[STEP2] 인트로 {vocal_start:.1f}초 ≤ {max_intro_sec}초 — 정상",
                  file=sys.stderr)
            return

        # 3) 보컬 2초 전부터 트림 (최소 0초)
        trim_start = max(0, vocal_start - 2.0)
        print(f"[STEP2] 인트로 {vocal_start:.1f}초 > {max_intro_sec}초, "
              f"{trim_start:.1f}초부터 트림", file=sys.stderr)
        trimmed = Path(file_path).with_suffix(".intro_trimmed.mp3")
        cmd = [
            "ffmpeg", "-y", "-i", file_path,
            "-ss", f"{trim_start:.2f}",
            "-acodec", "copy",
            str(trimmed),
        ]
        result = await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, text=True)
        if result.returncode == 0 and trimmed.exists():
            trimmed.replace(file_path)
            # Demucs 캐시 무효화 (프로젝트 루트/demucs)
            demucs_dir = Path(file_path).parent.parent / "demucs"
            if demucs_dir.exists():
                shutil.rmtree(demucs_dir, ignore_errors=True)
            print(f"[STEP2] 인트로 트림 완료 ({trim_start:.1f}초 제거)",
                  file=sys.stderr)
        else:
            print(f"[STEP2] 인트로 트림 실패: {result.stderr[-200:]}",
                  file=sys.stderr)
    except Exception as e:
        print(f"[STEP2] 인트로 트림 에러 (무시): {e}", file=sys.stderr)


async def generate_music(
    project_id: str,
    music_prompt: str,
    lyrics: str,
    length: str = "short",
) -> tuple[str, float]:
    """음악 생성 후 (파일 경로, 실제 곡 길이 초) 반환."""
    if not settings.suno_api_key:
        raise ValueError(
            "SUNO_API_KEY가 설정되지 않았습니다.\n"
            "sunoapi.org → 로그인 → API Keys에서 발급 후 설정 페이지에 입력하세요."
        )

    out_path = music_path(project_id)
    headers = {
        "Authorization": f"Bearer {settings.suno_api_key}",
        "Content-Type": "application/json"
    }

    # 보컬 강조: Suno가 instrumental로 빠지지 않도록 style에 보컬 강제 힌트 추가
    style = music_prompt
    vocal_keywords = ["vocal", "singing", "singer"]
    if not any(kw in style.lower() for kw in vocal_keywords):
        style = f"clear singing vocals, {style}"

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{SUNO_BASE}/api/v1/generate",
            headers=headers,
            json={
                "customMode": True,
                "instrumental": False,
                "model": "V3_5",
                "title": "MusiqCut",
                "style": style,
                "prompt": LYRICS_PREFIX.get(length, "") + lyrics + LYRICS_SUFFIX.get(length, ""),
                "callBackUrl": "https://example.com/callback",
            }
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 200:
            raise ValueError(f"Suno 생성 실패: {data.get('msg', data)}")

        task_id = data["data"]["taskId"]

        # 완료까지 폴링 (최대 10분)
        audio_url = await _poll_suno(client, headers, task_id, max_wait=600)

        # 다운로드
        audio_resp = await client.get(audio_url, timeout=120.0)
        audio_resp.raise_for_status()
        out_path.write_bytes(audio_resp.content)

    # 인트로 트림을 먼저! (원곡에서 보컬 전 인트로 제거)
    # 순서: 인트로 컷 → fade-out 트림 (이래야 최대 60초 분량이 유지됨)
    if length == "short":
        await _trim_long_intro(str(out_path), max_intro_sec=2.0)

    # 최대 길이 초과 시 fade-out 트림
    max_dur = MAX_DURATION.get(length, 60)
    await _trim_with_fadeout(str(out_path), max_dur)

    # 보컬 유무 체크 — Suno가 인스트루멘탈 생성하면 재시도
    has_vocal = True
    try:
        from backend.services.lipsync_precheck import check_vocal_energy
        has_vocal = await check_vocal_energy(str(out_path))
    except Exception:
        pass

    if not has_vocal:
        print("[STEP2] 보컬 미감지 → Suno 재시도 (1회)", file=sys.stderr)
        try:
            # 보컬 강조 스타일로 재생성
            retry_style = f"strong clear singing vocals throughout, {style}"
            async with httpx.AsyncClient(timeout=60.0) as client2:
                resp2 = await client2.post(
                    f"{SUNO_BASE}/api/v1/generate",
                    headers=headers,
                    json={
                        "customMode": True,
                        "instrumental": False,
                        "model": "V3_5",
                        "title": "MusiqCut",
                        "style": retry_style,
                        "prompt": LYRICS_PREFIX.get(length, "") + lyrics + LYRICS_SUFFIX.get(length, ""),
                        "callBackUrl": "https://example.com/callback",
                    }
                )
                resp2.raise_for_status()
                data2 = resp2.json()
                if data2.get("code") == 200:
                    task_id2 = data2["data"]["taskId"]
                    audio_url2 = await _poll_suno(client2, headers, task_id2, max_wait=600)
                    audio_resp2 = await client2.get(audio_url2, timeout=120.0)
                    audio_resp2.raise_for_status()
                    out_path.write_bytes(audio_resp2.content)
                    # 재트림
                    if length == "short":
                        import shutil
                        demucs_dir = out_path.parent.parent / "demucs"
                        if demucs_dir.exists():
                            shutil.rmtree(demucs_dir, ignore_errors=True)
                        await _trim_long_intro(str(out_path), max_intro_sec=2.0)
                    max_dur = MAX_DURATION.get(length, 60)
                    await _trim_with_fadeout(str(out_path), max_dur)
                    print("[STEP2] Suno 재시도 완료", file=sys.stderr)
        except Exception as e:
            print(f"[STEP2] Suno 재시도 실패 (원본 사용): {e}", file=sys.stderr)

    # 실제 곡 길이 측정 (트림 후)
    actual_duration = await measure_audio_duration(str(out_path))
    return str(out_path), actual_duration


async def _poll_suno(client: httpx.AsyncClient, headers: dict,
                     task_id: str, max_wait: int = 600) -> str:
    elapsed = 0
    while elapsed < max_wait:
        await asyncio.sleep(5)
        elapsed += 5

        resp = await client.get(
            f"{SUNO_BASE}/api/v1/generate/record-info",
            headers=headers,
            params={"taskId": task_id}
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 200:
            raise ValueError(f"Suno 폴링 오류: {data.get('msg', data)}")

        status = data["data"].get("status", "")

        if status == "SUCCESS":
            suno_data = data["data"]["response"]["sunoData"]
            if suno_data and suno_data[0].get("audioUrl"):
                return suno_data[0]["audioUrl"]
        elif status == "FAILED":
            raise ValueError(f"Suno 생성 실패: {data['data'].get('errorMessage', '알 수 없는 오류')}")

    raise TimeoutError(f"Suno 음악 생성 타임아웃 ({max_wait}초 초과)")
