import asyncio
import json
import math
import sys
import uuid
import aiosqlite
from datetime import datetime
from pathlib import Path
from backend.config import settings
from backend.database import DB_PATH
from backend.utils.progress import ProgressEmitter
from backend.utils.file_manager import (
    lyrics_path, music_path, image_path, clip_path, video_path
)
from backend.services.gemini_script_service import generate_story, generate_scenes
from backend.services.gemini_image_service import generate_images
from backend.services.suno_service import generate_music, measure_audio_duration
from backend.services.wan_video_service import generate_video_clips as wan_generate_clips
from backend.services.wan_video_service import get_clip_duration
from backend.services.wan_video_service import _AbortedError as _ComfyAbortError
from backend.services.gemini_image_service import ImageAbortedError as _ImageAbortError
from backend.services.wan_s2v_service import is_available as s2v_available
from backend.services.wan_s2v_service import generate_lipsync_clip as s2v_generate_lipsync
from backend.services.lipsync_precheck import separate_vocals
from backend.services.ffmpeg_service import render_video
from backend.services.lyrics_sync_service import extract_lyrics_timestamps
from backend.models.project import GeneratedScript, ScriptScene


def _free_comfyui_vram(tag: str = ""):
    """ComfyUI 모델 언로드 + VRAM 해제 — 모델 전환 시점에만 호출."""
    try:
        import urllib.request as _ur
        _req = _ur.Request(
            "http://127.0.0.1:8189/free",
            data=json.dumps({"unload_models": True, "free_memory": True}).encode(),
            headers={"Content-Type": "application/json"})
        _ur.urlopen(_req)
        print(f"[STEP4] VRAM 해제 완료 ({tag})", file=sys.stderr)
    except Exception:
        pass


def _clear_comfyui_queue():
    """ComfyUI 대기 큐 + 현재 실행 중인 작업 취소 — 재시작 시 이전 워크플로우 제거."""
    try:
        import urllib.request as _ur
        # 큐 대기 항목 삭제
        _req = _ur.Request(
            "http://127.0.0.1:8189/queue",
            data=json.dumps({"clear": True}).encode(),
            headers={"Content-Type": "application/json"})
        _ur.urlopen(_req)
        # 현재 실행 중인 작업 중단
        _req2 = _ur.Request(
            "http://127.0.0.1:8189/interrupt",
            data=b"",
            headers={"Content-Type": "application/json"},
            method="POST")
        _ur.urlopen(_req2)
        print("[STEP4] ComfyUI 큐 클리어 + 현재 작업 중단", file=sys.stderr)
    except Exception:
        pass


async def _update_project(project_id: str, **kwargs):
    async with aiosqlite.connect(DB_PATH) as db:
        sets = ", ".join(f"{k}=?" for k in kwargs)
        vals = list(kwargs.values()) + [project_id]
        await db.execute(
            f"UPDATE projects SET {sets}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            vals
        )
        await db.commit()


async def _log_step(project_id: str, step_no: int, step_name: str,
                    status: str, output_data: dict = None,
                    error_msg: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        now = datetime.utcnow().isoformat()
        if status == "running":
            # 기존 행 삭제 후 새로 삽입 (resume 시 중복 방지)
            await db.execute(
                "DELETE FROM pipeline_steps WHERE project_id=? AND step_no=?",
                (project_id, step_no))
            await db.execute(
                """INSERT INTO pipeline_steps
                   (project_id, step_no, step_name, status, started_at,
                    finished_at, output_data, error_msg)
                   VALUES (?,?,?,?,?,NULL,?,?)""",
                (project_id, step_no, step_name, status, now,
                 json.dumps(output_data or {}, ensure_ascii=False),
                 error_msg)
            )
        else:
            # 완료/실패: finished_at 기록, started_at 유지
            await db.execute(
                """UPDATE pipeline_steps SET status=?, finished_at=?,
                   output_data=?, error_msg=?
                   WHERE project_id=? AND step_no=?""",
                (status, now,
                 json.dumps(output_data or {}, ensure_ascii=False),
                 error_msg, project_id, step_no)
            )
        await db.commit()


async def _update_step_progress(project_id: str, step_no: int,
                                step_name: str, current: int, total: int):
    """running 중인 스텝의 진행률(current/total)을 DB에 업데이트."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE pipeline_steps SET output_data=?
               WHERE project_id=? AND step_no=? AND status='running'""",
            (json.dumps({"current": current, "total": total}, ensure_ascii=False),
             project_id, step_no)
        )
        await db.commit()


async def _get_completed_steps(project_id: str) -> dict:
    """DB에서 완료된 STEP 번호와 output_data를 반환."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT step_no, output_data FROM pipeline_steps "
            "WHERE project_id=? AND status='done' ORDER BY step_no",
            (project_id,)
        )
        rows = await cursor.fetchall()
        return {r["step_no"]: json.loads(r["output_data"] or "{}") for r in rows}


def _read_lyrics(project_id: str) -> dict:
    """lyrics.json 읽기."""
    lp = lyrics_path(project_id)
    if not lp.exists():
        return {}
    return json.loads(lp.read_text(encoding="utf-8"))


def _write_lyrics(project_id: str, data: dict):
    """lyrics.json 쓰기."""
    lp = lyrics_path(project_id)
    lp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_clip_slot(project_id: str, scene, status: str = "pending",
                     has_clip: bool = False, _has_vocals_fn=None) -> dict:
    """클립 슬롯 dict 생성 (Step 4 init/progress/final 공용)."""
    sno = scene.scene_no
    slot = {
        "status": status,
        "image_url": f"/storage/projects/{project_id}/images/scene_{sno:02d}.png",
        "start_sec": getattr(scene, 'start_sec', 0),
        "end_sec": getattr(scene, 'end_sec', 0),
        "duration": getattr(scene, 'duration', 0),
        "vocal_lines": getattr(scene, 'vocal_lines', []),
        "description": getattr(scene, 'description', ''),
        "image_prompt": getattr(scene, 'image_prompt', ''),
        "shot_type": getattr(scene, 'shot_type', 'medium'),
        "_has_vocal": _has_vocals_fn(scene) if _has_vocals_fn else False,
        "is_vocalist": getattr(scene, "is_vocalist", False),
    }
    if has_clip:
        slot["url"] = f"/storage/projects/{project_id}/clips/clip_{sno:02d}.mp4"
    return slot


def _story_emit_data(story_data: dict, mood: str) -> dict:
    """Step 1 완료 시 emitter에 전달할 데이터."""
    return {
        "title": story_data["title"], "lyrics": story_data["lyrics"],
        "art_style": story_data.get("art_style", ""),
        "vocal_style": story_data.get("vocal_style", ""),
        "characters": story_data.get("characters", []),
        "mood": mood,
    }


async def _merge_audio_to_clip(clip_path: str, audio_path: str,
                               start_sec: float, duration: float):
    """클립 영상에 오디오 구간을 합성 (in-place)."""
    import subprocess, tempfile
    tmp = tempfile.NamedTemporaryFile(suffix='.mp4', delete=False)
    tmp.close()
    cmd = [
        'ffmpeg', '-y',
        '-i', clip_path,
        '-ss', f'{start_sec:.2f}', '-t', f'{duration:.2f}', '-i', audio_path,
        '-c:v', 'copy', '-c:a', 'aac', '-b:a', '128k',
        '-map', '0:v:0', '-map', '1:a:0',
        '-shortest', '-movflags', '+faststart',
        tmp.name,
    ]
    result = await asyncio.to_thread(
        subprocess.run, cmd, capture_output=True, text=True)
    if result.returncode == 0:
        Path(tmp.name).replace(clip_path)
    else:
        Path(tmp.name).unlink(missing_ok=True)
        raise RuntimeError(result.stderr[-200:])


def _apply_corrected_words(timed_lines: list, original_words: list, corrected_words: list):
    """보정된 단어를 원본 타이밍에 매핑. SequenceMatcher로 정렬하여 타이밍 보존."""
    from difflib import SequenceMatcher

    orig_texts = [w["text"].lower() for w in original_words]
    corr_texts = [w.lower() for w in corrected_words]

    # 매칭: 원본 단어 인덱스 → 보정 단어 매핑
    matcher = SequenceMatcher(None, orig_texts, corr_texts)
    # 결과: 원본 인덱스별 새 단어 리스트 (삽입 포함)
    new_words_flat = []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            # 매칭 → 원본 타이밍 유지, 텍스트만 보정본으로
            for k, oi in enumerate(range(i1, i2)):
                w = original_words[oi].copy()
                w["text"] = corrected_words[j1 + k]
                new_words_flat.append(w)
        elif op == "replace":
            # 대체 → 원본 시간 범위에 보정 단어들 배분
            t_start = original_words[i1]["start"]
            t_end = original_words[i2 - 1]["end"]
            n = j2 - j1
            step = (t_end - t_start) / n if n > 0 else 0
            for k in range(n):
                new_words_flat.append({
                    "text": corrected_words[j1 + k],
                    "start": round(t_start + k * step, 3),
                    "end": round(t_start + (k + 1) * step, 3),
                })
        elif op == "insert":
            # 삽입 → 이전 단어와 다음 단어 사이 시간에 배분
            if new_words_flat:
                t_start = new_words_flat[-1]["end"]
            elif i1 < len(original_words):
                t_start = original_words[i1]["start"]
            else:
                t_start = 0.0
            if i1 < len(original_words):
                t_end = original_words[i1]["start"]
            elif new_words_flat:
                t_end = t_start + 0.5
            else:
                t_end = t_start + 0.5
            if t_end <= t_start:
                t_end = t_start + 0.3
            n = j2 - j1
            step = (t_end - t_start) / n if n > 0 else 0
            for k in range(n):
                new_words_flat.append({
                    "text": corrected_words[j1 + k],
                    "start": round(t_start + k * step, 3),
                    "end": round(t_start + (k + 1) * step, 3),
                })
        elif op == "delete":
            # 삭제 → 원본 단어 스킵 (Gemini가 제거한 것)
            pass

    # 평탄화된 words를 세그먼트에 재배분 (시간 기준)
    for sg in timed_lines:
        s, e = sg["start"], sg["end"]
        sg["words"] = [w for w in new_words_flat if s <= w["start"] < e]

    print(f"[LyricsSync] 단어 매핑 완료: 원본 {len(original_words)}개 → 보정 {len(new_words_flat)}개",
          file=sys.stderr)


async def _correct_lyrics_with_gemini(raw_lyrics: list[str], story_text: str) -> list[str]:
    """Gemini Flash로 Whisper 가사 오타 보정."""
    from backend.utils.gemini_client import gemini_generate

    whisper_joined = " ".join(raw_lyrics)
    prompt = f"""AI 음성 인식(Whisper)으로 추출한 한국어 노래 가사를 원본 가사와 비교하여 교정하세요.
전체 가사의 흐름을 보고 교정해주세요.

규칙:
- 원본에 나오는 단어와 발음이 비슷한 Whisper 오인식을 적극 교정 (예: "곤 엮고" → "곤룡포", "벌" → "궁궐", "나카라코코" → "마스카라 콕콕")
- 원본에 등장하는 고유명사, 키워드를 우선 매칭하세요
- 원본에 있는 단어가 Whisper에서 누락된 경우 추가 가능 (예: "꿈을 쏴올려" → "꿈을 쏘아올린 빛")
- 단, **맨 끝**에는 누락 단어를 추가하지 마세요 (녹음에서 잘린 부분일 수 있음)
- 보컬라이즈/허밍(오 아 Oh 등)은 그대로 유지하세요 (삭제하지 마세요)
- 원본에 없는 내용을 새로 만들지 마세요
- 보정된 가사를 공백으로 구분된 단어 나열로 출력하세요 (줄바꿈 없이 한 줄로)

[원본 가사]
{story_text[:500]}

[Whisper 추출 가사]
{whisper_joined}"""

    resp = await gemini_generate(
        model="gemini-2.5-flash",
        contents=prompt)
    result = resp.text.strip().replace("\n", " ").strip()
    print(f"[LyricsSync] Gemini 보정: '{result[:60]}...'", file=sys.stderr)
    return result.split()


async def _clean_step_files(project_id: str, from_step: int, reset: bool = False):
    """from_step 이후 스텝의 캐시 파일 삭제 — 이전 스텝이 재생성되면 후속 스텝도 새로 만들도록."""
    import shutil
    pdir = Path(lyrics_path(project_id)).parent

    if from_step <= 2:
        if reset:
            mp = music_path(project_id)
            if mp.exists():
                mp.unlink()
        # demucs 캐시 삭제 (Whisper 재분석용)
        demucs_dir = pdir / "demucs"
        if demucs_dir.exists():
            shutil.rmtree(demucs_dir, ignore_errors=True)
    # lyrics.json에서 scenes 데이터 초기화 (과거 vocal_lines 제거)
    if from_step <= 3:
        lp = pdir / "lyrics.json"
        if lp.exists():
            try:
                import json
                data = _read_lyrics(project_id)
                changed = False
                keys_to_del = ["scenes"]
                if from_step <= 2:
                    keys_to_del.append("whisper_lyrics")
                for key in keys_to_del:
                    if key in data:
                        del data[key]
                        changed = True
                if changed:
                    _write_lyrics(project_id, data)
            except Exception:
                pass
    if from_step <= 3:
        imgs_dir = pdir / "images"
        if imgs_dir.exists():
            shutil.rmtree(imgs_dir, ignore_errors=True)
            imgs_dir.mkdir(parents=True, exist_ok=True)
    if from_step <= 3 or (from_step == 4 and reset):
        clips_dir = pdir / "clips"
        if clips_dir.exists():
            shutil.rmtree(clips_dir, ignore_errors=True)
            clips_dir.mkdir(parents=True, exist_ok=True)
    if from_step <= 5:
        video_dir = pdir / "video"
        if video_dir.exists():
            shutil.rmtree(video_dir, ignore_errors=True)
        concat_file = pdir / "concat.txt"
        if concat_file.exists():
            concat_file.unlink()


def _count_files(directory: Path, pattern: str) -> int:
    """디렉토리에서 패턴에 맞는 파일 수를 센다."""
    if not directory.exists():
        return 0
    return len(list(directory.glob(pattern)))


_MAX_VOCAL_RETRY = 2  # 보컬 감지 실패 시 Step 1부터 재시도 최대 횟수


async def run_pipeline(
    project_id: str,
    theme: str,
    mood: str,
    emitter: ProgressEmitter,
    resume_from: int = 0,
    length: str = "short",
    skip_clean: bool = False,
):
    """파이프라인 실행. 보컬 감지 실패 시 Step 1부터 자동 재시도.
    skip_clean=True면 resume 시 _clean_step_files를 건너뜀 (장면 재생성용)."""
    for attempt in range(_MAX_VOCAL_RETRY + 1):
        try:
            result = await _run_pipeline_steps(
                project_id, theme, mood, emitter,
                resume_from=resume_from, length=length,
                skip_clean=skip_clean)
            return result  # 성공
        except _VocalDetectionError:
            if attempt >= _MAX_VOCAL_RETRY:
                raise RuntimeError("보컬 감지 실패 — 재시도 소진")
            print(f"[Pipeline] 보컬 감지 실패 → Step 1부터 재시도 "
                  f"({attempt + 1}/{_MAX_VOCAL_RETRY})", file=sys.stderr)
            await emitter.update(2, "running",
                f"보컬 감지 실패, Step 1부터 재시도 ({attempt + 1}/{_MAX_VOCAL_RETRY})")
            await _clean_step_files(project_id, 1)
            resume_from = 0  # 다음 루프에서 Step 1부터


class _VocalDetectionError(Exception):
    """보컬 감지 실패 — Step 1 재시도 트리거용."""
    pass


class _PipelineAbortError(Exception):
    """파이프라인 중단 요청 — 장면 재생성 등으로 현재 작업 중단."""
    pass


async def _run_pipeline_steps(
    project_id: str,
    theme: str,
    mood: str,
    emitter: ProgressEmitter,
    resume_from: int = 0,
    length: str = "short",
    skip_clean: bool = False,
):
    current_step = 0
    try:
        await _update_project(project_id, status="running")

        # 재시도 시 완료된 STEP 확인 — resume_from 이전 스텝만 캐시 사용
        if resume_from > 0:
            all_completed = await _get_completed_steps(project_id)
            completed = {k: v for k, v in all_completed.items() if k < resume_from}
            if not skip_clean:
                await _clean_step_files(project_id, resume_from)
            # 완료된 스텝의 원래 시작 시간을 emitter에 복원 (0초 소요 버그 방지)
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                rows = await db.execute_fetchall(
                    "SELECT step_no, started_at FROM pipeline_steps "
                    "WHERE project_id=? AND status='done'", (project_id,))
                for r in rows:
                    if r["started_at"]:
                        emitter._step_starts[r["step_no"]] = r["started_at"]
        else:
            completed = {}

        # ── STEP 1: 스토리/컨셉 생성 ──────────────────────────
        current_step = 1
        lp = lyrics_path(project_id)
        if 1 in completed and lp.exists():
            story_data = _read_lyrics(project_id)
            await emitter.update(1, "done",
                f"스토리 완성: '{story_data['title']}'",
                _story_emit_data(story_data, mood))
        else:
            await emitter.update(1, "running", "Gemini가 스토리와 작곡 지시를 구성하는 중...")
            await _log_step(project_id, 1, "스토리 생성", "running")
            story_data = await generate_story(theme, mood, length=length)
            _write_lyrics(project_id, story_data)
            await _update_project(project_id, title=story_data["title"])
            await _log_step(project_id, 1, "스토리 생성", "done",
                            {"title": story_data["title"]})
            await emitter.update(1, "done",
                f"스토리 완성: '{story_data['title']}'",
                _story_emit_data(story_data, mood))

        title = story_data["title"]
        lyrics = story_data["lyrics"]
        music_prompt = story_data["music_prompt"]
        characters = story_data.get("characters", [])
        art_style = story_data.get("art_style", "Pixar-style 3D animation")

        # ── STEP 2: 음악 생성 → 곡 길이 측정 → scene_count 결정 ──
        current_step = 2
        audio = music_path(project_id)
        if audio.exists():
            audio_file = str(audio)
            actual_duration = await measure_audio_duration(audio_file)
            scene_count = max(3, round(actual_duration / get_clip_duration()))
            await emitter.update(2, "running",
                f"음악 로드 완료, 가사 분석 중...",
                {"audio_url": f"/storage/projects/{project_id}/music/output.mp3"})
        else:
            await emitter.update(2, "running",
                "Suno AI가 음악을 생성하는 중... ")
            await _log_step(project_id, 2, "음악 생성", "running")
            audio_file, actual_duration = await generate_music(
                project_id, music_prompt, lyrics, length=length
            )
            scene_count = max(3, round(actual_duration / get_clip_duration()))
            await emitter.update(2, "running",
                f"음악 생성 완료! 가사 분석 준비 중...",
                {"audio_url": f"/storage/projects/{project_id}/music/output.mp3"})

        duration = int(scene_count * 8)

        # ── 가사 타임스탬프 추출 (Whisper) — 캐시 있으면 스킵 ──
        demucs_dir = str(Path(lyrics_path(project_id)).parent / "demucs")
        timed_lines = None
        _cached_lyrics = _read_lyrics(project_id).get("whisper_lyrics")
        if _cached_lyrics and len(_cached_lyrics) > 0 and resume_from >= 3:
            # 이미 분석된 결과 재사용 (구 문자열 형식 + 신 dict 형식 모두 지원)
            print("[LyricsSync] 캐시된 가사 사용 (Whisper 스킵)", file=sys.stderr)
            is_legacy = any(isinstance(t, str) for t in _cached_lyrics)
            if is_legacy:
                # 구 형식: 보컬만 저장 → 곡 길이 기준으로 전체 세그먼트 재구성
                print("[LyricsSync] 구 형식 캐시 → Whisper 재분석 필요", file=sys.stderr)
                timed_lines = None  # 강제 재분석
            else:
                # 신 형식: instrumental 포함 전체
                timed_lines = []
                for i, t in enumerate(_cached_lyrics):
                    seg = {"text": t.get("text", ""),
                           "start": t.get("start", i * 5.0),
                           "end": t.get("end", (i+1) * 5.0),
                           "has_vocal": t.get("has_vocal", False),
                           "words": t.get("words", [])}
                    # 빈 세그먼트 (0.0~0.0) 제거 — ceil→round 전환 시 남은 잔재
                    if seg["start"] == 0 and seg["end"] == 0 and not seg["text"]:
                        continue
                    timed_lines.append(seg)
                # scene_count와 동기화 (초과분 트리밍)
                if len(timed_lines) > scene_count:
                    timed_lines = timed_lines[:scene_count]
                    if timed_lines:
                        timed_lines[-1]["end"] = round(actual_duration, 2)
                elif len(timed_lines) < scene_count:
                    scene_count = len(timed_lines)
        try:
            if not timed_lines:
                if resume_from <= 2:
                    await emitter.update(2, "running",
                        f"가사 싱크 분석 중... (Whisper large-v3)",
                        {"audio_url": f"/storage/projects/{project_id}/music/output.mp3"})
                else:
                    print("[LyricsSync] 가사 캐시 없음, Whisper 재분석 (STEP 2 UI 유지)",
                          file=sys.stderr)
                timed_lines = await extract_lyrics_timestamps(
                    audio_file, lyrics, demucs_dir,
                    total_duration=actual_duration)

                # 보컬 감지 실패 → _VocalDetectionError로 상위 루프에서 재시도
                # 보컬 세그먼트 0개이거나, 인식된 총 단어가 가사의 30% 미만이면 실패
                vocal_count = sum(1 for sg in timed_lines if sg.get("has_vocal"))
                total_words = sum(len(sg.get("words", [])) for sg in timed_lines)
                expected_words = len(lyrics.replace("[End]", "").split())
                if vocal_count == 0 or (expected_words > 0 and total_words < expected_words * 0.5):
                    print(f"[STEP2] 보컬 감지 실패: {total_words}단어/{expected_words}예상",
                          file=sys.stderr)
                    raise _VocalDetectionError()

            # scene_count와 timed_lines 동기화 (트리밍을 보정 전에 수행)
            if len(timed_lines) > scene_count:
                # 초과 세그먼트 제거 (마지막 세그먼트의 end를 곡 끝으로)
                timed_lines = timed_lines[:scene_count]
                if timed_lines:
                    timed_lines[-1]["end"] = round(actual_duration, 2)
                print(f"[LyricsSync] 세그먼트 {len(timed_lines)}개로 트리밍 (scene_count={scene_count})",
                      file=sys.stderr)
            elif len(timed_lines) < scene_count:
                scene_count = len(timed_lines)

            # Gemini Flash로 가사 오타 보정 (캐시 아닐 때만, 트리밍 후)
            if not _cached_lyrics:
                if resume_from <= 2:
                    await emitter.update(2, "running", "가사 보정 중... (Gemini Flash)")
                raw_lyrics = [sg["text"] for sg in timed_lines if sg["text"].strip() and sg.get("words")]
                try:
                    corrected_words_raw = await _correct_lyrics_with_gemini(
                        raw_lyrics, lyrics)
                    # 구두점 제거
                    import re as _re
                    _strip_punct = lambda w: _re.sub(r'[.,!?;:~…\"\'\-]+', '', w).strip()
                    corrected_words = [_strip_punct(w) for w in corrected_words_raw if _strip_punct(w)]
                    # 원본 words 평탄화
                    original_words = []
                    for sg in timed_lines:
                        if sg.get("words"):
                            original_words.extend(sg["words"])
                    # SequenceMatcher로 원본↔보정 정렬, 타이밍 보존
                    _apply_corrected_words(timed_lines, original_words, corrected_words)
                    # 세그먼트 text도 words에서 재구성
                    for sg in timed_lines:
                        words = sg.get("words", [])
                        sg["text"] = " ".join(w["text"] for w in words) if words else ""
                        sg["has_vocal"] = len(words) > 0
                    # scenes의 vocal_lines도 보정된 text로 동기화
                    script_tmp = _read_lyrics(project_id)
                    for si, sc_d in enumerate(script_tmp.get("scenes", [])):
                        if si < len(timed_lines) and timed_lines[si]["text"].strip():
                            sc_d["vocal_lines"] = [timed_lines[si]["text"]]
                    _write_lyrics(project_id, script_tmp)
                    print(f"[LyricsSync] Gemini 가사 보정 완료 ({ci}줄)", file=sys.stderr)
                except Exception as e:
                    print(f"[LyricsSync] Gemini 보정 실패 (원본 사용): {e}", file=sys.stderr)
            for sg in timed_lines:
                label = sg["text"][:30] if sg["text"] else "(instrumental)"
                vocal = "♪" if sg["has_vocal"] else " "
                print(f"  {vocal} [{sg['start']:.1f}~{sg['end']:.1f}초] {label}",
                      file=sys.stderr)
        except Exception as e:
            print(f"[LyricsSync] 가사 싱크 실패: {e}", file=sys.stderr)
            timed_lines = None

        # Whisper 가사를 즉시 lyrics.json에 저장 (STEP 3 전에)
        # instrumental 포함 전체 세그먼트 저장 (resume 시 scene_count 동기화)
        early_lyrics = []
        if timed_lines:
            early_lyrics = [{"text": sg["text"], "start": sg["start"],
                             "end": sg["end"], "has_vocal": sg.get("has_vocal", False),
                             "words": sg.get("words", [])}
                            for sg in timed_lines]
            script_data_tmp = _read_lyrics(project_id)
            script_data_tmp["whisper_lyrics"] = early_lyrics
            _write_lyrics(project_id, script_data_tmp)

        # STEP 2 완료 (음악 생성 + 가사 분석 모두 끝남) — DB + 소켓 동시
        await _log_step(project_id, 2, "음악 생성", "done",
            {"audio_path": audio_file, "actual_duration": round(actual_duration, 1)})
        await emitter.update(2, "done",
            f"음악 + 가사 분석 완료!",
            {"audio_url": f"/storage/projects/{project_id}/music/output.mp3",
             "whisper_lyrics": early_lyrics})

        # ── STEP 3: 장면 구성 + 이미지 생성 ──────────────────────
        current_step = 3
        # lyrics.json에 scenes가 있으면 장면 구성 캐시 사용
        script_data = _read_lyrics(project_id)
        cached_scenes = script_data.get("scenes", [])
        imgs_dir = image_path(project_id, 1).parent
        img_count = _count_files(imgs_dir, "scene_*.png")

        if 3 in completed and cached_scenes and img_count >= len(cached_scenes):
            # 전부 완료 — 스킵
            scenes = [ScriptScene(**s) for s in cached_scenes]
            image_files = [str(image_path(project_id, i + 1))
                          for i in range(img_count)]
            await emitter.update(3, "done", f"이미지 {img_count}장",
                {"image_urls": [
                    f"/storage/projects/{project_id}/images/scene_{i+1:02d}.png"
                    for i in range(img_count)]})
        elif cached_scenes:
            # scenes 캐시 있음 — Gemini 장면 구성 스킵, 누락 이미지만 생성
            scenes = [ScriptScene(**s) for s in cached_scenes]
            # 누락된 이미지 확인
            missing = [i for i, sc in enumerate(scenes)
                       if not image_path(project_id, sc.scene_no).exists()]
            if missing:
                await emitter.update(3, "running",
                    f"누락 이미지 {len(missing)}장 재생성 중...")
                await _log_step(project_id, 3, "장면 구성 + 이미지", "running")

                async def _step3_progress(current, total):
                    existing_urls = [
                        f"/storage/projects/{project_id}/images/scene_{sc.scene_no:02d}.png"
                        for sc in scenes if image_path(project_id, sc.scene_no).exists()]
                    display = min(current + 1, total)
                    await emitter.update(3, "running",
                        f"이미지 생성 중... {display}/{total}",
                        {"current": display, "total": total, "image_urls": existing_urls})
                    await _update_step_progress(project_id, 3,
                        "장면 구성 + 이미지", display, total)

                # generate_images는 내부에서 파일 존재 시 건너뛰므로 전체 전달 OK
                image_files = await generate_images(
                    project_id, scenes, progress_cb=_step3_progress,
                    abort_check=lambda: emitter._abort)
            else:
                image_files = [str(image_path(project_id, sc.scene_no))
                              for sc in scenes]

            await _log_step(project_id, 3, "장면 구성 + 이미지", "done",
                            {"image_count": len(image_files)})
            await emitter.update(3, "done",
                f"이미지 {len(image_files)}장 완료!",
                {"image_urls": [
                    f"/storage/projects/{project_id}/images/scene_{sc.scene_no:02d}.png"
                    for sc in scenes]})
        else:
            # 3-1: 장면 구성 (Gemini) — 가사 싱크 타이밍 전달
            await emitter.update(3, "running",
                f"Gemini가 {scene_count}장면 구성 중...")
            await _log_step(project_id, 3, "장면 구성 + 이미지", "running")
            scenes = await generate_scenes(
                title, lyrics, mood, scene_count, duration,
                scene_timing=timed_lines,
                characters=characters,
                art_style=art_style)

            # 5초 세그먼트를 장면에 1:1 매칭
            if timed_lines:
                for i, sc in enumerate(scenes):
                    if i < len(timed_lines):
                        sg = timed_lines[i]
                        sc.start_sec = sg["start"]
                        sc.end_sec = sg["end"]
                        sc.duration = round(sg["end"] - sg["start"], 2)
                        sc.vocal_lines = [sg["text"]] if sg["text"] else []

            # _has_vocal 플래그를 scenes에 기록 (STEP 3/4 보컬 태그 통일)
            if timed_lines:
                for i, sc in enumerate(scenes):
                    if i < len(timed_lines):
                        sd = sc.dict() if hasattr(sc, 'dict') else vars(sc)
                        sd['_has_vocal'] = timed_lines[i].get("has_vocal", False)

            # whisper_lyrics 저장 + 프론트 전송
            # instrumental 포함 전체 세그먼트 저장 (resume 시 scene_count 동기화)
            whisper_lyrics = []
            for i, sc in enumerate(scenes):
                entry = {"text": sc.vocal_lines[0] if sc.vocal_lines else "",
                         "start": sc.start_sec, "end": sc.end_sec,
                         "has_vocal": bool(sc.vocal_lines and sc.vocal_lines[0].strip())}
                if timed_lines and i < len(timed_lines):
                    entry["has_vocal"] = timed_lines[i].get("has_vocal", False)
                    entry["words"] = timed_lines[i].get("words", [])
                whisper_lyrics.append(entry)
            script_data["whisper_lyrics"] = whisper_lyrics
            scenes_data = []
            for i, sc in enumerate(scenes):
                sd = sc.dict() if hasattr(sc, 'dict') else dict(sc)
                if timed_lines and i < len(timed_lines):
                    sd['_has_vocal'] = timed_lines[i].get("has_vocal", False)
                scenes_data.append(sd)
            script_data["scenes"] = scenes_data
            _write_lyrics(project_id, script_data)

            # 3-2: 이미지 생성 (Imagen 4 API)
            await emitter.update(3, "running",
                f"Imagen으로 {scene_count}개 이미지 생성 중...")
            async def _step3_progress(current, total):
                urls = [f"/storage/projects/{project_id}/images/scene_{i+1:02d}.png"
                        for i in range(current)]
                # 생성 중인 이미지 포함한 진행 수
                display = min(current + 1, total)
                await emitter.update(3, "running",
                    f"이미지 생성 중... {display}/{total}",
                    {"current": display, "total": total, "image_urls": urls})
                await _update_step_progress(project_id, 3,
                    "장면 구성 + 이미지", display, total)

            image_files = await generate_images(
                project_id, scenes, progress_cb=_step3_progress,
                abort_check=lambda: emitter._abort)
            await _log_step(project_id, 3, "장면 구성 + 이미지", "done",
                            {"image_count": len(image_files)})
            await emitter.update(3, "done",
                f"이미지 {len(image_files)}장 생성 완료!",
                {"image_urls": [
                    f"/storage/projects/{project_id}/images/scene_{i+1:02d}.png"
                    for i in range(len(image_files))]})

        # ── STEP 4: 이미지→영상 클립 (보컬=Wan S2V 립싱크, 나머지=Wan I2V) ──
        current_step = 4
        use_s2v = s2v_available()
        clip1 = clip_path(project_id, 1)
        if 4 in completed and clip1.exists():
            clips_dir = clip_path(project_id, 1).parent
            clip_count = _count_files(clips_dir, "clip_*.mp4")
            clip_files = [str(clip_path(project_id, i + 1)) for i in range(clip_count)]
            await emitter.update(4, "done", f"영상 클립 {clip_count}개",
                                 {"clip_urls": [
                                     f"/storage/projects/{project_id}/clips/clip_{i+1:02d}.mp4"
                                     for i in range(clip_count)
                                 ]})
        else:
            # STEP 4 시작 전 ComfyUI 큐 클리어 + VRAM 정리
            # (재시작 시 이전 워크플로우가 큐에 남아있는 문제 방지)
            await asyncio.to_thread(_clear_comfyui_queue)
            await asyncio.to_thread(_free_comfyui_vram, "Qwen→Wan STEP4 시작")

            # 보컬 분리 (EchoMimic + Whisper 판단용)
            demucs_dir = str(Path(lyrics_path(project_id)).parent / "demucs")
            vocals_path = await separate_vocals(audio_file, demucs_dir)
            print(f"[STEP4] 보컬 분리 완료: {vocals_path}", file=sys.stderr)

            # lyrics.json에서 _has_vocal 로드 (STEP 2에서 Whisper 분석한 결과 재사용)
            _vocal_map = {}
            try:
                _scenes_data = _read_lyrics(project_id).get("scenes", [])
                for sd in _scenes_data:
                    if "_has_vocal" in sd:
                        _vocal_map[sd.get("scene_no", 0)] = sd["_has_vocal"]
            except Exception:
                pass

            if _vocal_map:
                print(f"[STEP4] STEP 2 보컬 분석 결과 재사용 ({sum(_vocal_map.values())}/{len(_vocal_map)} 보컬)",
                      file=sys.stderr)
                print(f"[STEP4] _vocal_map: {_vocal_map}", file=sys.stderr)
            else:
                # _has_vocal 캐시 없으면 scenes 객체의 vocal_lines로 폴백
                print("[STEP4] _has_vocal 캐시 없음, vocal_lines 폴백 사용", file=sys.stderr)
                for sc in scenes:
                    vl = getattr(sc, 'vocal_lines', [])
                    has = bool(vl and any(l.strip() for l in vl))
                    _vocal_map[sc.scene_no] = has

            def _has_vocals(sc) -> bool:
                return _vocal_map.get(sc.scene_no, False)

            s2v_indices = [i for i, s in enumerate(scenes)
                           if _has_vocals(s)
                           and getattr(s, 'is_vocalist', False)]
            has_s2v = use_s2v and len(s2v_indices) > 0

            engine_parts = []
            if has_s2v:
                engine_parts.append(f"Wan 2.2 S2V {len(s2v_indices)}개 (보컬 립싱크)")
            non_ltx = len(scenes) - len(s2v_indices)
            if non_ltx > 0:
                engine_parts.append(f"Wan 2.2 I2V {non_ltx}개 (wide)")
            engine_desc = " + ".join(engine_parts)

            # Wan 대상 인덱스 (wide shots)
            wan_indices = [i for i in range(len(scenes)) if i not in s2v_indices]

            # 기존 클립 파일이 있으면 미리 채움 (재생성 시 다른 클립이 pending 안 되도록)
            clip_files = [None] * len(scenes)
            for _ci, _sc in enumerate(scenes):
                _cp = clip_path(project_id, _sc.scene_no)
                if _cp.exists():
                    clip_files[_ci] = str(_cp)

            # 초기 클립 슬롯 — 실제 첫 처리 클립에 스피너
            init_slots = []
            first_idx = wan_indices[0] if wan_indices else (s2v_indices[0] if s2v_indices else 0)
            for i, sc in enumerate(scenes):
                status = "done" if clip_files[i] else ("running" if i == first_idx else "pending")
                init_slots.append(_build_clip_slot(
                    project_id, sc, status, has_clip=bool(clip_files[i]),
                    _has_vocals_fn=_has_vocals))
            await emitter.update(4, "running",
                                 f"{engine_desc} 영상 클립 생성 중...",
                                 {"current": 0, "total": len(scenes),
                                  "clip_slots": init_slots})
            await _log_step(project_id, 4, "영상 클립 생성", "running")

            clip_duration = get_clip_duration()
            done_count = 0
            _current_clip_idx = -1  # 현재 제작중인 클립 인덱스

            async def _step4_progress_update():
                nonlocal done_count
                done_count = sum(1 for f in clip_files if f)
                # 전체 장면 슬롯: 완료된 건 clip URL, 미완료는 이미지 URL
                clip_slots = []
                for i, sc in enumerate(scenes):
                    status = "done" if clip_files[i] else ("running" if i == _current_clip_idx else "pending")
                    clip_slots.append(_build_clip_slot(
                        project_id, sc, status, has_clip=bool(clip_files[i]),
                        _has_vocals_fn=_has_vocals))
                # 생성 중인 클립 포함한 진행 수
                display_count = done_count + (1 if _current_clip_idx >= 0 else 0)
                data = {"current": display_count, "total": len(scenes),
                        "clip_slots": clip_slots}
                await emitter.update(4, "running",
                    f"영상 클립 생성 중... {display_count}/{len(scenes)}", data)
                # DB에도 저장 (dashboard 폴링용)
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        """UPDATE pipeline_steps SET output_data=?
                           WHERE project_id=? AND step_no=? AND status='running'""",
                        (json.dumps(data, ensure_ascii=False), project_id, 4))
                    await db.commit()

            async def _generate_s2v_clip(idx, scene):
                """S2V 립싱크 클립 생성 (폴백 포함)."""
                sno = scene.scene_no
                start_sec = scene.start_sec if scene.start_sec > 0 else idx * clip_duration
                scene_dur = scene.duration if scene.duration > 0 else clip_duration
                try:
                    result = await s2v_generate_lipsync(
                        project_id, sno, vocals_path,
                        scene_start_sec=start_sec,
                        clip_duration=scene_dur,
                        prompt=getattr(scene, 'image_prompt', scene.description),
                        has_vocal=_has_vocals(scene),
                        is_vocalist=getattr(scene, 'is_vocalist', True),
                        shot_type=getattr(scene, 'shot_type', 'medium'),
                        abort_check=lambda: emitter._abort)
                    clip_files[idx] = result
                except Exception as e:
                    print(f"[STEP4] S2V 실패 (장면 {sno}), 폴백: {e}", file=sys.stderr)
                    from backend.services.wan_video_service import _ffmpeg_still_video
                    still_out = clip_path(project_id, sno)
                    await _ffmpeg_still_video(image_files[idx], still_out, duration=scene_dur)
                    clip_files[idx] = str(still_out)

            async def _generate_i2v_clip(idx, scene):
                """I2V 모션 클립 단건 생성 (폴백 포함)."""
                sno = scene.scene_no
                scene_dur = scene.duration if scene.duration > 0 else clip_duration
                try:
                    wan_result = await wan_generate_clips(
                        project_id, [scene], [image_files[idx]],
                        abort_check=lambda: emitter._abort)
                    clip_files[idx] = wan_result[0]
                except Exception as e:
                    print(f"[STEP4] I2V 실패 (장면 {sno}), 폴백: {e}", file=sys.stderr)
                    from backend.services.wan_video_service import _ffmpeg_still_video
                    still_out = clip_path(project_id, sno)
                    await _ffmpeg_still_video(image_files[idx], still_out, duration=scene_dur)
                    clip_files[idx] = str(still_out)

            # 보컬 립싱크 먼저 → Wan 2.2 S2V (시간이 오래 걸리므로 우선 처리)
            if has_s2v and vocals_path:
                for idx in s2v_indices:
                    _current_clip_idx = idx
                    await _step4_progress_update()
                    await _generate_s2v_clip(idx, scenes[idx])
                    await _step4_progress_update()
                    if emitter._abort:
                        print(f"[STEP4] 중단 요청 감지 — S2V 루프 종료", file=sys.stderr)
                        raise _PipelineAbortError("파이프라인 중단 요청")

            # 와이드/비보컬 → Wan I2V
            wan_scenes = [s for i, s in enumerate(scenes) if i in wan_indices]
            wan_images = [f for i, f in enumerate(image_files) if i in wan_indices]

            if wan_scenes:
                if has_s2v and vocals_path:
                    await asyncio.to_thread(_free_comfyui_vram, "S2V→Wan I2V")
                # 첫 I2V 클립 스피너 표시
                _current_clip_idx = wan_indices[0]
                await _step4_progress_update()
                _wan_done_count = 0
                async def _wan_progress(current, total):
                    nonlocal _wan_done_count, _current_clip_idx
                    if current > _wan_done_count:
                        idx = wan_indices[current - 1]
                        clip_files[idx] = str(clip_path(project_id,
                                              scenes[idx].scene_no))
                        _wan_done_count = current
                    if current < total:
                        _current_clip_idx = wan_indices[current]
                    else:
                        _current_clip_idx = -1
                    await _step4_progress_update()
                    # 중단 요청 확인 (장면 재생성 등)
                    if emitter._abort:
                        print(f"[STEP4] 중단 요청 감지 — I2V 루프 종료", file=sys.stderr)
                        raise _PipelineAbortError("파이프라인 중단 요청")

                wan_results = await wan_generate_clips(
                    project_id, wan_scenes, wan_images,
                    progress_cb=_wan_progress,
                    abort_check=lambda: emitter._abort)
                for j, idx in enumerate(wan_indices):
                    clip_files[idx] = wan_results[j]

            # 누락된 클립 재생성 (실행 중 재생성 요청으로 삭제된 클립)
            missing = [(i, sc) for i, sc in enumerate(scenes)
                       if not clip_files[i] or not Path(clip_files[i]).exists()]
            if missing:
                print(f"[STEP4] 누락 클립 {len(missing)}개 재생성: "
                      f"{[sc.scene_no for _, sc in missing]}", file=sys.stderr)
                for idx, scene in missing:
                    _current_clip_idx = idx
                    await _step4_progress_update()
                    if idx in s2v_indices and use_s2v and vocals_path:
                        await _generate_s2v_clip(idx, scene)
                    else:
                        await _generate_i2v_clip(idx, scene)
                    await _step4_progress_update()
                    if emitter._abort:
                        raise _PipelineAbortError("파이프라인 중단 요청")

            clip_files = [f for f in clip_files if f]  # None 제거

            # 최종 clip_slots (뱃지 표시용)
            _current_clip_idx = -1
            final_slots = [_build_clip_slot(project_id, sc, "done", has_clip=True,
                                            _has_vocals_fn=_has_vocals) for sc in scenes]
            await _log_step(project_id, 4, "영상 클립 생성", "done",
                            {"clip_count": len(clip_files), "clip_slots": final_slots})
            await emitter.update(4, "done",
                                 f"영상 클립 {len(clip_files)}개 생성 완료!",
                                 {"clip_slots": final_slots})

        # ── STEP 5: 최종 영상 합성 (FFmpeg) ──────────────────────
        current_step = 5
        await emitter.update(5, "running", "최종 영상 합성 중...")
        await _log_step(project_id, 5, "영상 합성", "running")

        # scenes 데이터를 dict로 변환 (자막용)
        scenes_dicts = [
            {"scene_no": s.scene_no,
             "start_sec": getattr(s, 'start_sec', 0),
             "duration": getattr(s, 'duration', 5),
             "vocal_lines": getattr(s, 'vocal_lines', [])}
            for s in scenes
        ]
        # whisper_lyrics 로드 (정밀 타이밍 자막용)
        _whisper_lyrics = None
        try:
            _ldata = _read_lyrics(project_id)
            _whisper_lyrics = _ldata.get("whisper_lyrics")
        except Exception:
            pass

        final_video = await render_video(
            project_id, clip_files, audio_file,
            scenes=scenes_dicts, whisper_lyrics=_whisper_lyrics,
            title=title, theme=theme,
        )
        await _log_step(project_id, 5, "영상 합성", "done",
                        {"video_path": final_video})
        await _update_project(project_id, status="done", video_path=final_video)

        await emitter.update(5, "done", "영상 합성 완료!")
        await emitter.complete(final_video)

        # 자동 업로드 (source='auto' 작품만)
        try:
            from backend.services.upload_service import auto_upload_if_configured
            async with aiosqlite.connect(DB_PATH) as _db:
                _row = await (await _db.execute(
                    "SELECT source FROM projects WHERE id=?", (project_id,)
                )).fetchone()
                if _row and _row[0] == 'auto':
                    await auto_upload_if_configured(project_id)
        except Exception as _e:
            print(f"[Pipeline] 자동 업로드 스킵: {_e}", file=sys.stderr)

    except (_PipelineAbortError, _ComfyAbortError, _ImageAbortError):
        # 중단 요청 — 프로젝트 + 현재 스텝을 failed로 마킹
        print(f"[Pipeline] 중단됨 (project={project_id}, step={current_step})", file=sys.stderr)
        await _update_project(project_id, status="failed", error_msg="사용자 중단")
        step_names = {1: "스토리 생성", 2: "음악 생성", 3: "장면 구성 + 이미지",
                      4: "영상 클립 생성", 5: "영상 합성"}
        await _log_step(project_id, current_step,
                        step_names.get(current_step, f"STEP {current_step}"),
                        "failed", error_msg="사용자 중단")
        await emitter._broadcast({"type": "aborted", "step": current_step,
                                   "message": "파이프라인이 중단되었습니다."})
        emitter._done = True
        # 새 emitter가 이미 등록되었을 수 있으므로, 자기 자신일 때만 제거
        from backend.utils.progress import get_emitter, unregister_emitter
        if get_emitter(project_id) is emitter:
            unregister_emitter(project_id)
        return  # 정상 종료 — lock 해제됨
    except Exception as e:
        await _update_project(project_id, status="failed", error_msg=str(e))
        step_names = {
            1: "스토리 생성", 2: "음악 생성", 3: "장면 구성 + 이미지",
            4: "영상 클립 생성", 5: "영상 합성"
        }
        await _log_step(
            project_id, current_step,
            step_names.get(current_step, f"STEP {current_step}"),
            "failed", error_msg=str(e)
        )
        await emitter.error(current_step, str(e))
        raise
